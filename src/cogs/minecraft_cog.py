"""Minecraft Bedrock server status & monitor.

Talks to the itzg/minecraft-bedrock-server container over the same public UDP
port players use (RakNet "unconnected ping" via mcstatus). MC_SERVER_HOST is
the server's EXTERNAL address so latency reflects the internet-facing route.
The Bedrock pong carries player *counts* only, never names — so the monitor
posts anonymous "a player joined — 3/10" notices, not per-gamertag events.
Count deltas also feed the persistent player stats (mc_player_events +
mc_daily_player_stats, migration 0041): joins/leaves, daily peak concurrent,
and accumulated player-seconds, all count-based approximations. The pong
also carries the server version: a change between two pongs posts a "server
updated" alert, with the last version seen persisted (mc_server_versions,
migration 0068) so the comparison survives the bot restarting alongside the
server. Named join/leave notices would need the console log; see below.

The block shop (`!mc shop` and friends) is the one path into the server
itself: it runs `give` / `clear` / `list` / `tellraw` over the itzg image's
SSH remote console (src/mc_console.py — no docker socket) and keeps its own
currency, 🟫 blocks, earned only by selling to it (catalog and rules in
src/mc_shop.py). Off per server until `!settings minecraft-shop on`, and
closed everywhere until MC_CONSOLE_HOST / MC_CONSOLE_PASSWORD are set.
"""
import asyncio
import collections
import dataclasses
import logging
import secrets
import time

import discord
from discord.ext import commands, tasks
from mcstatus import BedrockServer

from src import state, status_manager
# Accessed as attributes (not `from`-imported) so the test suite's stubs on
# the persistence package reach the calls here.
import src.persistence as persistence
from src.config import (
    MC_SERVER_HOST, MC_SERVER_PORT, MC_POLL_SECONDS, MC_SERVER_SHOW_IP,
    MC_CONSOLE_HOST, MC_CONSOLE_PORT, MC_CONSOLE_PASSWORD,
)
from src.helpers import emb, parse_int_amount, C_GREEN, C_RED, C_GREY, C_BLUE, C_ORANGE, C_PURPLE
from src.guild_config import get_guild_cfg
from src.mc_console import McConsole, ConsoleError, ConsoleRefused
from src.mc_shop import (
    BLOCK, CATEGORIES, CATEGORY_LABELS, MAX_TRADE_COUNT, ShopItem,
    find_item, fmt_blocks, items_in, match_category,
)
from src.permissions import requires_perm
from src.settings_views import Field, open_form

logger = logging.getLogger(__name__)

MC_STATUS_TIMEOUT_SECS = 5.0
# Consecutive failed pings before the monitor declares the server offline.
# One lost UDP packet must not produce an offline/online alert pair.
MC_OFFLINE_AFTER_FAILURES = 2
# Rolling windows for the !mc derived stats. Samples are persisted per poll
# (mc_ping_samples, migration 0035) and restored on boot, so the windows
# survive restarts; the uptime field still labels its actual coverage when
# the table holds less than a full week.
MC_UPTIME_WINDOW_SECS = 7 * 86_400
MC_AVG_PING_WINDOW_SECS = 3_600
# Prune the persisted samples once an hour rather than on every poll.
MC_PRUNE_EVERY_TICKS = 3_600 // max(MC_POLL_SECONDS, 1)
# Retention for the long-term stats tables (mc_player_events /
# mc_daily_player_stats, migration 0041; mc_daily_ping_stats, migration
# 0042) — ~10 years, matching GRAPH_HISTORY_RETENTION_DAYS in
# src/economy.py.
MC_STATS_RETENTION_DAYS = 3650
# Cap the per-poll playtime accrual so a long bot outage doesn't credit the
# whole gap to whoever happens to be online at the next poll.
MC_PLAYTIME_MAX_GAP_SECS = 3 * MC_POLL_SECONDS
# A `!mc link` code is read off the in-game screen; ten minutes is plenty.
LINK_CODE_TTL_SECS = 600
# Bedrock gamertags: 1–16 characters on Xbox, up to 32 with a suffix on
# other platforms; the column is VARCHAR(32).
GAMERTAG_MAX_LEN = 32


@dataclasses.dataclass
class McSample:
    """One monitor poll result: reachable or not, and the measured ping."""
    ts: float
    online: bool
    latency_ms: "float | None"


def _uptime_pct(samples, now: float) -> "tuple[float, float] | None":
    """Fraction of monitor polls that answered over the last week.

    Returns (pct, window_start_ts) or None with no samples. window_start is
    the older of (now - 7d, first sample) so callers can label short coverage
    honestly after a reboot.
    """
    cutoff = now - MC_UPTIME_WINDOW_SECS
    window = [s for s in samples if s.ts >= cutoff]
    if not window:
        return None
    up = sum(1 for s in window if s.online)
    return 100.0 * up / len(window), window[0].ts


def _avg_ping_ms(samples, now: float) -> "float | None":
    """Mean ping of successful polls over the last hour, or None."""
    cutoff = now - MC_AVG_PING_WINDOW_SECS
    pings = [s.latency_ms for s in samples
             if s.ts >= cutoff and s.online and s.latency_ms is not None]
    if not pings:
        return None
    return sum(pings) / len(pings)


