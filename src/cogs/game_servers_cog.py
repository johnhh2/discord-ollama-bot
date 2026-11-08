"""Online / offline / update notices for V Rising and Vintage Story servers.

Each configured server is polled every GAME_POLL_SECONDS. Polls fold through
minecraft_cog's `_mc_events`, so the offline debounce and the update-replaces-
online rule match Minecraft, and post via src/server_notices.py to the
shared `!settings channel game-servers` channel.

- **V Rising** answers Steam's A2S_INFO on its query port. That reply carries
  the player count, so it also posts join/leave notices
  (`!settings vrising-joins`). Its version is whatever A2S reports. A2S
  only answers while the server is listed on Steam, so with VRISING_RCON_PORT
  set it's polled like Vintage Story instead: online means the RCON port
  accepts a TCP connection, with no players, join notices or version.
- **Vintage Story** has no query protocol. Online means the game port accepts
  a TCP connection. The version comes from the public master list (~4 MB),
  fetched only while an online stretch has no version yet, and found only if
  the server advertises itself. There is no player count.

The last version seen is kept in `state.bot_settings` (`<key>_version`), so an
update that lands while the bot is down is announced on the first poll.

`!gameservers` (`!servers`, singular too) shows every configured server live;
its subcommands also answer bare as `!vrising` / `!vintagestory`, and
`!gameservers minecraft` forwards to `!mc`.
"""
import asyncio
import dataclasses
import logging
import socket
import time
from typing import Awaitable, Callable

import aiohttp
import discord
from discord.ext import commands, tasks

from src import state
import src.cogs.minecraft_cog as mc_mod
import src.persistence as persistence
from src.config import (
    VRISING_SERVER_HOST, VRISING_QUERY_PORT, VRISING_RCON_PORT,
    VINTAGESTORY_SERVER_HOST, VINTAGESTORY_SERVER_PORT,
)
from src.cogs.minecraft_cog import MonitorState, _mc_events
from src.forwarding import forward
from src.helpers import emb, C_GREEN, C_GREY, C_RED
from src.permissions import requires_perm
from src.server_notices import announce, event_payloads

logger = logging.getLogger(__name__)

GAME_POLL_SECONDS = 60
GAME_QUERY_TIMEOUT_SECS = 5.0

_A2S_INFO = b"\xff\xff\xff\xffTSource Engine Query\x00"

VS_MASTER_LIST_URL = "https://masterserver.vintagestory.at/api/v1/servers/list"
# The list is served with max-age=300, so asking sooner gets the same answer.
# A server takes a heartbeat or two to re-register after a restart; past the
# last try it's taken as unadvertised until its next restart.
VS_LOOKUP_INTERVAL_SECS = 300
VS_LOOKUP_TRIES = 4

# key → (display name, env var that enables it), for the "not configured" reply.
GAMES = {
    "vrising": ("V Rising", "VRISING_SERVER_HOST"),
    "vintagestory": ("Vintage Story", "VINTAGESTORY_SERVER_HOST"),
}
# Bare `!<name>` copies of `!gameservers <sub>`, registered as IdleCog does.
_TOP_ALIASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("vrising", "cmd_vrising", ()),
    ("vintagestory", "cmd_vintagestory", ("vintage",)),
)


@dataclasses.dataclass
class ServerStatus:
    players: int
    max_players: int
    version: str


@dataclasses.dataclass(frozen=True)
class GameServer:
    key: str                  # bot_settings prefix
    game: str                 # display name
    host: str
    port: int
    probe: Callable[[str, int], Awaitable[ServerStatus]]
    joins_key: "str | None"   # None: no player count, so no join notices

    @property
    def version_key(self) -> str:
        return f"{self.key}_version"


# ── V Rising: Steam A2S_INFO ─────────────────────────────────────────────────
class _OneReply(asyncio.DatagramProtocol):
    def __init__(self):
        self.reply = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, addr):
        if not self.reply.done():
            self.reply.set_result(data)

    def error_received(self, exc):
        if not self.reply.done():
            self.reply.set_exception(exc)


async def _udp_exchange(host: str, port: int, payload: bytes) -> bytes:
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(_OneReply, remote_addr=(host, port))
    try:
        transport.sendto(payload)
        return await asyncio.wait_for(proto.reply, GAME_QUERY_TIMEOUT_SECS)
    finally:
        transport.close()


def _parse_a2s_info(data: bytes) -> ServerStatus:
    if data[:5] != b"\xff\xff\xff\xffI":
        raise ValueError(f"unexpected A2S reply: {data[:8]!r}")
    i = 6  # header + protocol byte
    for _ in range(4):  # name, map, folder, game
        i = data.index(b"\0", i) + 1
    players, max_players = data[i + 2], data[i + 3]
    i += 9  # appid(2) players max bots type environment visibility vac
    version = data[i:data.index(b"\0", i)].decode("utf-8", "replace")
    return ServerStatus(players=players, max_players=max_players, version=version)


