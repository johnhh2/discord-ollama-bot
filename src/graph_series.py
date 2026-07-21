"""Graph-series registry powering combinable `!graph` subcommands.

Each subcommand (`balance`, `economy`, `commands`, `server`, `ai`, `memory`)
contributes a `SeriesSpec` declaring its aliases, group, and a build function
that returns the standardized `SeriesData` shape. The combiner picks rendering
style from (number-of-series, group):

  - 1 series, native style "line"  → overlaid lines (preserves multi-segment).
  - 1 series, native style "bar"   → stacked bar (segments stack within each bar).
  - N series, group "coins"        → overlaid lines (all segments).
  - N series, group "counts"       → grouped bars per day, each bar internally
                                     stacked from its segments.

`memory`, `ping`, and `minecraft` are each their own group of size 1;
combination with anything else is rejected.

Adding a new graph series: write a `build_series_xxx()` async function returning
`SeriesData`, then append a `SeriesSpec(...)` entry to `REGISTRY` below.
"""
from __future__ import annotations

import datetime
import time as _time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import discord

from src import state
from src.economy import (
    _ct_today_date, _calendar_today_date,  # re-exported for tests
    _current_bucket_ct, _bucket_start_dt,
    get_balance,
)
# Silence unused-import warnings — these names are part of this module's
# test-facing surface even though graph_series itself doesn't reference them.
__all_test_reexports__ = (_ct_today_date, _calendar_today_date)
from src.helpers import get_memory_mb
from src.persistence import (
    load_balance_history, load_bot_stats_history, load_command_usage_history,
    load_crime_history, load_gambling_history, load_levelup_history,
    load_mc_ping_samples, load_mc_daily_player_stats, load_mc_daily_ping_stats,
)


# ── Group identifiers ────────────────────────────────────────────────────────

GROUP_COINS = "coins"
GROUP_COUNTS = "counts"
GROUP_MB = "mb"
GROUP_XP = "xp"
GROUP_MS = "ms"
GROUP_MS_MC = "ms_mc"   # Minecraft ping; own group so it stays solo like memory/ping

_GROUP_LABEL = {
    GROUP_COINS: "coins",
    GROUP_COUNTS: "counts",
    GROUP_MB: "MB",
    GROUP_XP: "xp",
    GROUP_MS: "ms",
    GROUP_MS_MC: "ms",
}


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class Segment:
    """One coloured slice within a series (a single bar segment, or one line).

    `band_lower`/`band_upper` optionally carry a per-point min/max envelope
    (same length as `y_values`); line renders draw it as a faded band around
    the line instead of the usual fill-to-zero.
    """
    label: str
    color: str
    y_values: list[float]
    band_lower: Optional[list[float]] = None
    band_upper: Optional[list[float]] = None


@dataclass
class SeriesData:
    """Result of building one series. Multi-segment when the source produces
    more than one stripe (e.g. economy = wallets/savings/total).

    `x_points` are full datetime values (bucket-start timestamps in CT) so
    each calendar day produces up to 4 plotted points (one per 6h bucket).
    """
    title: str            # short identifier for legend prefixing in combined mode
    segments: list[Segment]
    x_points: list[datetime.datetime]
    native_style: str     # "line" or "bar"
    # Optional extras only used by ai in single-mode:
    extras: dict = field(default_factory=dict)


# A build function may take an optional discord.Member (only `balance` uses it).
BuildFn = Callable[..., Awaitable[SeriesData]]


@dataclass
class SeriesSpec:
    name: str
    aliases: tuple[str, ...]
    group: str
    build: BuildFn
    accepts_member: bool = False
    accepts_guild: bool = False   # build needs current guild context (e.g. levels)
    accepts_bot: bool = False     # build needs the Bot instance (e.g. ping's live point)
    y_unit_label: str = ""        # e.g. "🪙 Coins", "Count", "MB"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sorted_dates(history: dict, limit: int = 14) -> list[str]:
    return sorted(history.keys())[-limit:]


def _date_to_iso(d: datetime.date) -> str:
    return d.isoformat()


def _iter_points(history: dict, limit_days: int = 14):
    """Walk a {date_str: {bucket: payload}} history in chronological order,
    yielding (point_dt, payload) for each (date, bucket) pair. `point_dt` is
    the bucket's CT-start as a UTC-aware datetime — used directly as the
    matplotlib x position.
    """
    for date_str in _sorted_dates(history, limit=limit_days):
        by_bucket = history[date_str]
        if not isinstance(by_bucket, dict):
            continue
        for bucket in sorted(by_bucket.keys()):
            yield _bucket_start_dt(date_str, bucket), by_bucket[bucket]


def _live_now_point() -> datetime.datetime:
    """Bucket-start timestamp for the current 6h CT bucket."""
    return _bucket_start_dt(_ct_now_iso_date(), _current_bucket_ct())


def _ct_now_iso_date() -> str:
    """Calendar date in CT as ISO string. Inlined small helper so this
    module doesn't need to import _ct_now from economy just for one use.
    """
    from src.economy import _ct_now
    return _ct_now().date().isoformat()


def _ct_date_of_ts(ts: float) -> str:
    """CT calendar date (ISO) of a Unix timestamp — the day key used by the
    Minecraft daily rollup tables."""
    from zoneinfo import ZoneInfo
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc
    ).astimezone(ZoneInfo("America/Chicago")).date().isoformat()


# ── build_series for each registered command ─────────────────────────────────