@dataclasses.dataclass
class McStatus:
    players: int
    max_players: int
    latency_ms: int
    version: str
    motd: str
    gamemode: str
    map_name: str


@dataclasses.dataclass
class MonitorState:
    # online is tri-state: None = no confirmed baseline yet (bot just booted),
    # so the first observation never fires an alert — only changes do.
    online: bool | None = None
    count: int = 0
    fail_streak: int = 0
    # Last version string a pong carried. Kept across offline stretches (an
    # update is a restart, so the new version first shows up on the
    # up-transition) and seeded from mc_server_versions at boot, so the
    # update alert always compares against the last version actually seen.
    version: str | None = None


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric prefix of a Bedrock version string, for ordering: "1.21.51" →
    (1, 21, 51); anything unparsable contributes nothing ("" → ())."""
    parts = []
    for piece in version.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def _ct_date_iso(ts: float) -> str:
    """CT calendar date (midnight rollover, not the 5am gameplay boundary)
    for a Unix timestamp — the day key for mc_daily_player_stats."""
    import datetime
    from zoneinfo import ZoneInfo
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc
    ).astimezone(ZoneInfo("America/Chicago")).date().isoformat()


def _server_label() -> str:
    """How embeds refer to the server. The address is only exposed when the
    operator opts in via MC_SERVER_SHOW_IP."""
    if MC_SERVER_SHOW_IP:
        return f"**{MC_SERVER_HOST}:{MC_SERVER_PORT}**"
    return "The Minecraft server"


async def fetch_mc_status() -> "McStatus | None":
    """One status ping. Returns None if the server didn't answer.

    Module-level (not a method) so tests can monkeypatch
    src.cogs.minecraft_cog.fetch_mc_status.
    """
    try:
        server = BedrockServer.lookup(f"{MC_SERVER_HOST}:{MC_SERVER_PORT}")
        status = await asyncio.wait_for(
            server.async_status(), timeout=MC_STATUS_TIMEOUT_SECS
        )
    except Exception:
        return None
    return McStatus(
        players=status.players.online,
        max_players=status.players.max,
        latency_ms=round(status.latency),
        version=status.version.name or "",
        motd=status.motd.to_plain().strip(),
        gamemode=status.gamemode or "",
        map_name=status.map_name or "",
    )


def _mc_events(prev: MonitorState, status: "McStatus | None") -> tuple[MonitorState, list[str]]:
    """Fold one poll result into the monitor state.

    Pure — returns (next_state, events); the loop owns all I/O. Events:
    "came_online", "went_offline", "count_up", "count_down",
    "version_changed".
    """
    if status is None:
        streak = prev.fail_streak + 1
        if prev.online and streak >= MC_OFFLINE_AFTER_FAILURES:
            return MonitorState(online=False, count=0, fail_streak=streak,
                                version=prev.version), ["went_offline"]
        # Not enough failures yet, or we never saw it up (don't alert on a
        # server that was already down when the bot booted).
        return MonitorState(online=prev.online, count=prev.count,
                            fail_streak=streak, version=prev.version), []

    events = []
    # A version change is a restart, so it stands in for the up-transition
    # and any player-count change it caused: one "updated" embed (carrying
    # the current count), not an online / players-left notice plus it.
    # Compared independently of online-ness — with a baseline restored from
    # disk, the first pong after a boot can already announce an update. A
    # pong with an empty version keeps the last known one.
    if prev.version and status.version and status.version != prev.version:
        events.append("version_changed")
    elif prev.online is False:
        events.append("came_online")
    elif prev.online is True:
        if status.players > prev.count:
            events.append("count_up")
        elif status.players < prev.count:
            events.append("count_down")
    # prev.online is None → first confirmed sighting; baseline silently.
    return MonitorState(online=True, count=status.players, fail_streak=0,
                        version=status.version or prev.version), events


class MinecraftCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._monitor = MonitorState()
        self._last_seen_online: float | None = None
        # (players, max_players) from the most recent successful poll; feeds
        # the presence provider between polls.
        self._last_counts: tuple[int, int] | None = None
        # When the current online stretch began: set on the up-transition
        # (or first sighting after boot), cleared when confirmed offline.
        self._online_since: float | None = None
        # Rolling poll results for uptime % / avg ping; pruned to 7 days.
        # Restored from mc_ping_samples in the monitor's before_loop.
        self._samples: collections.deque[McSample] = collections.deque()
        self._ticks_until_prune = 0
        # Timestamp of the previous poll — the playtime accrual window for
        # mc_daily_player_stats (players × elapsed each successful poll).
        self._last_poll_ts: float | None = None
        # Registered even when unconfigured — status_text() returns None
        # until the monitor confirms players online, so the line stays hidden.
        status_manager.register("minecraft", self.status_text)
        if MC_SERVER_HOST:
            self.mc_monitor.start()
        # The block shop's console session; None keeps every shop command
        # answering "not configured". Tests swap in a fake.
        self.console: "McConsole | None" = (
            McConsole(MC_CONSOLE_HOST, MC_CONSOLE_PORT, MC_CONSOLE_PASSWORD)
            if MC_CONSOLE_HOST and MC_CONSOLE_PASSWORD else None
        )
        # uid → (gamertag as the server spells it, code, expires_at) for a
        # `!mc link` awaiting its `!mc verify`. In-memory: a reboot just
        # means asking for a new code.
        self._link_codes: dict[int, tuple[str, str, float]] = {}

    def cog_unload(self):
        status_manager.unregister("minecraft")
        if MC_SERVER_HOST:
            self.mc_monitor.cancel()
        if self.console is not None:
            asyncio.ensure_future(self.console.close())

    # ── !mc ───────────────────────────────────────────────────────────────────
    @commands.group(name="mc", aliases=["minecraft", "mcstatus"], invoke_without_command=True,
                    help="Show the Minecraft server's status: players, ping, version and uptime")
    @requires_perm
    async def cmd_mc(self, ctx: commands.Context):
        if not MC_SERVER_HOST:
            await ctx.send(embed=emb(
                "⛏️ Minecraft",
                "Minecraft integration isn't configured (set `MC_SERVER_HOST`).",
                C_GREY,
            ))
            return

        status = await fetch_mc_status()
        if status is None:
            desc = f"{_server_label()} didn't respond."
            if self._last_seen_online:
                desc += f"\nLast seen online <t:{int(self._last_seen_online)}:R>."
            await ctx.send(embed=emb("🔴 Server Offline", desc, C_RED))
            return

        self._last_seen_online = time.time()
        embed = discord.Embed(title="🟢 Minecraft Server Status", color=C_GREEN)
        if MC_SERVER_SHOW_IP:
            embed.description = f"**{MC_SERVER_HOST}:{MC_SERVER_PORT}**"
        embed.add_field(name="Players", value=f"{status.players}/{status.max_players}")
        embed.add_field(name="Ping", value=f"{status.latency_ms} ms")
        embed.add_field(name="Version", value=status.version or "?")
        # "World" shows the server name (Bedrock's MOTD line) — the level
        # name in map_name is usually the default "Bedrock level" noise.
        if status.motd:
            embed.add_field(name="World", value=status.motd, inline=False)
        if status.gamemode:
            embed.add_field(name="Gamemode", value=status.gamemode)

        # Monitor-derived stats; absent until the poll loop has data.
        now = time.time()
        if self._online_since:
            embed.add_field(name="Online since", value=f"<t:{int(self._online_since)}:R>")
        avg = _avg_ping_ms(self._samples, now)
        if avg is not None:
            embed.add_field(name="Avg ping (1h)", value=f"{avg:.0f} ms")
        uptime = _uptime_pct(self._samples, now)
        if uptime is not None:
            pct, window_start = uptime
            # Label the real coverage while the window is still filling
            # (mc_ping_samples covers under 7 days, with 6h of slack).
            if now - window_start < MC_UPTIME_WINDOW_SECS - 6 * 3_600:
                value = f"{pct:.1f}% since <t:{int(window_start)}:R>"
            else:
                value = f"{pct:.1f}% (last 7 days)"
            embed.add_field(name="Uptime", value=value)
        await ctx.send(embed=embed)

    # ── Block shop: gates and helpers ─────────────────────────────────────────
    def _shop_refusal(self, ctx: commands.Context) -> "discord.Embed | None":
        """Why the shop can't serve this command here, or None. The switch
        is per server and off by default; DMs have no switch to flip, so
        the shop stays shut there too."""
        if self.console is None:
            return emb("⛏️ Block Shop",
                       "The block shop isn't configured (set `MC_CONSOLE_HOST` and `MC_CONSOLE_PASSWORD`).",
                       C_GREY)
        if ctx.guild is None:
            return emb("⛏️ Block Shop", "The block shop works in a server that has it switched on, not in DMs.", C_GREY)
        if not get_guild_cfg(ctx.guild.id).get("mc_shop"):
            return emb("🚫 Turned Off",
                       "The Minecraft block shop is off in this server. "
                       "An admin can switch it on with `!settings minecraft-shop on`.",
                       C_GREY)
        return None

    @staticmethod
    def _player(uid: int) -> dict:
        return state.mc_players.setdefault(int(uid), {"gamertag": None, "blocks": 0, "linked_at": None})

    @staticmethod
    def _holder_of(gamertag: str) -> "int | None":
        """The Discord user a gamertag is linked to, case-insensitively."""
        wanted = gamertag.lower()
        for uid, row in state.mc_players.items():
            if (row.get("gamertag") or "").lower() == wanted:
                return uid
        return None

    async def _linked_gamertag(self, ctx: commands.Context) -> "str | None":
        gamertag = self._player(ctx.author.id).get("gamertag")
        if not gamertag:
            await ctx.send(embed=emb(
                "⛏️ Block Shop",
                "Link your gamertag first: join the server, then `!mc link <gamertag>`.",
                C_GREY,
            ))
        return gamertag

    @staticmethod
    def _parse_trade(args: tuple, *, allow_all: bool) -> "tuple[ShopItem | None, int, str | None]":
        """`<item> [count]` or `[count] <item>` → (item, count, error).
        A count is a number (`64`, `1k`) or, for selling, `all`."""
        tokens = [t for t in args if t.strip()]
        count = 1

        def _count(tok: str) -> "int | None":
            if allow_all and tok.lower() == "all":
                return MAX_TRADE_COUNT
            return parse_int_amount(tok)

        if len(tokens) >= 2 and _count(tokens[-1]) is not None:
            count = _count(tokens.pop())
        elif len(tokens) >= 2 and _count(tokens[0]) is not None:
            count = _count(tokens.pop(0))
        if not tokens:
            return None, 0, "Name an item — see `!mc shop`."
        item = find_item(" ".join(tokens))
        if item is None:
            return None, 0, f"I don't know **{' '.join(tokens)}** — see `!mc shop` for what's listed."
        if count < 1:
            return None, 0, "The count must be at least 1."
        return item, min(count, MAX_TRADE_COUNT), None

    async def _trade_form(self, ctx: commands.Context, *, verb: str) -> "tuple | None":
        """The bare `!mc buy` / `!mc sell` prompt: an item and a count."""
        values = await open_form(
            ctx,
            title=f"⛏️ {verb.title()} blocks",
            description=f"Usage: `!mc {verb} <item> [count]`\n`!mc shop` lists the items and prices.",
            fields=(
                Field("item", "Item", placeholder="stone bricks", max_length=64),
                Field("count", "How many", default="64", max_length=8,
                      description="a number, or `all` to sell everything you carry" if verb == "sell" else None),
            ),
        )
        if not values:
            return None
        return (values["item"], values["count"] or "1")

    @staticmethod
    def _refused_text(exc: Exception) -> str:
        line = str(exc)
        if line.startswith("No targets matched"):
            return "You're not on the server right now — join it, then try again."
        return f"The server refused: `{line}`"

    async def _report_console_failure(self, ctx: commands.Context, title: str, exc: Exception):
        if isinstance(exc, ConsoleRefused):
            await ctx.send(embed=emb(title, self._refused_text(exc), C_RED))
        else:
            logger.warning("[mc shop] console unavailable: %s", exc)
            await ctx.send(embed=emb(title, "I couldn't reach the server console — try again in a minute.", C_RED))

    # ── !mc link / verify / unlink ────────────────────────────────────────────
    @cmd_mc.command(name="link", help="Link your Minecraft gamertag to your Discord account for the block shop — join the server first, a code is sent to you in-game",
                    usage="<gamertag>")
    async def cmd_mc_link(self, ctx: commands.Context, *gamertag_words: str):
        refusal = self._shop_refusal(ctx)
        if refusal is not None:
            await ctx.send(embed=refusal)
            return
        gamertag = " ".join(gamertag_words).strip()
        if not gamertag:
            await ctx.send(embed=emb("⛏️ Link Gamertag", "Usage: `!mc link <gamertag>` — join the server first, then run it.", C_GREY))
            return
        if len(gamertag) > GAMERTAG_MAX_LEN:
            await ctx.send(embed=emb("⛏️ Link Gamertag", "That doesn't look like a gamertag.", C_RED))
            return
        if self._player(ctx.author.id).get("gamertag"):
            await ctx.send(embed=emb(
                "⛏️ Link Gamertag",
                f"You're already linked as **{self._player(ctx.author.id)['gamertag']}**. `!mc unlink` first to change it.",
                C_GREY,
            ))
            return
        holder = self._holder_of(gamertag)
        if holder is not None and holder != ctx.author.id:
            await ctx.send(embed=emb("⛏️ Link Gamertag", f"**{gamertag}** is already linked to <@{holder}>.", C_RED))
            return
        try:
            online = await self.console.online_players()
            actual = next((n for n in online if n.lower() == gamertag.lower()), None)
            if actual is None:
                await ctx.send(embed=emb(
                    "⛏️ Link Gamertag",
                    f"**{gamertag}** isn't on the server right now. Join it, then run `!mc link {gamertag}` again.",
                    C_RED,
                ))
                return
            code = secrets.token_hex(3).upper()
            # Registered before the message is sent so the code is valid the
            # moment the player can read it.
            self._link_codes[ctx.author.id] = (actual, code, time.time() + LINK_CODE_TTL_SECS)
            await self.console.tell(actual, f"Discord link code: {code} - run !mc verify {code} in Discord (expires in 10 minutes)")
        except (ConsoleRefused, ConsoleError) as exc:
            self._link_codes.pop(ctx.author.id, None)
            await self._report_console_failure(ctx, "⛏️ Link Gamertag", exc)
            return
        await ctx.send(embed=emb(
            "⛏️ Link Gamertag",
            f"I've sent **{actual}** a code in-game. Run `!mc verify <code>` here within 10 minutes.",
            C_BLUE,
        ))

    @cmd_mc.command(name="verify", help="Finish linking your gamertag with the code that was sent to you in-game",
                    usage="<code>")
    async def cmd_mc_verify(self, ctx: commands.Context, code: str = ""):
        refusal = self._shop_refusal(ctx)
        if refusal is not None:
            await ctx.send(embed=refusal)
            return
        pending = self._link_codes.get(ctx.author.id)
        if pending is None or pending[2] < time.time():
            self._link_codes.pop(ctx.author.id, None)
            await ctx.send(embed=emb("⛏️ Link Gamertag", "No code is waiting for you — start with `!mc link <gamertag>`.", C_GREY))
            return
        gamertag, expected, _ = pending
        if code.strip().upper() != expected:
            await ctx.send(embed=emb("⛏️ Link Gamertag", "That's not the code — check the message in-game.", C_RED))
            return
        # Claimed synchronously (no await since the checks) — a second
        # `!mc verify` can't link the same gamertag twice.
        self._link_codes.pop(ctx.author.id, None)
        holder = self._holder_of(gamertag)
        if holder is not None and holder != ctx.author.id:
            await ctx.send(embed=emb("⛏️ Link Gamertag", f"**{gamertag}** was linked to <@{holder}> in the meantime.", C_RED))
            return
        player = self._player(ctx.author.id)
        player["gamertag"] = gamertag
        player["linked_at"] = int(time.time())
        await persistence.save_mc_player(ctx.author.id)
        await ctx.send(embed=emb(
            "⛏️ Gamertag Linked",
            f"**{gamertag}** is now yours. Sell with `!mc sell`, buy with `!mc buy`; `!mc shop` has the prices.",
            C_GREEN,
        ))

    @cmd_mc.command(name="unlink", help="Unlink your Minecraft gamertag (your 🟫 blocks are kept)")
    async def cmd_mc_unlink(self, ctx: commands.Context):
        player = self._player(ctx.author.id)
        if not player.get("gamertag"):
            await ctx.send(embed=emb("⛏️ Unlink Gamertag", "You don't have a gamertag linked.", C_GREY))
            return
        was = player["gamertag"]
        player["gamertag"] = None
        player["linked_at"] = None
        await persistence.save_mc_player(ctx.author.id)
        await ctx.send(embed=emb("⛏️ Unlink Gamertag", f"**{was}** is no longer linked. Your {fmt_blocks(player['blocks'])} stay with you.", C_GREEN))

    # ── !mc blocks ────────────────────────────────────────────────────────────
    @cmd_mc.command(name="blocks", aliases=["bal", "purse"], help="Show your 🟫 blocks — the block shop's currency, earned only by selling to it",
                    usage="[@user]")
    async def cmd_mc_blocks(self, ctx: commands.Context, target: discord.Member = None):
        who = target or ctx.author
        player = state.mc_players.get(who.id) or {"gamertag": None, "blocks": 0}
        lines = [f"**{fmt_blocks(player['blocks'])}**"]
        if player.get("gamertag"):
            lines.append(f"Gamertag: **{player['gamertag']}**")
        elif who.id == ctx.author.id:
            lines.append("No gamertag linked — `!mc link <gamertag>`.")
        title = "⛏️ Your Blocks" if who.id == ctx.author.id else f"⛏️ {who.display_name}'s Blocks"
        await ctx.send(embed=emb(title, "\n".join(lines), C_PURPLE))

    # ── !mc shop ──────────────────────────────────────────────────────────────
    @cmd_mc.command(name="shop", aliases=["store", "prices"], help="The block shop's catalog: what it sells and what it pays, in 🟫 blocks",
                    usage="[category]")
    async def cmd_mc_shop(self, ctx: commands.Context, *category_words: str):
        refusal = self._shop_refusal(ctx)
        if refusal is not None:
            await ctx.send(embed=refusal)
            return
        query = " ".join(category_words).strip()
        if not query:
            await ctx.send(embed=self._shop_overview_embed())
            return
        key = match_category(query)
        if key is None:
            item = find_item(query)
            if item is None:
                await ctx.send(embed=emb("⛏️ Block Shop", f"No category or item called **{query}**. Bare `!mc shop` lists the categories.", C_GREY))
                return
            await ctx.send(embed=emb("⛏️ Block Shop", self._item_line(item), C_PURPLE))
            return
        await ctx.send(embed=self._category_embed(key))

    @staticmethod
    def _item_line(item: ShopItem) -> str:
        buy = f"buy {fmt_blocks(item.buy)}" if item.buy is not None else "not for sale"
        sell = f"sells for {fmt_blocks(item.sell)}" if item.sell is not None else "not bought"
        return f"**{item.name}** · {buy} · {sell}"

    def _shop_overview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="⛏️ Block Shop",
            description=(
                f"Prices are in {BLOCK} **blocks**, earned only by selling here. "
                "`!mc shop <category>` for the prices, `!mc sell <item> [count|all]`, `!mc buy <item> [count]`.\n"
                "Bought items land in your inventory on the server; sold ones are taken from it — be online."
            ),
            color=C_PURPLE,
        )
        for key, label in CATEGORIES:
            items = items_in(key)
            buyable = sum(1 for i in items if i.buy is not None)
            sellable = sum(1 for i in items if i.sell is not None)
            if key == "minerals":
                what = f"{sellable} items · sell only"
            elif buyable and not sellable:
                what = f"{buyable} blocks · buy only"
            else:
                what = f"{buyable} blocks · {sellable} bought back"
            embed.add_field(name=label, value=f"`!mc shop {key}` — {what}", inline=True)
        return embed

    def _category_embed(self, key: str) -> discord.Embed:
        embed = discord.Embed(title=f"⛏️ Block Shop — {CATEGORY_LABELS[key]}", color=C_PURPLE)
        lines = [self._item_line(item) for item in items_in(key)]
        # Embed fields hold 1024 characters; the coloured sets run past it.
        chunk, size, first = [], 0, True
        for line in lines + [None]:
            if line is None or size + len(line) + 1 > 1000:
                embed.add_field(name="Items" if first else "…", value="\n".join(chunk), inline=False)
                chunk, size, first = [], 0, False
                if line is None:
                    break
            chunk.append(line)
            size += len(line) + 1
        return embed

    # ── !mc buy / sell ────────────────────────────────────────────────────────
    @cmd_mc.command(name="buy", help="Buy building blocks from the block shop with 🟫 blocks — they're given to you in-game, so be on the server",
                    usage="<item> [count]")
    async def cmd_mc_buy(self, ctx: commands.Context, *args: str):
        refusal = self._shop_refusal(ctx)
        if refusal is not None:
            await ctx.send(embed=refusal)
            return
        if not args:
            args = await self._trade_form(ctx, verb="buy")
            if args is None:
                return
        item, count, error = self._parse_trade(args, allow_all=False)
        if error:
            await ctx.send(embed=emb("⛏️ Buy", error, C_RED))
            return
        if item.buy is None:
            await ctx.send(embed=emb("⛏️ Buy", f"**{item.name}** isn't for sale — the shop only buys it ({self._item_line(item)}).", C_GREY))
            return
        gamertag = await self._linked_gamertag(ctx)
        if not gamertag:
            return
        player = self._player(ctx.author.id)
        cost = item.buy * count
        if player["blocks"] < cost:
            await ctx.send(embed=emb(
                "⛏️ Buy",
                f"{count:,} × **{item.name}** costs {fmt_blocks(cost)}; you have {fmt_blocks(player['blocks'])}.",
                C_RED,
            ))
            return
        # Charge before the console call (see CLAUDE.md: Concurrency) and
        # refund if the server doesn't hand the items over.
        player["blocks"] -= cost
        await persistence.save_mc_player(ctx.author.id)
        try:
            given = await self.console.give(gamertag, item.id, count)
        except (ConsoleRefused, ConsoleError) as exc:
            player["blocks"] += cost
            await persistence.save_mc_player(ctx.author.id)
            await self._report_console_failure(ctx, "⛏️ Buy", exc)
            return
        await persistence.log_mc_trade(
            ts=int(time.time()), user_id=ctx.author.id, guild_id=ctx.guild.id, gamertag=gamertag,
            kind="buy", item_id=item.id, count=given, blocks=cost,
        )
        await ctx.send(embed=emb(
            "⛏️ Bought",
            f"**{given:,} × {item.name}** given to **{gamertag}** for {fmt_blocks(cost)}.\n"
            f"You have {fmt_blocks(player['blocks'])} left.",
            C_GREEN,
        ))

    @cmd_mc.command(name="sell", help="Sell ores or building blocks from your in-game inventory to the block shop for 🟫 blocks — be on the server",
                    usage="<item> [count|all]")
    async def cmd_mc_sell(self, ctx: commands.Context, *args: str):
        refusal = self._shop_refusal(ctx)
        if refusal is not None:
            await ctx.send(embed=refusal)
            return
        if not args:
            args = await self._trade_form(ctx, verb="sell")
            if args is None:
                return
        item, count, error = self._parse_trade(args, allow_all=True)
        if error:
            await ctx.send(embed=emb("⛏️ Sell", error, C_RED))
            return
        if item.sell is None:
            await ctx.send(embed=emb("⛏️ Sell", f"The shop doesn't buy **{item.name}** ({self._item_line(item)}).", C_GREY))
            return
        gamertag = await self._linked_gamertag(ctx)
        if not gamertag:
            return
        # The console takes the items first; the purse is credited for
        # exactly what came out, so two sells racing can't double-pay.
        try:
            removed = await self.console.clear(gamertag, item.id, count)
        except (ConsoleRefused, ConsoleError) as exc:
            await self._report_console_failure(ctx, "⛏️ Sell", exc)
            return
        if removed <= 0:
            await ctx.send(embed=emb("⛏️ Sell", f"You don't have any **{item.name}** on you.", C_GREY))
            return
        earned = item.sell * removed
        player = self._player(ctx.author.id)
        player["blocks"] += earned
        await persistence.save_mc_player(ctx.author.id)
        await persistence.log_mc_trade(
            ts=int(time.time()), user_id=ctx.author.id, guild_id=ctx.guild.id, gamertag=gamertag,
            kind="sell", item_id=item.id, count=removed, blocks=earned,
        )
        short = f" (you had {removed:,}, not {count:,})" if removed < count and count != MAX_TRADE_COUNT else ""
        await ctx.send(embed=emb(
            "⛏️ Sold",
            f"**{removed:,} × {item.name}** taken from **{gamertag}** for {fmt_blocks(earned)}{short}.\n"
            f"You now have {fmt_blocks(player['blocks'])}.",
            C_GREEN,
        ))

    # ── Monitor loop ──────────────────────────────────────────────────────────
    @tasks.loop(seconds=MC_POLL_SECONDS)
    async def mc_monitor(self):
        status = await fetch_mc_status()
        prev = self._monitor
        self._monitor, events = _mc_events(prev, status)
        now = time.time()
        if status is not None:
            self._last_seen_online = now
            self._last_counts = (status.players, status.max_players)

        # Rolling stats window (uptime % / avg ping for !mc).
        sample = McSample(
            ts=now, online=status is not None,
            latency_ms=float(status.latency_ms) if status else None,
        )
        self._samples.append(sample)
        cutoff = now - MC_UPTIME_WINDOW_SECS
        while self._samples and self._samples[0].ts < cutoff:
            self._samples.popleft()

        # Persist the sample so the stats window and graph survive restarts.
        try:
            await persistence.save_mc_ping_sample(
                int(sample.ts), sample.online, sample.latency_ms)
            if self._ticks_until_prune <= 0:
                self._ticks_until_prune = MC_PRUNE_EVERY_TICKS
                await self._rollup_daily_ping(now)
                await persistence.prune_mc_ping_samples(int(cutoff))
                retention_cutoff = now - MC_STATS_RETENTION_DAYS * 86_400
                await persistence.prune_mc_player_events(int(retention_cutoff))
                await persistence.prune_mc_daily_player_stats(
                    _ct_date_iso(retention_cutoff))
                await persistence.prune_mc_daily_ping_stats(
                    _ct_date_iso(retention_cutoff))
            self._ticks_until_prune -= 1
        except Exception:
            logger.exception("[minecraft] failed to persist ping sample")

        # Player join/leave events + the daily rollup (migration 0041).
        try:
            await self._track_player_stats(prev, status, now)
        except Exception:
            logger.exception("[minecraft] failed to persist player stats")

        # Version baseline for the update alert (migration 0068): the first
        # version seen, then every change.
        if status is not None and status.version and status.version != prev.version:
            try:
                await persistence.record_mc_server_version(int(now), status.version)
            except Exception:
                logger.exception("[minecraft] failed to persist server version")

        # Track when the current online stretch began. `not prev.online`
        # covers both a real up-transition (False) and first sighting (None);
        # a value restored from disk in before_loop is kept.
        if self._monitor.online and not prev.online:
            if self._online_since is None:
                self._online_since = now
        elif self._monitor.online is False:
            self._online_since = None

        # Publish the sample for the graph scheduler's bot-stats snapshot.
        # During the offline debounce window online stays True — that's fine,
        # the graph treats "up but no ping this sample" as no data point.
        state.mc_last_online = self._monitor.online
        state.mc_last_ping_ms = float(status.latency_ms) if status else None

        if events:
            await self._announce(self._event_payloads(events, prev, status))

    async def _rollup_daily_ping(self, now: float):
        """Fold completed CT days of mc_ping_samples into mc_daily_ping_stats
        (avg/min/max, downtime counted as 0 — the graph line's semantics).

        Runs on the hourly prune tick, before samples are pruned. Skips the
        ongoing day (still accumulating) and days that already have a row —
        a finished row is never recomputed, so later sample pruning can't
        degrade it. Because every completed day still inside the 7-day
        sample window is (re)considered, days missed during bot downtime
        backfill automatically.
        """
        today = _ct_date_iso(now)
        samples = await persistence.load_mc_ping_samples(
            int(now - MC_UPTIME_WINDOW_SECS))
        by_day: dict[str, list[float]] = {}
        for ts, online, latency in samples:
            day = _ct_date_iso(ts)
            if day >= today:
                continue
            by_day.setdefault(day, []).append(
                float(latency) if (online and latency) else 0.0)
        if not by_day:
            return
        done = {
            row[0]
            for row in await persistence.load_mc_daily_ping_stats(min(by_day))
        }
        for day in sorted(by_day):
            if day in done:
                continue
            ys = by_day[day]
            await persistence.save_mc_daily_ping_stats(
                day, sum(ys) / len(ys), min(ys), max(ys))

    async def _track_player_stats(self, prev: MonitorState,
                                  status: "McStatus | None", now: float):
        """Fold one poll into the persistent player stats (migration 0041).

        Joins/leaves are count deltas between consecutive successful polls
        (the Bedrock pong is anonymous). A baseline poll (prev.online None —
        bot just booted) records no event: players already on mid-session
        aren't "joins", and we can't know when they arrived. A server
        up-transition counts everyone present as joining (prev.count is 0).

        Playtime accrues as players × elapsed-since-last-poll on every
        successful poll, capped at MC_PLAYTIME_MAX_GAP_SECS so bot downtime
        isn't credited. The daily row upserts SQL-side (GREATEST/+=), so no
        state needs restoring on boot.
        """
        if status is None:
            # Down (or debouncing): no playtime accrues over this gap.
            self._last_poll_ts = now
            return
        joins = 0
        if prev.online is not None:
            delta = status.players - prev.count
            joins = max(0, delta)
            if delta != 0:
                await persistence.record_mc_player_event(
                    int(now), delta, status.players)
        elapsed = 0.0
        if self._last_poll_ts is not None:
            elapsed = min(max(now - self._last_poll_ts, 0.0),
                          MC_PLAYTIME_MAX_GAP_SECS)
        self._last_poll_ts = now
        await persistence.upsert_mc_daily_player_stats(
            _ct_date_iso(now), status.players, joins, status.players * elapsed)

    @mc_monitor.before_loop
    async def _before_monitor(self):
        await self.bot.wait_until_ready()
        await persistence.init_done.wait()
        try:
            await self._restore_samples()
        except Exception:
            logger.exception("[minecraft] failed to restore persisted samples")
        try:
            await self._restore_version()
        except Exception:
            logger.exception("[minecraft] failed to restore server version")

    async def _restore_samples(self):
        """Reload the 7-day stats window from mc_ping_samples after a boot."""
        if self._samples:
            return  # gateway reconnect, not a fresh boot
        now = time.time()
        rows = await persistence.load_mc_ping_samples(
            int(now - MC_UPTIME_WINDOW_SECS))
        for ts, online, latency in rows:
            self._samples.append(McSample(
                ts=float(ts), online=online,
                latency_ms=float(latency) if latency is not None else None,
            ))
        online_ts = [s.ts for s in self._samples if s.online]
        if online_ts:
            self._last_seen_online = online_ts[-1]
        # Resume the "online since" stretch only when the samples run right
        # up to this boot and end online — after a longer gap we can't know
        # the server stayed up while the bot was down.
        if (self._samples and self._samples[-1].online
                and now - self._samples[-1].ts < 3 * MC_POLL_SECONDS):
            run_start = None
            for s in reversed(self._samples):
                if not s.online:
                    break
                run_start = s.ts
            self._online_since = run_start

    async def _restore_version(self):
        """Seed the update-alert baseline from mc_server_versions.

        A server update is a restart, and when the whole stack restarts
        together the bot comes back with no version to compare against —
        without this the update that most likely just happened would
        baseline silently.
        """
        if self._monitor.version is not None:
            return  # gateway reconnect, not a fresh boot
        self._monitor.version = await persistence.load_mc_server_version()

    def _event_payloads(self, events: list[str], prev: MonitorState,
                        status: "McStatus | None") -> list[dict]:
        """Turn events into channel.send kwargs (colored embeds)."""
        payloads = []
        for ev in events:
            if ev == "came_online":
                payloads.append({"embed": emb(
                    "🟢 Minecraft Server Online",
                    f"{_server_label()} is back up — "
                    f"**{status.players}/{status.max_players}** online.",
                    C_GREEN,
                )})
            elif ev == "went_offline":
                desc = f"{_server_label()} stopped responding."
                if self._last_seen_online:
                    desc += f"\nLast seen online <t:{int(self._last_seen_online)}:R>."
                payloads.append({"embed": emb("🔴 Minecraft Server Offline", desc, C_RED)})
            elif ev == "count_up":
                delta = status.players - prev.count
                who = "A player" if delta == 1 else f"{delta} players"
                payloads.append({"embed": emb(
                    "🟢 Player Joined",
                    f"{who} joined the Minecraft server — "
                    f"**{status.players}/{status.max_players}** online",
                    C_GREEN,
                )})
            elif ev == "count_down":
                delta = prev.count - status.players
                who = "A player" if delta == 1 else f"{delta} players"
                payloads.append({"embed": emb(
                    "🔴 Player Left",
                    f"{who} left the Minecraft server — "
                    f"**{status.players}/{status.max_players}** online",
                    C_RED,
                )})
            elif ev == "version_changed":
                old, new = _version_key(prev.version), _version_key(status.version)
                rollback = bool(old and new and new < old)
                title = ("⬇️ Minecraft Server Rolled Back" if rollback
                         else "⬆️ Minecraft Server Updated")
                what = "rolled back" if rollback else "updated"
                # This embed replaces the "back online" one when the monitor
                # saw the server go down for the update.
                lead = "is back up," if prev.online is False else "was"
                payloads.append({"embed": emb(
                    title,
                    f"{_server_label()} {lead} {what}: "
                    f"**{prev.version}** → **{status.version}** — "
                    f"**{status.players}/{status.max_players}** online",
                    C_ORANGE if rollback else C_BLUE,
                )})
        return payloads

    async def _announce(self, payloads: list[dict]):
        for guild in self.bot.guilds:
            channel_id = get_guild_cfg(guild.id).get("minecraft_channel")
            if not channel_id:
                continue
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                for payload in payloads:
                    await channel.send(silent=True, **payload)
            except Exception:
                logger.exception("[minecraft] announce failed for guild %s", guild.id)

    def status_text(self) -> "str | None":
        """Presence line for the status manager rotation.

        Hidden (None) unless the monitor has the server confirmed online
        with at least one player. During the offline-debounce window (one
        dropped ping) the last known counts keep showing — the same grace
        the announce path gives.
        """
        if self._monitor.online is not True or self._last_counts is None:
            return None
        players, max_players = self._last_counts
        if players < 1:
            return None
        return f"⛏️ {players}/{max_players} on Minecraft"


async def setup(bot):
    await bot.add_cog(MinecraftCog(bot))
