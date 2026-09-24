"""Bedrock console over the itzg image's SSH remote console.

Vanilla BDS has no RCON; its console is the process's stdin. With
`ENABLE_SSH=true` the itzg/minecraft-bedrock-server image serves that console
over SSH on port 2222 inside the container (`mc-server-runner
--remote-console`): password-only auth with its `RCON_PASSWORD`, the
username ignored, interactive PTY sessions only, and every line the server
prints broadcast to each session. So one persistent session both sends
commands and receives their replies — interleaved with the ordinary server
log, which is why replies are matched by pattern rather than read in order.

Command replies (BDS prints them without the timestamped log prefix):

    give     → `Gave Stone * 64 to Steve`
    clear    → `Cleared the inventory of Steve, removing 5 items`
               `Could not clear the inventory of Steve, no items to remove`
    list     → `There are 2/10 players online:` then a line of names
    tellraw  → nothing on success
    offline  → `No targets matched selector`
    bad item → `Syntax error: Unexpected "foo": at "give Steve >>foo<<"`

Console commands ignore `allow-cheats=false` (they run as the server), so
the shop works on a survival server with cheats off — and must never run
anything that would flip that switch (see CLAUDE.md: Minecraft block shop).
"""
from __future__ import annotations

import asyncio
import logging
import re

import asyncssh

logger = logging.getLogger(__name__)

# How long to wait for a reply before giving up. Console commands answer
# within milliseconds; the slack is for a busy tick.
REPLY_TIMEOUT_SECS = 6.0
# tellraw answers only on failure; this is how long we listen for one.
SILENT_OK_SECS = 1.0
CONNECT_TIMEOUT_SECS = 8.0
# Lines kept for matching. Each command scans only lines that arrived after
# it was sent, so this just bounds memory on a chatty server.
LINE_BUFFER = 500

GIVE_OK = re.compile(r"^Gave (?P<item>.+) \* (?P<count>\d+) to (?P<player>.+)$")
CLEAR_OK = re.compile(r"^Cleared the inventory of (?P<player>.+), removing (?P<count>\d+) items?$")
CLEAR_NONE = re.compile(r"^Could not clear the inventory of (?P<player>.+), no items to remove$")
NO_TARGET = re.compile(r"^No targets matched selector")
SYNTAX_ERROR = re.compile(r"^(?:Syntax error|Unknown command|Unknown item|Item .* not found)", re.IGNORECASE)
LIST_HEADER = re.compile(r"^There are (?P<online>\d+)/(?P<max>\d+) players online:")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class ConsoleError(Exception):
    """The console couldn't be reached or didn't answer."""


class ConsoleRefused(Exception):
    """The server answered, and said no: player offline, unknown item."""


def clean_line(raw: str) -> str:
    """A PTY session carries colour codes and CRs; replies are matched bare."""
    return _ANSI.sub("", raw).replace("\r", "").strip()


def quote_name(gamertag: str) -> str:
    """Gamertags may contain spaces; the console takes them quoted."""
    return '"' + gamertag.replace('"', "") + '"'


def item_arg(item_id: str) -> str:
    return item_id if ":" in item_id else f"minecraft:{item_id}"


def parse_players(line: str) -> list[str]:
    """The names line that follows the `list` header: comma-separated."""
    return [n.strip() for n in line.split(",") if n.strip()]


