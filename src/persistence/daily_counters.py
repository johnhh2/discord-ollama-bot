"""Generic per-gameplay-day counters (migration 0036).

One row per (day, counter). `day` is the 5am-CT day string from
src.economy._ct_today(), so counters roll over with the rest of the daily
economy state. Currently backs the "lottery tickets sold today" presence
line; future daily statuses can add their own counter names without a new
table. Rows older than the graph retention window are pruned by
do_daily_reset.
"""
from src.db import with_cursor


async def bump_daily_counter(day: str, counter: str, delta: int):
    """Atomically add delta to the counter, creating the row at delta."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO daily_counters (day, counter, value) VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE value = value + VALUES(value)",
            (day, counter, delta),
        )


async def load_daily_counter(day: str, counter: str) -> int:
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT value FROM daily_counters WHERE day=%s AND counter=%s",
            (day, counter),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def prune_daily_counters(before_date: str):
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM daily_counters WHERE day < %s", (before_date,))