async def build_series_balance(member: discord.Member) -> SeriesData:
    """Per-(date, bucket) wallet, savings, and total for `member`.

    Mirrors the segment shape of build_series_economy (Wallets/Savings/Total)
    but scoped to a single user. The renderer dashes the Total line as a
    summary, matching the existing economy graph's style.
    """
    from src.economy import get_savings_value

    history = await load_balance_history()
    uid_str = str(member.id)

    x_points: list[datetime.datetime] = []
    y_wallet: list[float] = []
    y_savings: list[float] = []
    for point_dt, snap_by_user in _iter_points(history):
        snap = snap_by_user.get(uid_str)
        if snap is not None:
            x_points.append(point_dt)
            y_wallet.append(snap["wallet"])
            y_savings.append(snap.get("savings", 0))

    # Append live "now" point for the current bucket if we don't already have it.
    now_point = _live_now_point()
    live_wallet = await get_balance(member.id)
    live_savings = int(await get_savings_value(member.id))
    if not x_points or x_points[-1] != now_point:
        x_points.append(now_point)
        y_wallet.append(live_wallet)
        y_savings.append(live_savings)

    y_total = [w + s for w, s in zip(y_wallet, y_savings)]
    return SeriesData(
        title=f"{member.display_name}'s Balance",
        segments=[
            Segment(label="Wallet",  color="#2ecc71", y_values=y_wallet),
            Segment(label="Savings", color="#9b59b6", y_values=y_savings),
            Segment(label="Total",   color="#f1c40f", y_values=y_total),
        ],
        x_points=x_points,
        native_style="line",
    )


async def build_series_crime(member: discord.Member) -> SeriesData:
    """Per-(date, bucket) totals of coins gained/lost via !steal and !mug.

    Each calendar day produces up to 4 plotted points (one per 6h CT bucket).
    Days/buckets where the user had no crime activity are skipped — no noisy
    zero line for inactive users.
    """
    history = await load_crime_history()
    # crime_history is keyed (guild_id, user) since migration 0018 — scope
    # the chart to crime committed in THIS member's guild.
    key = (member.guild.id, str(member.id))

    x_points: list[datetime.datetime] = []
    y_gained: list[float] = []
    y_lost: list[float] = []
    for point_dt, by_key in _iter_points(history):
        rec = by_key.get(key)
        if rec is None:
            continue
        x_points.append(point_dt)
        y_gained.append(rec.get("gained", 0))
        y_lost.append(rec.get("lost", 0))

    # Append the current bucket's live totals if we don't already have a row
    # for that bucket on disk.
    now_point = _live_now_point()
    live = state.crime_today_by_user.get(key)
    if live is not None and (not x_points or x_points[-1] != now_point):
        x_points.append(now_point)
        y_gained.append(live.get("gained", 0))
        y_lost.append(live.get("lost", 0))

    return SeriesData(
        title=f"{member.display_name}'s Crime",
        segments=[
            Segment(label="Gained", color="#2ecc71", y_values=y_gained),
            Segment(label="Lost",   color="#e74c3c", y_values=y_lost),
        ],
        x_points=x_points,
        native_style="line",
    )


async def build_series_gambling(member: discord.Member) -> SeriesData:
    """Per-(date, bucket) P/L from games and gambling commands.

    Tracks net wins/losses across slots, flip, blackjack, scratchoff, hangman,
    ttt, c4, race, lottery. Refunds and pushes are not recorded (net 0). The
    Net line is dashed, mirroring economy's Total line.
    """
    history = await load_gambling_history()
    # gambling_history is keyed (guild_id, user) since migration 0018 —
    # scope the chart to gambling done in THIS member's guild.
    key = (member.guild.id, str(member.id))

    x_points: list[datetime.datetime] = []
    y_gained: list[float] = []
    y_lost: list[float] = []
    for point_dt, by_key in _iter_points(history):
        rec = by_key.get(key)
        if rec is None:
            continue
        x_points.append(point_dt)
        y_gained.append(rec.get("gained", 0))
        y_lost.append(rec.get("lost", 0))

    now_point = _live_now_point()
    live = state.gambling_today_by_user.get(key)
    if live is not None and (not x_points or x_points[-1] != now_point):
        x_points.append(now_point)
        y_gained.append(live.get("gained", 0))
        y_lost.append(live.get("lost", 0))

    y_net = [g - l for g, l in zip(y_gained, y_lost)]

    return SeriesData(
        title=f"{member.display_name}'s Gambling",
        segments=[
            Segment(label="Gained", color="#2ecc71", y_values=y_gained),
            Segment(label="Lost",   color="#e74c3c", y_values=y_lost),
            Segment(label="Net",    color="#f1c40f", y_values=y_net),
        ],
        x_points=x_points,
        native_style="line",
    )


async def build_series_levels(member: discord.Member, guild_id: int) -> SeriesData:
    """Per-day count of level-ups for `member` within guild `guild_id`.

    A single XP grant can cross multiple level boundaries; the count records
    boundaries crossed, not events triggered. Days the user didn't level up
    are skipped (no noisy zero line for inactive users).
    """
    history = await load_levelup_history()
    uid_str = str(member.id)
    key = (int(guild_id), uid_str)

    x_points: list[datetime.datetime] = []
    y_count: list[float] = []
    for point_dt, by_key in _iter_points(history):
        count = by_key.get(key)
        if count is None or count == 0:
            continue
        x_points.append(point_dt)
        y_count.append(count)

    now_point = _live_now_point()
    live = state.levelups_today.get(key)
    if live and (not x_points or x_points[-1] != now_point):
        x_points.append(now_point)
        y_count.append(live)

    return SeriesData(
        title=f"{member.display_name}'s Level-ups",
        segments=[
            Segment(label="Level-ups", color="#9b59b6", y_values=y_count),
        ],
        x_points=x_points,
        native_style="bar",
    )


