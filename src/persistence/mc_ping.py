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