async def a2s_status(host: str, port: int) -> ServerStatus:
    reply = await _udp_exchange(host, port, _A2S_INFO)
    # Servers since 2020 answer the first request with a challenge ("A" + 4
    # bytes) that must be echoed back.
    if reply[:5] == b"\xff\xff\xff\xffA":
        reply = await _udp_exchange(host, port, _A2S_INFO + reply[5:9])
    return _parse_a2s_info(reply)


# ── Vintage Story: TCP connect + master list ─────────────────────────────────
async def tcp_status(host: str, port: int) -> ServerStatus:
    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), GAME_QUERY_TIMEOUT_SECS)
    writer.close()
    return ServerStatus(players=0, max_players=0, version="")


async def vintagestory_listed_version(host: str, port: int) -> "str | None":
    """The master list's gameVersion for host:port, or None if it isn't listed."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    wanted = {f"{addr}:{port}" for addr in {host, *(info[4][0] for info in infos)}}
    async with aiohttp.ClientSession() as session:
        async with session.get(VS_MASTER_LIST_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            listing = await resp.json(content_type=None)
    for entry in listing.get("data") or []:
        if entry.get("serverIP") in wanted:
            return str(entry.get("gameVersion") or "") or None
    return None


def configured_servers() -> list[GameServer]:
    servers = []
    if VRISING_SERVER_HOST and VRISING_RCON_PORT:
        servers.append(GameServer("vrising", "V Rising", VRISING_SERVER_HOST, VRISING_RCON_PORT,
                                  tcp_status, joins_key=None))
    elif VRISING_SERVER_HOST:
        servers.append(GameServer("vrising", "V Rising", VRISING_SERVER_HOST, VRISING_QUERY_PORT,
                                  a2s_status, joins_key="vrising_join_notices"))
    if VINTAGESTORY_SERVER_HOST:
        servers.append(GameServer("vintagestory", "Vintage Story", VINTAGESTORY_SERVER_HOST,
                                  VINTAGESTORY_SERVER_PORT, tcp_status, joins_key=None))
    return servers


class GameServersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.servers = configured_servers()
        self._monitors: dict[str, MonitorState] = {}
        self._last_seen_online: dict[str, float] = {}
        self._online_since: dict[str, float] = {}
        # Vintage Story's version for the current online stretch, and the
        # master-list tries spent finding it; reset when the server goes down.
        self._vs_version: "str | None" = None
        self._vs_tries = 0
        self._vs_tried_at = 0.0
        if self.servers:
            self.game_monitor.start()
        # Tests hand in a fake bot and call the callbacks directly.
        if isinstance(bot, commands.Bot):
            for name, attr, aliases in _TOP_ALIASES:
                source = getattr(self, attr)
                alias = commands.Command(source.callback, name=name, aliases=list(aliases),
                                         help=source.help, usage=source.usage)
                # Without it discord.py binds `self` to the Context.
                alias.cog = self
                bot.add_command(alias)

    def cog_unload(self):
        if self.servers:
            self.game_monitor.cancel()
        if isinstance(self.bot, commands.Bot):
            for name, _attr, _aliases in _TOP_ALIASES:
                self.bot.remove_command(name)

    @tasks.loop(seconds=GAME_POLL_SECONDS)
    async def game_monitor(self):
        await asyncio.gather(*(self._poll(server) for server in self.servers))

    @game_monitor.before_loop
    async def _before_monitor(self):
        await self.bot.wait_until_ready()
        for server in self.servers:
            self._monitors.setdefault(
                server.key, MonitorState(version=state.bot_settings.get(server.version_key) or None))

    async def _poll(self, server: GameServer):
        try:
            status = await server.probe(server.host, server.port)
        except Exception as exc:
            logger.debug("[%s] poll failed: %s", server.key, exc)
            status = None
        if status is not None and server.key == "vintagestory":
            status.version = await self._vintagestory_version(server)

        prev = self._monitors.get(server.key) or MonitorState()
        self._monitors[server.key], events = _mc_events(prev, status)
        now = time.time()
        if status is not None:
            self._last_seen_online[server.key] = now
            self._online_since.setdefault(server.key, now)
        elif self._monitors[server.key].online is False:
            self._online_since.pop(server.key, None)
            if server.key == "vintagestory":
                self._vs_version, self._vs_tries, self._vs_tried_at = None, 0, 0.0

        if status is not None and status.version and status.version != prev.version:
            state.bot_settings[server.version_key] = status.version
            try:
                await persistence.save_bot_settings()
            except Exception:
                logger.exception("[%s] failed to persist server version", server.key)

        if events:
            label = f"The {server.game} server"
            payloads = event_payloads(server.game, label, events, prev, status,
                                      self._last_seen_online.get(server.key) if status is None else None)
            await announce(self.bot, payloads, joins_key=server.joins_key)

    async def _vintagestory_version(self, server: GameServer) -> str:
        # "" until found: an empty version keeps the monitor's baseline.
        if self._vs_version is not None:
            return self._vs_version
        now = time.time()
        if self._vs_tries >= VS_LOOKUP_TRIES or now - self._vs_tried_at < VS_LOOKUP_INTERVAL_SECS:
            return ""
        self._vs_tries += 1
        self._vs_tried_at = now
        try:
            self._vs_version = await vintagestory_listed_version(server.host, server.port)
        except Exception:
            logger.warning("[vintagestory] master list lookup failed", exc_info=True)
        return self._vs_version or ""

    # ── !gameservers ──────────────────────────────────────────────────────────
    def _server(self, key: str) -> "GameServer | None":
        return next((s for s in self.servers if s.key == key), None)

    async def _live(self, server: GameServer) -> "ServerStatus | None":
        try:
            status = await server.probe(server.host, server.port)
        except Exception:
            return None
        # Vintage Story's version is never looked up live (the list is ~4 MB).
        if not status.version:
            status.version = self._vs_version or state.bot_settings.get(server.version_key) or ""
        return status

    @staticmethod
    def _summary_line(server: GameServer, status: "ServerStatus | None") -> str:
        if status is None:
            return f"🔴 **{server.game}** — offline"
        parts = [f"{status.players}/{status.max_players} online" if server.joins_key else "online"]
        if status.version:
            parts.append(status.version)
        return f"🟢 **{server.game}** — " + " · ".join(parts)

    @commands.group(name="gameservers", aliases=["gameserver", "servers", "server"], invoke_without_command=True,
                    help="Show whether the Minecraft, V Rising and Vintage Story servers are up, with players and version")
    @requires_perm
    async def cmd_gameservers(self, ctx: commands.Context):
        lines = []
        if mc_mod.MC_SERVER_HOST:
            mc = await mc_mod.fetch_mc_status()
            lines.append(f"🟢 **Minecraft** — {mc.players}/{mc.max_players} online · {mc.version}"
                         if mc else "🔴 **Minecraft** — offline")
        statuses = await asyncio.gather(*(self._live(server) for server in self.servers))
        lines += [self._summary_line(server, status) for server, status in zip(self.servers, statuses)]
        if not lines:
            await ctx.send(embed=emb("🎮 Game Servers", "No game servers are configured.", C_GREY))
            return
        details = (["`!mc`"] if mc_mod.MC_SERVER_HOST else []) + [f"`!{server.key}`" for server in self.servers]
        lines.append("\nDetails: " + ", ".join(details))
        await ctx.send(embed=emb("🎮 Game Servers", "\n".join(lines), C_GREEN))

    @cmd_gameservers.command(name="minecraft", aliases=["mc"], help="Show the Minecraft server's status (same as !mc)")
    @requires_perm
    async def cmd_minecraft(self, ctx: commands.Context):
        await forward(ctx, self.bot.get_cog("MinecraftCog").cmd_mc, invoked_with="mc")

    @cmd_gameservers.command(name="vrising", help="Show the V Rising server's status: players, version and uptime")
    @requires_perm
    async def cmd_vrising(self, ctx: commands.Context):
        await self._show(ctx, "vrising")

    @cmd_gameservers.command(name="vintagestory", aliases=["vintage"], help="Show the Vintage Story server's status: online and version")
    @requires_perm
    async def cmd_vintagestory(self, ctx: commands.Context):
        await self._show(ctx, "vintagestory")

    async def _show(self, ctx: commands.Context, key: str):
        game, env = GAMES[key]
        server = self._server(key)
        if server is None:
            await ctx.send(embed=emb(f"🎮 {game}", f"{game} integration isn't configured (set `{env}`).", C_GREY))
            return
        status = await self._live(server)
        if status is None:
            desc = f"The {game} server didn't respond."
            if key in self._last_seen_online:
                desc += f"\nLast seen online <t:{int(self._last_seen_online[key])}:R>."
            await ctx.send(embed=emb(f"🔴 {game} Server Offline", desc, C_RED))
            return
        embed = discord.Embed(title=f"🟢 {game} Server Status", color=C_GREEN)
        if server.joins_key:
            embed.add_field(name="Players", value=f"{status.players}/{status.max_players}")
        embed.add_field(name="Version", value=status.version or "?")
        if key in self._online_since:
            embed.add_field(name="Online since", value=f"<t:{int(self._online_since[key])}:R>")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(GameServersCog(bot))
