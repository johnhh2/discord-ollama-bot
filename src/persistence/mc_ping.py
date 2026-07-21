"""Persistence for per-poll Minecraft monitor samples (migration 0035).

One row per status ping (~60s cadence). Offline rows carry latency_ms NULL.
The monitor prunes rows older than its 7-day stats window, so the table
stays around 10k rows.
"""
from src.db import with_cursor


async def save_mc_ping_sample(ts: int, online: bool, latency_ms: "float | None"):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO mc_ping_samples (ts, online, latency_ms) VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE online=VALUES(online), latency_ms=VALUES(latency_ms)",
            (ts, 1 if online else 0, latency_ms),
        )


async def load_mc_ping_samples(since_ts: int) -> "list[tuple[int, bool, float | None]]":
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT ts, online, latency_ms FROM mc_ping_samples"
            " WHERE ts >= %s ORDER BY ts",
            (since_ts,),
        )
        rows = await cur.fetchall()
    return [(row[0], bool(row[1]), row[2]) for row in rows]


async def prune_mc_ping_samples(before_ts: int):
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM mc_ping_samples WHERE ts < %s", (before_ts,))


# ── Player tracking (migration 0041) ─────────────────────────────────────────
# The Bedrock pong is anonymous, so a "player event" is an observed change in
# the player count: delta > 0 joins, delta < 0 leaves. One row per monitor
# poll that saw a change; retained ~10 years (pruned by the monitor).


async def record_mc_player_event(ts: int, delta: int, players: int):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO mc_player_events (ts, delta, players) VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE delta=VALUES(delta), players=VALUES(players)",
            (ts, delta, players),
        )


async def load_mc_player_events(since_ts: int) -> "list[tuple[int, int, int]]":
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT ts, delta, players FROM mc_player_events"
            " WHERE ts >= %s ORDER BY ts",
            (since_ts,),
        )
        rows = await cur.fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


async def prune_mc_player_events(before_ts: int):
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM mc_player_events WHERE ts < %s", (before_ts,))


async def upsert_mc_daily_player_stats(
    date_iso: str, players: int, joins: int, player_seconds: float,
):
    """Fold one monitor poll into the day's rollup row: peak concurrent is a
    running GREATEST, joins and player-seconds accumulate. SQL-side
    accumulation keeps the row correct across bot restarts with no
    restore-on-boot step.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO mc_daily_player_stats"
            " (stat_date, max_concurrent, total_joins, player_seconds)"
            " VALUES (%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE"
            " max_concurrent=GREATEST(max_concurrent, VALUES(max_concurrent)),"
            " total_joins=total_joins+VALUES(total_joins),"
            " player_seconds=player_seconds+VALUES(player_seconds)",
            (date_iso, players, joins, player_seconds),
        )


async def load_mc_daily_player_stats(
    since_date_iso: str,
) -> "list[tuple[str, int, int, float]]":
    """Rows of (date_iso, max_concurrent, total_joins, player_seconds),
    oldest first. ISO date strings compare lexicographically = chronologically.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT stat_date, max_concurrent, total_joins, player_seconds"
            " FROM mc_daily_player_stats WHERE stat_date >= %s ORDER BY stat_date",
            (since_date_iso,),
        )
        rows = await cur.fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


async def prune_mc_daily_player_stats(before_date_iso: str):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM mc_daily_player_stats WHERE stat_date < %s",
            (before_date_iso,),
        )


# ── Daily ping rollup (migration 0042) ───────────────────────────────────────
# One row per completed CT day: avg/min/max over that day's monitor polls,
# downtime counted as 0. Written by the monitor's hourly rollup; kept ~10
# years so the ping history outlives the 7-day sample table.


async def save_mc_daily_ping_stats(
    date_iso: str, avg_ping: float, min_ping: float, max_ping: float,
):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO mc_daily_ping_stats"
            " (stat_date, avg_ping, min_ping, max_ping) VALUES (%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE avg_ping=VALUES(avg_ping),"
            " min_ping=VALUES(min_ping), max_ping=VALUES(max_ping)",
            (date_iso, avg_ping, min_ping, max_ping),
        )


async def load_mc_daily_ping_stats(
    since_date_iso: str,
) -> "list[tuple[str, float, float, float]]":
    """Rows of (date_iso, avg_ping, min_ping, max_ping), oldest first."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT stat_date, avg_ping, min_ping, max_ping"
            " FROM mc_daily_ping_stats WHERE stat_date >= %s ORDER BY stat_date",
            (since_date_iso,),
        )
        rows = await cur.fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


async def prune_mc_daily_ping_stats(before_date_iso: str):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM mc_daily_ping_stats WHERE stat_date < %s",
            (before_date_iso,),
        )
