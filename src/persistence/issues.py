"""Persistence for !bug / !issue reports and auto-filed command-error reports.

The issue row is keyed by `message_id` (the embed posted in the bug-report
channel). The on_raw_reaction_add handler in utility_cog looks up rows by
message_id when an admin reacts, so reactions added after a restart still
work — Discord re-delivers the gateway event but the bot's message cache
is empty until the message is touched.

Auto-filed error reports also persist a `mute_key` so the 🔇 reaction can
flip the corresponding `error_mutes` entry without rebuilding the key.
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
    kind: str = "bug",
    mute_key: str | None = None,
) -> int:
    """Insert a new open issue row. Returns the inserted id.

    `kind` is one of 'bug' | 'feature' | 'task' | 'improvement' | 'error'.
    `mute_key` is set for kind='error' only; it lets the 🔇 reaction handler
    toggle the matching error_mutes row without recomputing the key.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO issues (guild_id, channel_id, message_id, reporter_id, report, status, kind, mute_key)"
            " VALUES (%s,%s,%s,%s,%s,'open',%s,%s)",
            (guild_id, channel_id, message_id, reporter_id, report, kind, mute_key),
        )
        return cur.lastrowid


async def get_issue_by_message(message_id: int) -> dict | None:
    """Fetch the issue row for a given embed message_id, or None."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT id, guild_id, channel_id, message_id, reporter_id, report, status,"
            " created_at, resolved_by, resolved_at, kind, mute_key, deleted"
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
        "kind": row[10],
        "mute_key": row[11],
        "deleted": bool(row[12]),
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


async def list_issues(
    *,
    statuses: tuple[str, ...] | None = None,
    include_deleted: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Return issue rows, newest first, optionally filtered by status.

    Filters are AND-ed: `statuses=('open','wip')` returns only those two.
    None means no status filter (include every status). `deleted=1` rows
    are excluded unless `include_deleted=True`.
    """
    sql = (
        "SELECT id, guild_id, channel_id, message_id, reporter_id, report, status,"
        " created_at, resolved_by, resolved_at, kind, mute_key, deleted"
        " FROM issues WHERE 1=1"
    )
    params: list = []
    if not include_deleted:
        sql += " AND deleted=0"
    if statuses:
        placeholders = ",".join(["%s"] * len(statuses))
        sql += f" AND status IN ({placeholders})"  # nosec B608 - placeholders are literal "%s"
        params.extend(statuses)
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(int(limit))

    async with with_cursor() as cur:
        await cur.execute(sql, tuple(params))
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "guild_id": r[1],
            "channel_id": r[2],
            "message_id": r[3],
            "reporter_id": r[4],
            "report": r[5],
            "status": r[6],
            "created_at": r[7],
            "resolved_by": r[8],
            "resolved_at": r[9],
            "kind": r[10],
            "mute_key": r[11],
            "deleted": bool(r[12]),
        }
        for r in rows
    ]


async def soft_delete_issue(issue_id: int) -> None:
    """Mark `id=...` as deleted. Idempotent — re-deleting is a no-op."""
    async with with_cursor() as cur:
        await cur.execute(
            "UPDATE issues SET deleted=1 WHERE id=%s",
            (issue_id,),
        )


async def insert_error_mute(mute_key: str, muted_by: int) -> None:
    """Idempotent: same key reacted twice doesn't error."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO error_mutes (mute_key, muted_by) VALUES (%s,%s)"
            " ON DUPLICATE KEY UPDATE muted_by=VALUES(muted_by)",
            (mute_key, muted_by),
        )


async def delete_error_mute(mute_key: str) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM error_mutes WHERE mute_key=%s",
            (mute_key,),
        )


async def load_error_mutes() -> set[str]:
    """Fetch all currently muted error keys. Called at boot."""
    async with with_cursor() as cur:
        await cur.execute("SELECT mute_key FROM error_mutes")
        rows = await cur.fetchall()
    return {r[0] for r in rows}
