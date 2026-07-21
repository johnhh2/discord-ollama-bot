"""Minecraft Bedrock server status & monitor.

Talks to the itzg/minecraft-bedrock-server container over the same public UDP
port players use (RakNet "unconnected ping" via mcstatus). MC_SERVER_HOST is
the server's EXTERNAL address so latency reflects the internet-facing route.
The Bedrock pong carries player *counts* only, never names — so the monitor
posts anonymous "a player joined — 3/10" notices, not per-gamertag events.
Count deltas also feed the persistent player stats (mc_player_events +
mc_daily_player_stats, migration 0041): joins/leaves, daily peak concurrent,
and accumulated player-seconds, all count-based approximations. Named
join/leave and console commands would need docker-socket access
(deliberately not mounted).
"""
import asyncio
import collections
import dataclasses
import logging
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
)
from src.helpers import emb, C_GREEN, C_RED, C_GREY
from src.guild_config import get_guild_cfg
from src.permissions import requires_perm

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
    "came_online", "went_offline", "count_up", "count_down".
    """
    if status is None:
        streak = prev.fail_streak + 1
        if prev.online and streak >= MC_OFFLINE_AFTER_FAILURES:
            return MonitorState(online=False, count=0, fail_streak=streak), ["went_offline"]
        # Not enough failures yet, or we never saw it up (don't alert on a
        # server that was already down when the bot booted).
        return MonitorState(online=prev.online, count=prev.count, fail_streak=streak), []

    events = []
    if prev.online is False:
        events.append("came_online")
    elif prev.online is True:
        if status.players > prev.count:
            events.append("count_up")
        elif status.players < prev.count:
            events.append("count_down")
    # prev.online is None → first confirmed sighting; baseline silently.
    return MonitorState(online=True, count=status.players, fail_streak=0), events


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

    def cog_unload(self):
        status_manager.unregister("minecraft")
        if MC_SERVER_HOST:
            self.mc_monitor.cancel()

    # ── !mc ───────────────────────────────────────────────────────────────────
    @commands.command(name="mc", aliases=["minecraft", "mcstatus"])
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
            # Label the real coverage when the window is still filling
            # (samples only live in memory, so a reboot restarts it).
            if now - window_start < MC_UPTIME_WINDOW_SECS - 6 * 3_600:
                value = f"{pct:.1f}% since <t:{int(window_start)}:R>"
            else:
                value = f"{pct:.1f}% (last 7 days)"
            embed.add_field(name="Uptime", value=value)
        await ctx.send(embed=embed)

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
        return payloads

    async def _announce(self, payloads: list[dict]):
        for guild in self.bot.guilds:
            channel_id = get_guild_cfg(guild.id).get("minecraft_channel")
            if not channel_id:
                continue
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                for payload in payloads:
                    await channel.send(**payload)
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
