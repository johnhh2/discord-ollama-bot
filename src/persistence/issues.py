"""Persistence for !bug / !issue reports.

The issue row is keyed by `message_id` (the embed posted in the bug-report
channel). The on_raw_reaction_add handler in utility_cog looks up rows by
message_id when an admin reacts, so reactions added after a restart still
work — Discord re-delivers the gateway event but the bot's message cache
is empty until the message is touched.
"""
import datetime
import time

from src.db import with_cursor


async def insert_issue(
    *,
    guild_id: int | None,
    channel_id: int,
    message_id: int,
    reporter_id: int,
    report: str,
) -> int:
    """Insert a new open issue row. Returns the inserted id."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO issues (guild_id, channel_id, message_id, reporter_id, report, status)"
            " VALUES (%s,%s,%s,%s,%s,'open')",
            (guild_id, channel_id, message_id, reporter_id, report),
        )
        return cur.lastrowid


async def get_issue_by_message(message_id: int) -> dict | None:
    """Fetch the issue row for a given embed message_id, or None."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT id, guild_id, channel_id, message_id, reporter_id, report, status,"
            " created_at, resolved_by, resolved_at"
            " FROM issues WHERE message_id=%s",
            (message_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "guild_id": row[1],
        "channel_id": row[2],
        "message_id": row[3],
        "reporter_id": row[4],
        "report": row[5],
        "status": row[6],
        "created_at": row[7],
        "resolved_by": row[8],
        "resolved_at": row[9],
    }


async def update_issue_status(
    message_id: int, status: str, resolved_by: int | None
) -> None:
    """Set the status on the issue row keyed by message_id."""
    resolved_at = datetime.datetime.fromtimestamp(time.time(), tz=datetime.timezone.utc)
    async with with_cursor() as cur:
        await cur.execute(
            "UPDATE issues SET status=%s, resolved_by=%s, resolved_at=%s"
            " WHERE message_id=%s",
            (status, resolved_by, resolved_at, message_id),
        )