async def build_series_economy() -> SeriesData:
    history = await load_balance_history()

    x_points: list[datetime.datetime] = []
    y_wallet: list[float] = []
    y_savings: list[float] = []
    for point_dt, snap_by_user in _iter_points(history):
        x_points.append(point_dt)
        y_wallet.append(sum(u["wallet"] for u in snap_by_user.values()))
        y_savings.append(sum(u["savings"] for u in snap_by_user.values()))

    now_point = _live_now_point()
    now = _time.time()
    live_wallet = sum(u.get("balance", 0) for u in state.economy["users"].values())
    live_savings = int(sum(
        e["amount"] * (1.01 ** ((now - e["deposited_at"]) / 86400.0))
        for u in state.economy["users"].values()
        for e in u.get("savings", [])
    ))
    if not x_points or x_points[-1] != now_point:
        x_points.append(now_point)
        y_wallet.append(live_wallet)
        y_savings.append(live_savings)

    y_total = [w + s for w, s in zip(y_wallet, y_savings)]
    return SeriesData(
        title="Total Economy",
        segments=[
            Segment(label="Wallets", color="#2ecc71", y_values=y_wallet),
            Segment(label="Savings", color="#9b59b6", y_values=y_savings),
            Segment(label="Total",   color="#f1c40f", y_values=y_total),
        ],
        x_points=x_points,
        native_style="line",
    )


async def build_series_commands() -> SeriesData:
    history = await load_command_usage_history()

    x_points: list[datetime.datetime] = []
    per_point: list[dict] = []
    for point_dt, by_cog in _iter_points(history):
        x_points.append(point_dt)
        per_point.append(by_cog)

    # Append the live current-bucket data point.
    now_point = _live_now_point()
    live_now = dict(state.stats_commands_today_by_cog)
    if not x_points or x_points[-1] != now_point:
        x_points.append(now_point)
        per_point.append(live_now)

    # Union of cog names across the window, sorted by total volume desc.
    totals: dict[str, int] = {}
    for slot in per_point:
        for cog, count in slot.items():
            totals[cog] = totals.get(cog, 0) + count
    cogs = sorted(totals.keys(), key=lambda c: totals[c], reverse=True)

    def _label(cog_name: str) -> str:
        return cog_name[:-3] if cog_name.endswith("Cog") else cog_name

    # tab20 palette so even ~11 cogs stay distinguishable.
    import matplotlib.pyplot as _plt
    palette = _plt.cm.tab20.colors

    segments = [
        Segment(
            label=_label(cog),
            color=palette[i % len(palette)],
            y_values=[float(slot.get(cog, 0)) for slot in per_point],
        )
        for i, cog in enumerate(cogs)
    ]
    return SeriesData(
        title="Commands by Cog",
        segments=segments,
        x_points=x_points,
        native_style="bar",
    )


async def build_series_server() -> SeriesData:
    history = await load_bot_stats_history()
    x_points: list[datetime.datetime] = []
    y_messages: list[float] = []
    y_commands: list[float] = []
    for point_dt, snap in _iter_points(history):
        x_points.append(point_dt)
        y_messages.append(snap.get("messages", 0))
        y_commands.append(snap.get("commands", 0))

    now_point = _live_now_point()
    if not x_points or x_points[-1] != now_point:
        x_points.append(now_point)
        y_messages.append(state.stats_messages_today)
        y_commands.append(state.stats_commands_today)

    return SeriesData(
        title="Server Activity",
        segments=[
            Segment(label="Messages", color="#3498db", y_values=y_messages),
            Segment(label="Commands", color="#e67e22", y_values=y_commands),
        ],
        x_points=x_points,
        native_style="line",
    )


async def build_series_ai() -> SeriesData:
    history = await load_bot_stats_history()
    x_points: list[datetime.datetime] = []
    y_responses: list[float] = []
    ai_up_flags: list[Optional[bool]] = []
    for point_dt, snap in _iter_points(history):
        x_points.append(point_dt)
        y_responses.append(snap.get("ai_responses", 0))
        ai_up_flags.append(snap.get("ai_up", False))

    now_point = _live_now_point()
    if not x_points or x_points[-1] != now_point:
        x_points.append(now_point)
        y_responses.append(state.stats_ai_responses_today)
        ai_up_flags.append(None)

    return SeriesData(
        title="AI Activity",
        segments=[Segment(label="AI Responses", color="#1abc9c", y_values=y_responses)],
        x_points=x_points,
        native_style="bar",
        extras={"ai_up_flags": ai_up_flags},
    )


async def build_series_memory() -> SeriesData:
    history = await load_bot_stats_history()
    x_points: list[datetime.datetime] = []
    y_mem: list[float] = []
    for point_dt, snap in _iter_points(history):
        mb = snap.get("memory_mb", 0)
        if mb > 0:
            x_points.append(point_dt)
            y_mem.append(mb)

    now_point = _live_now_point()
    live_mem = get_memory_mb()
    if live_mem > 0 and (not x_points or x_points[-1] != now_point):
        x_points.append(now_point)
        y_mem.append(live_mem)

    return SeriesData(
        title="Bot Memory",
        segments=[Segment(label="RSS Memory", color="#e74c3c", y_values=y_mem)],
        x_points=x_points,
        native_style="line",
    )


async def build_series_ping(bot) -> SeriesData:
    """Per-(date, bucket) Discord gateway heartbeat latency in ms.

    Buckets with no measurement (rows predating the ping_ms column, or
    snapshots taken before the first heartbeat ack) are skipped rather than
    plotted as 0 — a 0ms ping is a lie, not a data point.
    """
    import math

    history = await load_bot_stats_history()
    x_points: list[datetime.datetime] = []
    y_ping: list[float] = []
    for point_dt, snap in _iter_points(history):
        ping = snap.get("ping_ms")
        if ping is not None and ping > 0:
            x_points.append(point_dt)
            y_ping.append(ping)

    now_point = _live_now_point()
    latency_s = bot.latency if bot is not None else float("nan")
    if math.isfinite(latency_s) and (not x_points or x_points[-1] != now_point):
        x_points.append(now_point)
        y_ping.append(latency_s * 1000)

    return SeriesData(
        title="Discord Ping",
        segments=[Segment(label="Gateway Ping", color="#3498db", y_values=y_ping)],
        x_points=x_points,
        native_style="line",
    )