class McConsole:
    """One SSH session to the console, opened on first use and reopened
    after any error. Commands are serialised: replies carry no id, so two
    in flight could claim each other's lines.

    `connect` is the asyncssh.connect stand-in tests inject; it must
    return an object with `create_process(term_type=...)` and `close()`."""

    def __init__(self, host: str, port: int, password: str, *, connect=None):
        self.host, self.port, self.password = host, port, password
        self._connect = connect or asyncssh.connect
        self._lock = asyncio.Lock()
        self._conn = None
        self._proc = None
        self._reader: asyncio.Task | None = None
        self._lines: list[str] = []
        self._base = 0          # absolute index of self._lines[0]
        self._arrived = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._proc is not None

    # ── session ─────────────────────────────────────────────────────────

    async def _ensure(self) -> None:
        if self._proc is not None:
            return
        try:
            self._conn = await asyncio.wait_for(
                self._connect(self.host, port=self.port, username="bot", password=self.password,
                              known_hosts=None),
                timeout=CONNECT_TIMEOUT_SECS,
            )
            self._proc = await self._conn.create_process(term_type="xterm")
        except Exception as exc:
            await self.close()
            raise ConsoleError(f"can't reach the console: {exc}") from exc
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        proc = self._proc
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = clean_line(raw)
                if not line:
                    continue
                self._lines.append(line)
                if len(self._lines) > LINE_BUFFER:
                    drop = len(self._lines) - LINE_BUFFER
                    del self._lines[:drop]
                    self._base += drop
                self._arrived.set()
        except Exception:
            logger.info("[mc console] session dropped", exc_info=True)
        finally:
            if self._proc is proc:
                self._proc = None
                self._conn = None
            self._arrived.set()  # wake a waiter so it sees the drop

    async def close(self) -> None:
        proc, conn, reader = self._proc, self._conn, self._reader
        self._proc = self._conn = self._reader = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
        for closable in (proc, conn):
            try:
                if closable is not None:
                    closable.close()
            except Exception:
                pass

    # ── one command ─────────────────────────────────────────────────────

    async def run(self, command: str, *, expect: tuple[re.Pattern, ...],
                  timeout: float | None = None, following: int = 0):
        """Send `command`, return the first later line matching any of
        `expect` — as `(match, following_lines)` — or raise ConsoleError
        when nothing matches in time (REPLY_TIMEOUT_SECS unless given).
        `following` extra lines after the match are collected (the `list`
        names line)."""
        if timeout is None:
            timeout = REPLY_TIMEOUT_SECS
        async with self._lock:
            await self._ensure()
            start = self._base + len(self._lines)
            try:
                self._proc.stdin.write(command + "\n")
            except Exception as exc:
                await self.close()
                raise ConsoleError(f"console write failed: {exc}") from exc
            return await self._wait(expect, timeout, start, following)

    async def _wait(self, expect, timeout: float, start: int, following: int):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        found = None  # (absolute line index, match)
        scan = start
        while True:
            end = self._base + len(self._lines)
            if found is None:
                for idx in range(max(scan, self._base), end):
                    for pattern in expect:
                        m = pattern.match(self._lines[idx - self._base])
                        if m:
                            found = (idx, m)
                            break
                    if found is not None:
                        break
                scan = end
            if found is not None and end - (found[0] + 1) >= following:
                first = max(found[0] + 1 - self._base, 0)
                return found[1], self._lines[first:first + following]
            if self._proc is None:
                raise ConsoleError("the console session dropped")
            remaining = deadline - loop.time()
            if remaining <= 0:
                if found is not None:  # header seen, the names line never came
                    return found[1], []
                raise ConsoleError("the console didn't answer")
            # No await between the scan and this clear, so a line can't slip
            # in unnoticed.
            self._arrived.clear()
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    async def silent(self, command: str, *, failure: tuple[re.Pattern, ...] = (NO_TARGET,)) -> None:
        """A command that answers only when it fails (tellraw). Raises
        ConsoleRefused on a failure line, returns after SILENT_OK_SECS of
        quiet."""
        try:
            m, _ = await self.run(command, expect=failure, timeout=SILENT_OK_SECS)
        except ConsoleError as exc:
            if "didn't answer" in str(exc):
                return
            raise
        raise ConsoleRefused(m.group(0))

    # ── the shop's verbs ────────────────────────────────────────────────

    async def online_players(self) -> list[str]:
        m, tail = await self.run("list", expect=(LIST_HEADER,), following=1)
        if int(m.group("online")) == 0 or not tail:
            return []
        return parse_players(tail[0])

    async def give(self, gamertag: str, item_id: str, count: int) -> int:
        """Put `count` of the item in the player's inventory; returns the
        count the server reports. ConsoleRefused when the player is offline
        or the item id is wrong."""
        m, _ = await self.run(
            f"give {quote_name(gamertag)} {item_arg(item_id)} {int(count)}",
            expect=(GIVE_OK, NO_TARGET, SYNTAX_ERROR),
        )
        if m.re is not GIVE_OK:
            raise ConsoleRefused(m.group(0))
        return int(m.group("count"))

    async def clear(self, gamertag: str, item_id: str, count: int) -> int:
        """Take up to `count` of the item out of the player's inventory;
        returns how many were actually removed (0 when they had none)."""
        m, _ = await self.run(
            f"clear {quote_name(gamertag)} {item_arg(item_id)} -1 {int(count)}",
            expect=(CLEAR_OK, CLEAR_NONE, NO_TARGET, SYNTAX_ERROR),
        )
        if m.re is CLEAR_OK:
            return int(m.group("count"))
        if m.re is CLEAR_NONE:
            return 0
        raise ConsoleRefused(m.group(0))

    async def tell(self, gamertag: str, text: str) -> None:
        """A private in-game message (tellraw). ConsoleRefused when offline."""
        safe = text.replace("\\", "\\\\").replace('"', '\\"')
        await self.silent(f'tellraw {quote_name(gamertag)} {{"rawtext":[{{"text":"{safe}"}}]}}')
