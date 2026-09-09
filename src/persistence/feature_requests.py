"""Persistence for !featurerequest user submissions.

Mirrors the issues persistence module's shape: rows keyed by `message_id`
for fast resolution from `on_raw_reaction_add` payloads, plus a secondary
lookup by `feature_issue_id` so status-change reactions on a spawned
feature issue can re-render the originating request embed.
"""
import datetime
import time

from src.db import with_cursor


async def insert_feature_request(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    reporter_id: int,
    description: str,
) -> int:
    """Insert a new open feature_request row. Returns the inserted id."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO feature_requests"
            " (guild_id, channel_id, message_id, reporter_id, description, status)"
            " VALUES (%s,%s,%s,%s,%s,'open')",
            (guild_id, channel_id, message_id, reporter_id, description),
        )
        return cur.lastrowid


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "guild_id": row[1],
        "channel_id": row[2],
        "message_id": row[3],
        "reporter_id": row[4],
        "description": row[5],
        "status": row[6],
        "feature_issue_id": row[7],
        "created_at": row[8],
        "resolved_by": row[9],
        "resolved_at": row[10],
    }


_FR_COLS = (
    "id, guild_id, channel_id, message_id, reporter_id, description,"
    " status, feature_issue_id, created_at, resolved_by, resolved_at"
)


async def get_feature_request_by_message(message_id: int) -> dict | None:
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_FR_COLS} FROM feature_requests WHERE message_id=%s",  # nosec B608 - _FR_COLS is a literal
            (message_id,),
        )
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def get_feature_request_by_feature_id(feature_issue_id: int) -> dict | None:
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_FR_COLS} FROM feature_requests WHERE feature_issue_id=%s",  # nosec B608 - _FR_COLS is a literal
            (feature_issue_id,),
        )
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def update_feature_request_status(
    message_id: int, status: str, resolved_by: int | None
) -> None:
    resolved_at = datetime.datetime.fromtimestamp(time.time(), tz=datetime.timezone.utc)
    async with with_cursor() as cur:
        await cur.execute(
            "UPDATE feature_requests SET status=%s, resolved_by=%s, resolved_at=%s"
            " WHERE message_id=%s",
            (status, resolved_by, resolved_at, message_id),
        )


async def link_feature_to_request(message_id: int, feature_issue_id: int) -> None:
    """Set `feature_issue_id` on a feature_request keyed by its embed message_id."""
    async with with_cursor() as cur:
        await cur.execute(
            "UPDATE feature_requests SET feature_issue_id=%s WHERE message_id=%s",
            (feature_issue_id, message_id),
        )


# ── 👀 watchers ──────────────────────────────────────────────────────────────
# Users following a request they didn't file. The requester is notified on
# completion / rejection without a row; these rows extend the same DMs to
# anyone who reacted 👀. Keyed by the request embed's message_id so the raw
# reaction handlers can add/remove straight from the payload.

async def add_feature_request_watcher(message_id: int, user_id: int) -> None:
    """Idempotent: re-watching an already-watched request is a no-op."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO feature_request_watchers (message_id, user_id)"
            " VALUES (%s,%s)"
            " ON DUPLICATE KEY UPDATE user_id=VALUES(user_id)",
            (message_id, user_id),
        )


async def remove_feature_request_watcher(message_id: int, user_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM feature_request_watchers WHERE message_id=%s AND user_id=%s",
            (message_id, user_id),
        )


async def list_feature_request_watchers(message_id: int) -> list[int]:
    """User ids watching the request, in the order they started watching."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT user_id FROM feature_request_watchers WHERE message_id=%s"
            " ORDER BY created_at, user_id",
            (message_id,),
        )
        rows = await cur.fetchall()
    return [int(r[0]) for r in rows]