MC_BIN_SECONDS = 3600  # aggregation window for per-poll samples: 1 hour


async def build_series_minecraft() -> SeriesData:
    """Minecraft server ping in ms: averaged line with a faded min/max
    band, downtime counted as 0 so outages drag the band (and average)
    visibly toward the floor.

    Two properly-aggregated resolutions, never overlapping: the persisted
    monitor polls (mc_ping_samples, ~60s cadence, last 7 days) binned
    hourly for the recent span, and the daily avg/min/max rollup
    (mc_daily_ping_stats, migration 0042) for completed days strictly
    before the sample window's first CT day. Where both tables cover a
    day, the fine-grained samples win. The coarse per-(date, bucket)
    mc_up/mc_ping_ms snapshots in bot_stats_history are deliberately NOT
    plotted: each is a single instantaneous ping, not an average, and made
    the older tail read as wrong next to aggregated data.
    """
    samples = await load_mc_ping_samples(int(_time.time() - 7 * 86_400))
    raw = [
        (ts, float(latency) if (online and latency) else 0.0)
        for ts, online, latency in samples
    ]
    first_sample_day = _ct_date_of_ts(raw[0][0]) if raw else None

    # Aggregate polls into fixed bins: avg/min/max per bin, plotted at the
    # mean timestamp of the bin's samples (keeps the newest partial bin's
    # point inside its actual data span instead of at the bin edge).
    bins: dict[int, list[tuple[int, float]]] = {}
    for ts, y in raw:
        bins.setdefault(ts // MC_BIN_SECONDS, []).append((ts, y))
    sample_points: list[tuple[datetime.datetime, float, float, float]] = []
    for b in sorted(bins):
        pairs = bins[b]
        ys = [y for _, y in pairs]
        mid_ts = sum(ts for ts, _ in pairs) / len(pairs)
        sample_points.append((
            datetime.datetime.fromtimestamp(mid_ts, tz=datetime.timezone.utc),
            sum(ys) / len(ys), min(ys), max(ys),
        ))

    x_points: list[datetime.datetime] = []
    y_avg: list[float] = []
    y_min: list[float] = []
    y_max: list[float] = []

    def _append(point_dt: datetime.datetime, avg: float, lo: float, hi: float):
        x_points.append(point_dt)
        y_avg.append(avg)
        y_min.append(lo)
        y_max.append(hi)

    # Older tail from the daily rollup, one point per completed day at CT
    # noon. Days on/after the first sample's CT day are excluded so the two
    # resolutions never intersect — the samples cover those (even partially,
    # e.g. after bot downtime truncated the window).
    since = (_calendar_today_date() - datetime.timedelta(days=13)).isoformat()
    for day, avg, lo, hi in await load_mc_daily_ping_stats(since):
        if first_sample_day is not None and day >= first_sample_day:
            continue
        _append(_bucket_start_dt(day, 2), float(avg), float(lo), float(hi))

    for point_dt, avg, lo, hi in sample_points:
        _append(point_dt, avg, lo, hi)

    # Live "now" point from the monitor's latest in-memory sample — only
    # needed when there are no persisted samples ending near now.
    if not sample_points:
        now_point = _live_now_point()
        if state.mc_last_online is not None and (not x_points or x_points[-1] != now_point):
            live_ping = state.mc_last_ping_ms if state.mc_last_online else None
            y = float(live_ping) if live_ping else 0.0
            _append(now_point, y, y, y)

    # Daily player rollup (migration 0041) for the bar overlay: peak
    # concurrent players, cumulative joins, and player-hours per CT calendar
    # day, over the same 2-week window as the line. Today's row is already
    # live — the monitor upserts it every poll. Bars sit at CT noon (bucket
    # 2's start) so each one is centred in its day.
    daily_rows = await load_mc_daily_player_stats(since)
    extras = {}
    if daily_rows:
        extras["mc_daily"] = {
            "x": [_bucket_start_dt(d, 2) for d, _mc, _j, _s in daily_rows],
            "max_concurrent": [float(mc) for _d, mc, _j, _s in daily_rows],
            "joins": [float(j) for _d, _mc, j, _s in daily_rows],
            "hours": [s / 3600.0 for _d, _mc, _j, s in daily_rows],
        }

    return SeriesData(
        title="Minecraft Ping",
        segments=[Segment(
            label="Avg Ping (0 = offline)", color="#2ecc71",
            y_values=y_avg, band_lower=y_min, band_upper=y_max,
        )],
        x_points=x_points,
        native_style="line",
        extras=extras,
    )


# ── Registry ─────────────────────────────────────────────────────────────────

REGISTRY: list[SeriesSpec] = [
    SeriesSpec("balance",  ("balance", "bal"),                          GROUP_COINS,  build_series_balance,  accepts_member=True,  y_unit_label="🪙 Coins"),
    SeriesSpec("economy",  ("economy", "eco", "total", "totalbalance"), GROUP_COINS,  build_series_economy,                          y_unit_label="🪙 Coins"),
    SeriesSpec("crime",    ("crime",),                                  GROUP_COINS,  build_series_crime,    accepts_member=True,  y_unit_label="🪙 Coins"),
    SeriesSpec("gambling", ("gambling", "gamble", "games", "game"),     GROUP_COINS,  build_series_gambling, accepts_member=True,  y_unit_label="🪙 Coins"),
    SeriesSpec("commands", ("commands", "cmd", "cmds"),                 GROUP_COUNTS, build_series_commands,                         y_unit_label="Count"),
    SeriesSpec("server",   ("server", "srv"),                           GROUP_COUNTS, build_series_server,                           y_unit_label="Count"),
    SeriesSpec("ai",       ("ai",),                                     GROUP_COUNTS, build_series_ai,                               y_unit_label="Count"),
    SeriesSpec("memory",   ("memory", "mem", "ram"),                    GROUP_MB,     build_series_memory,                           y_unit_label="MB"),
    SeriesSpec("ping",     ("ping", "latency"),                         GROUP_MS,     build_series_ping,     accepts_bot=True,     y_unit_label="ms"),
    SeriesSpec("minecraft", ("minecraft", "mc", "mcping"),              GROUP_MS_MC,  build_series_minecraft,                        y_unit_label="ms"),
    SeriesSpec("levels",   ("levels", "level", "lvl"),                  GROUP_XP,     build_series_levels,   accepts_member=True, accepts_guild=True, y_unit_label="Level-ups"),
]


def find_spec(token: str) -> Optional[SeriesSpec]:
    """Resolve a token to a series spec, or None if it's not a known alias."""
    t = token.lower()
    for spec in REGISTRY:
        if t in spec.aliases:
            return spec
    return None


# ── Token parsing ────────────────────────────────────────────────────────────


@dataclass
class ParseResult:
    specs: list[SeriesSpec]
    member: Optional[discord.Member]
    guild_id: Optional[int] = None
    error: Optional[str] = None


async def parse_tokens(
    ctx, tokens: tuple[str, ...]
) -> ParseResult:
    """Walk free-form tokens; classify each as series alias OR member mention.

    Member-mention tokens are resolved via discord.py's `MemberConverter`. Any
    token that is neither a known series alias nor a resolvable member is
    rejected with a clear error.

    Validates: at least one series, all in same group, no duplicates,
    guild-scoped series (e.g. `levels`) require ctx.guild.
    """
    from discord.ext import commands as _cmds

    specs: list[SeriesSpec] = []
    seen_names: set[str] = set()
    member: Optional[discord.Member] = None
    converter = _cmds.MemberConverter()

    for tok in tokens:
        spec = find_spec(tok)
        if spec is not None:
            if spec.name in seen_names:
                return ParseResult([], None, error=f"duplicate series: `{spec.name}`")
            specs.append(spec)
            seen_names.add(spec.name)
            continue
        # Not a series alias — try to resolve as a member.
        try:
            resolved = await converter.convert(ctx, tok)
        except _cmds.BadArgument:
            return ParseResult(
                [], None,
                error=f"unknown token `{tok}` — expected a graph name or @user mention",
            )
        if member is not None and resolved.id != member.id:
            return ParseResult([], None, error="more than one user mention provided")
        member = resolved

    if not specs:
        return ParseResult([], None, error="no graph specified")

    groups = {s.group for s in specs}
    if len(groups) > 1:
        names = ", ".join(s.name for s in specs)
        readable = " + ".join(_GROUP_LABEL[g] for g in sorted(groups))
        return ParseResult(
            [], None,
            error=f"cannot combine `{names}` — incompatible y-axes ({readable})",
        )

    # `balance`/`crime`/`gambling`/`levels` accept a member; default to invoker.
    if any(s.accepts_member for s in specs) and member is None:
        member = ctx.author

    # Guild-scoped series need a guild — reject in DMs with a clear error.
    guild_id: Optional[int] = ctx.guild.id if ctx.guild else None
    if any(s.accepts_guild for s in specs) and guild_id is None:
        guilded = ", ".join(s.name for s in specs if s.accepts_guild)
        return ParseResult(
            [], None,
            error=f"`{guilded}` is per-server; run this command inside a server, not in a DM",
        )

    return ParseResult(specs=specs, member=member, guild_id=guild_id)


# ── Admin: per-user breakouts ────────────────────────────────────────────────
# `!graph admin wallet [N|@users…]` and `!graph admin savings [N|@users…]`
# render one line per user. Tokens can be either (a) a single integer N
# selecting top-N by current value, or (b) one or more @user mentions. They
# can't be mixed — the parser rejects with a clear error.

ADMIN_TOP_N_DEFAULT = 10
ADMIN_TOP_N_CAP = 50


@dataclass
class AdminParseResult:
    top_n: Optional[int] = None
    members: list = field(default_factory=list)  # list of discord.Member
    error: Optional[str] = None


async def parse_admin_tokens(ctx, tokens: tuple[str, ...]) -> AdminParseResult:
    """Tokens are either a single integer (top-N) or one-or-more @user
    mentions, never both. Empty tokens → top-N default.
    """
    from discord.ext import commands as _cmds
    converter = _cmds.MemberConverter()

    n: Optional[int] = None
    members: list = []
    seen_member_ids: set[int] = set()

    for tok in tokens:
        # Try integer first.
        if tok.isdigit():
            if n is not None:
                return AdminParseResult(error=f"multiple counts given: `{n}` and `{tok}`")
            try:
                value = int(tok)
            except ValueError:
                return AdminParseResult(error=f"invalid count `{tok}`")
            if value < 1:
                return AdminParseResult(error=f"count must be ≥ 1, got `{value}`")
            if value > ADMIN_TOP_N_CAP:
                return AdminParseResult(
                    error=f"count `{value}` exceeds the cap of {ADMIN_TOP_N_CAP} "
                          f"(too many lines to render legibly)"
                )
            n = value
            continue
        # Otherwise try a member mention.
        try:
            resolved = await converter.convert(ctx, tok)
        except _cmds.BadArgument:
            return AdminParseResult(
                error=f"unknown token `{tok}` — expected an integer or @user mention",
            )
        if resolved.id in seen_member_ids:
            continue  # silently dedupe rather than error
        seen_member_ids.add(resolved.id)
        members.append(resolved)

    if n is not None and members:
        return AdminParseResult(
            error="specify either a count OR @user mentions, not both",
        )
    if n is None and not members:
        n = ADMIN_TOP_N_DEFAULT
    return AdminParseResult(top_n=n, members=members)


async def _resolve_user_labels(bot, uids: list[str]) -> dict[str, str]:
    """Look up `display_name` (or `name`) for each uid via the bot's user
    cache, falling back to `bot.fetch_user(uid)` for uncached uids and
    `<@uid>` mention strings if even fetch fails (e.g. user no longer
    exists or no `bot` provided in tests).
    """
    labels: dict[str, str] = {}
    for uid_str in uids:
        label = f"<@{uid_str}>"  # mention-string fallback
        if bot is not None:
            try:
                uid_int = int(uid_str)
            except ValueError:
                labels[uid_str] = label
                continue
            user = bot.get_user(uid_int)
            if user is None:
                # Cache miss — hit the API. Best-effort; swallow on error.
                try:
                    user = await bot.fetch_user(uid_int)
                except Exception:
                    user = None
            if user is not None:
                label = getattr(user, "display_name", None) or user.name
        labels[uid_str] = label
    return labels


async def build_admin_series(
    field: str,
    *,
    top_n: Optional[int] = None,
    members: Optional[list] = None,
    bot=None,
) -> SeriesData:
    """Build a multi-line SeriesData with one line per user.

    `field` is "wallet", "savings", or "total" (wallet + savings) — the
    column from balance_history to plot. Either `top_n` (selects users
    with the highest current value) or `members` (explicit list) must be
    provided, not both.

    `bot` is the discord.py Bot instance, used to resolve uids to display
    names in top-N mode (we don't have member objects there). Optional
    for tests; falls back to `<@uid>` mention strings when absent.
    """
    assert field in ("wallet", "savings", "total"), f"unknown field {field!r}"
    assert (top_n is None) != (members is None), "exactly one of top_n/members"

    def _row_value(snap: dict) -> float:
        if field == "total":
            return snap.get("wallet", 0) + snap.get("savings", 0)
        return snap.get(field, 0)

    history = await load_balance_history()

    # Walk the history once to gather every (point_dt, uid_str -> value).
    # We need it pivoted: per user, a list of (point_dt, value) pairs.
    by_user: dict[str, list[tuple[datetime.datetime, float]]] = {}
    all_points: list[datetime.datetime] = []
    for point_dt, snap_by_user in _iter_points(history):
        all_points.append(point_dt)
        for uid_str, snap in snap_by_user.items():
            by_user.setdefault(uid_str, []).append(
                (point_dt, _row_value(snap)),
            )

    # Append today's live "now" point from in-memory state.
    now_point = _live_now_point()
    if not all_points or all_points[-1] != now_point:
        all_points.append(now_point)
        import time as _time
        now = _time.time()
        for uid_str, user in state.economy["users"].items():
            wallet = user.get("balance", 0)
            savings = int(sum(
                e["amount"] * (1.01 ** ((now - e["deposited_at"]) / 86400.0))
                for e in user.get("savings", [])
            ))
            if field == "wallet":
                value = wallet
            elif field == "savings":
                value = savings
            else:  # total
                value = wallet + savings
            if value > 0 or uid_str in by_user:
                by_user.setdefault(uid_str, []).append((now_point, value))

    # Pick which users to plot.
    if members is not None:
        picked_ids = [str(m.id) for m in members]
        # Members may not appear in history if they have no recorded balance;
        # filter to those with at least one data point.
        picked_ids = [uid for uid in picked_ids if uid in by_user]
        labels_by_uid = {str(m.id): m.display_name for m in members}
    else:
        # Top N by most-recent value. "Most recent" = each user's last
        # (point_dt, value) pair.
        ranking = []
        for uid, points in by_user.items():
            if not points:
                continue
            latest_value = points[-1][1]
            ranking.append((uid, latest_value))
        ranking.sort(key=lambda t: t[1], reverse=True)
        picked_ids = [uid for uid, _ in ranking[:top_n]]
        labels_by_uid = await _resolve_user_labels(bot, picked_ids)

    # Build segments aligned to the union of all_points.
    import matplotlib.pyplot as _plt
    palette = _plt.cm.tab20.colors

    segments: list[Segment] = []
    for i, uid in enumerate(picked_ids):
        by_pt = dict(by_user.get(uid, []))
        y_values = [by_pt.get(p, 0) for p in all_points]
        segments.append(Segment(
            label=labels_by_uid.get(uid, uid),
            color=palette[i % len(palette)],
            y_values=y_values,
        ))

    if members is not None:
        title = f"Per-User {field.capitalize()}"
    else:
        title = f"Top {top_n} {field.capitalize()}s"

    return SeriesData(
        title=title,
        segments=segments,
        x_points=all_points,
        native_style="line",
    )


# ── Rendering ────────────────────────────────────────────────────────────────


def _fmt_point(p: datetime.datetime) -> str:
    """Tick label for a bucket-start timestamp.

    Shows date for bucket 0 of the day; just the time for the other 3
    buckets — keeps the x-axis legible across ~14 days × 4 buckets.
    """
    if p.hour == 0:
        return f"{p.strftime('%b')} {p.day}"
    return f"{p.hour:02d}:00"


def _common_axes(history_list: list[SeriesData]) -> list[datetime.datetime]:
    """Union of x_points across all series, sorted."""
    seen: set[datetime.datetime] = set()
    for s in history_list:
        seen.update(s.x_points)
    return sorted(seen)


def _aligned_values(values: list[float], src_x: list[datetime.datetime], target_x: list[datetime.datetime]) -> list[float]:
    """Re-index a value list onto target_x. Missing points → 0."""
    by_point = dict(zip(src_x, values))
    return [by_point.get(p, 0.0) for p in target_x]


def _aligned_y(seg: Segment, src_x: list[datetime.datetime], target_x: list[datetime.datetime]) -> list[float]:
    """Re-index a segment's y_values onto target_x. Missing points → 0."""
    return _aligned_values(seg.y_values, src_x, target_x)


async def render_combined(serieses: list[SeriesData], group: str, y_unit_label: str, title: str):
    """Render `serieses` to a PNG buffer. Async wrapper that offloads the
    matplotlib work to a worker thread so the event loop stays responsive
    (renders take ~hundreds of ms and would otherwise block all other I/O).
    """
    import asyncio
    return await asyncio.to_thread(_render_combined_sync, serieses, group, y_unit_label, title)


def _render_combined_sync(serieses: list[SeriesData], group: str, y_unit_label: str, title: str):
    """Render `serieses` to a PNG buffer.

    Style rules:
      - 1 series, native "line"  → overlaid lines, one per segment.
      - 1 series, native "bar"   → stacked bar.
      - N series, group coins    → all segments overlaid as lines.
      - N series, group counts   → grouped bars; each bar internally stacked
                                   from its source's segments. Legend labels
                                   prefixed with [Source].
      - MB group is always size 1.

    Returns an `io.BytesIO` ready to send. Caller closes the figure.
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    if not serieses:
        raise ValueError("render_combined requires at least one series")

    fig, ax = plt.subplots(figsize=(9, 4))
    fig.patch.set_facecolor("#2f3136")
    ax.set_facecolor("#36393f")

    n = len(serieses)
    x_points = _common_axes(serieses)

    if n == 1:
        s = serieses[0]
        if s.native_style == "line":
            for seg in s.segments:
                y = _aligned_y(seg, s.x_points, x_points)
                is_summary = seg.label in ("Total", "Net")
                style = "--" if is_summary else "-"
                marker = "s" if is_summary else "o"
                has_band = seg.band_lower is not None and seg.band_upper is not None
                ax.plot(x_points, y, color=seg.color, linewidth=2, marker=marker,
                        markersize=3 if has_band else 4, linestyle=style, label=seg.label)
                if has_band:
                    lo = _aligned_values(seg.band_lower, s.x_points, x_points)
                    hi = _aligned_values(seg.band_upper, s.x_points, x_points)
                    ax.fill_between(x_points, lo, hi, alpha=0.22, color=seg.color,
                                    linewidth=0, label="min–max")
                elif not is_summary:
                    ax.fill_between(x_points, y, alpha=0.10, color=seg.color)
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_point(mdates.num2date(v))))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            fig.autofmt_xdate(rotation=35, ha="right")
        else:  # native_style == "bar" → stacked bar
            x_nums = list(range(len(x_points)))
            bottoms = [0.0] * len(x_points)
            for seg in s.segments:
                y = _aligned_y(seg, s.x_points, x_points)
                ax.bar(x_nums, y, bottom=bottoms, color=seg.color,
                       width=0.6, label=seg.label)
                bottoms = [b + v for b, v in zip(bottoms, y)]
            ax.set_xticks(x_nums)
            ax.set_xticklabels([_fmt_point(p) for p in x_points], rotation=35, ha="right", fontsize=8)
    else:
        if group == GROUP_COINS:
            # Multiple series, all coins → overlaid lines, prefix labels with source.
            for s in serieses:
                for seg in s.segments:
                    y = _aligned_y(seg, s.x_points, x_points)
                    label = f"[{s.title}] {seg.label}"
                    ax.plot(x_points, y, color=seg.color, linewidth=2, marker="o",
                            markersize=3, label=label)
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_point(mdates.num2date(v))))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            fig.autofmt_xdate(rotation=35, ha="right")
        else:
            # Multiple series, all counts → grouped stacked bars.
            x_nums = list(range(len(x_points)))
            group_total_width = 0.8
            bar_width = group_total_width / n
            for i, s in enumerate(serieses):
                offset = (i - (n - 1) / 2) * bar_width
                bar_x = [xn + offset for xn in x_nums]
                bottoms = [0.0] * len(x_points)
                for seg in s.segments:
                    y = _aligned_y(seg, s.x_points, x_points)
                    label = f"[{s.title}] {seg.label}"
                    ax.bar(bar_x, y, bottom=bottoms, color=seg.color,
                           width=bar_width * 0.95, label=label)
                    bottoms = [b + v for b, v in zip(bottoms, y)]
            ax.set_xticks(x_nums)
            ax.set_xticklabels([_fmt_point(p) for p in x_points], rotation=35, ha="right", fontsize=8)

    if y_unit_label == "MB":
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f} MB"))
    elif y_unit_label == "ms":
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f} ms"))
    else:
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.tick_params(colors="#dcddde", labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#4f545c")
    ax.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)

    ax.set_title(title, color="#ffffff", fontsize=12, pad=10)
    ax.set_ylabel(y_unit_label, color="#b9bbbe", fontsize=9)
    ax.legend(facecolor="#2f3136", edgecolor="#4f545c", labelcolor="#dcddde",
              fontsize=8, loc="upper left")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    plt.close(fig)
    return buf


def render_mc_daily_overlay(series: SeriesData):
    """Solo-minecraft render: the hourly ping line/band plus a per-day bar
    group (peak concurrent players, joins, player-hours) on a secondary
    axis behind it. Only used when `minecraft` is the sole series — its
    group is size 1, so this is the only render path for `!graph mc`.

    Falls back to a plain ping chart when no daily rows exist yet (feature
    just deployed, or the monitor is disabled).
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, ax = plt.subplots(figsize=(9, 4.4))
    fig.patch.set_facecolor("#2f3136")
    ax.set_facecolor("#36393f")

    seg = series.segments[0]
    ax.plot(series.x_points, seg.y_values, color=seg.color, linewidth=2,
            marker="o", markersize=3, label=seg.label, zorder=3)
    if seg.band_lower is not None and seg.band_upper is not None:
        ax.fill_between(series.x_points, seg.band_lower, seg.band_upper,
                        alpha=0.22, color=seg.color, linewidth=0,
                        label="min–max", zorder=2)

    bar_handles, bar_labels = [], []
    daily = series.extras.get("mc_daily")
    if daily and daily["x"]:
        ax2 = ax.twinx()
        # twinx draws its axes on top of the original; flip the order (and
        # clear ax's canvas patch) so the ping line stays above the bars.
        ax.set_zorder(ax2.get_zorder() + 1)
        ax.patch.set_visible(False)
        bar_w = 0.26  # matplotlib date units: fractions of a day
        groups = [
            ("Peak players", "#3498db", daily["max_concurrent"], -bar_w),
            ("Joins",        "#9b59b6", daily["joins"],          0.0),
            ("Player-hours", "#f1c40f", daily["hours"],          bar_w),
        ]
        for label, color, values, offset in groups:
            xs = [mdates.date2num(x) + offset for x in daily["x"]]
            ax2.bar(xs, values, width=bar_w * 0.92, color=color, alpha=0.45,
                    label=label, zorder=1)
        ax2.set_ylabel("players / hours", color="#b9bbbe", fontsize=9)
        ax2.set_ylim(bottom=0)
        ax2.tick_params(colors="#dcddde", labelsize=8)
        ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:g}"))
        for spine in ax2.spines.values():
            spine.set_edgecolor("#4f545c")
        bar_handles, bar_labels = ax2.get_legend_handles_labels()

    ax.xaxis.set_major_formatter(
        plt.FuncFormatter(lambda v, _: _fmt_point(mdates.num2date(v))))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=35, ha="right")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f} ms"))
    ax.tick_params(colors="#dcddde", labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#4f545c")
    ax.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)
    ax.set_title("Minecraft — Ping + Daily Players — Last 2 Weeks",
                 color="#ffffff", fontsize=12, pad=10)
    ax.set_ylabel("ms", color="#b9bbbe", fontsize=9)
    line_handles, line_labels = ax.get_legend_handles_labels()
    ax.legend(line_handles + bar_handles, line_labels + bar_labels,
              facecolor="#2f3136", edgecolor="#4f545c", labelcolor="#dcddde",
              fontsize=8, loc="upper left")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    plt.close(fig)
    return buf


