from src.db import with_cursor


async def log_notable_event(
    guild_id: int, day: str, kind: str, category: str | None,
    holder_name: str, value: int,
) -> None:
    """Append one notable-event row (a record break or a lottery win).

    Best-effort: a guild_id of None (DM context) is a no-op. `day` is the
    5am-CT day string from `_ct_today()`. Plain INSERT — every event is its
    own row, no upsert.
    """
    if guild_id is None:
        return
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO notable_events"
            " (guild_id, event_day, kind, category, holder_name, value)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (guild_id, day, kind, category, holder_name[:64], int(value)),
        )


async def load_notable_events_today(guild_id: int, day: str) -> list[dict]:
    """Today's notable events for `guild_id`, oldest first.

    Returns [{"kind", "category", "holder_name", "value"}]. Used by !recap
    to feed the AI a clean list of records-broken / lotteries-won.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT kind, category, holder_name, value FROM notable_events"
            " WHERE guild_id = %s AND event_day = %s ORDER BY id ASC",
            (guild_id, day),
        )
        rows = await cur.fetchall()
    return [
        {"kind": k, "category": c, "holder_name": h, "value": int(v)}
        for k, c, h, v in rows
    ]


async def prune_notable_events(*, before_date: str) -> None:
    """Drop notable_events older than `before_date` (a day string).

    Called from the daily reset alongside the other *_history pruners so
    the table stays bounded to the graph-history retention window.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM notable_events WHERE event_day < %s",
            (before_date,),
        )