def render_ai_uptime_strip(series: SeriesData):
    """Single-mode AI render: bar chart on top, uptime green/red/grey strip below.
    Only used when `ai` is the sole series — combination drops the strip.
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    flags: list = series.extras.get("ai_up_flags", [])
    x_points = series.x_points
    seg = series.segments[0]
    y_responses = seg.y_values

    fig, (ax_bar, ax_line) = plt.subplots(
        2, 1, figsize=(9, 6), gridspec_kw={"height_ratios": [3, 1]}
    )
    fig.patch.set_facecolor("#2f3136")
    for ax in (ax_bar, ax_line):
        ax.set_facecolor("#36393f")

    x_nums = list(range(len(x_points)))
    bar_colors = []
    for flag in flags:
        if flag is True:
            bar_colors.append("#2ecc71")
        elif flag is False:
            bar_colors.append("#e74c3c")
        else:
            bar_colors.append("#95a5a6")
    ax_bar.bar(x_nums, y_responses, color=bar_colors, alpha=0.85, width=0.6)
    ax_bar.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax_bar.set_xticks(x_nums)
    ax_bar.set_xticklabels([_fmt_point(p) for p in x_points], rotation=35, ha="right", fontsize=8)
    ax_bar.tick_params(colors="#dcddde", labelsize=8)
    for spine in ax_bar.spines.values():
        spine.set_edgecolor("#4f545c")
    ax_bar.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)
    ax_bar.set_title("AI Activity — Last 2 Weeks", color="#ffffff", fontsize=12, pad=10)
    ax_bar.set_ylabel("Responses", color="#b9bbbe", fontsize=9)
    legend_elements = [
        Patch(facecolor="#2ecc71", label="AI up"),
        Patch(facecolor="#e74c3c", label="AI down"),
        Patch(facecolor="#95a5a6", label="Unknown"),
    ]
    ax_bar.legend(handles=legend_elements, facecolor="#2f3136", edgecolor="#4f545c",
                  labelcolor="#dcddde", fontsize=8)

    for i, flag in enumerate(flags):
        color = "#2ecc71" if flag is True else ("#e74c3c" if flag is False else "#95a5a6")
        ax_line.barh(0, 1, left=i, color=color, height=1, alpha=0.85)
    ax_line.set_xlim(0, len(x_points))
    ax_line.set_ylim(-0.5, 0.5)
    ax_line.set_xticks([])
    ax_line.set_yticks([])
    ax_line.set_ylabel("Uptime", color="#b9bbbe", fontsize=8)
    for spine in ax_line.spines.values():
        spine.set_edgecolor("#4f545c")

    fig.tight_layout(pad=1.5)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    plt.close(fig)
    return buf
