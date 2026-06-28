"""Persistence for honor-based bounties (!shop bounty / !bounty).

A bounty row is keyed by `message_id` (the embed posted in the guild's bounty
channel). The on_raw_reaction_add handler in bounty_cog looks up rows by
message_id (the claim 🙋 reaction), by dm_message_id (the author's accept/reject
DM), by contest_message_id (the rejected claimant's contest DM), and by
poll_message_id (the @everyone contest poll) — so reactions added after a
restart still resolve, exactly like the issues table.

Escrow lives in the coin ledger, not here: the author's `amount` is deducted at
creation and re-added on cancel/expire/reject, or paid to the claimant on
accept / poll win. This table only tracks lifecycle, not balances.
"""
import json
import time

from src.db import with_cursor


_BOUNTY_COLS = (
    "id, guild_id, channel_id, message_id, author_id, amount, `condition`,"
    " status, claimant_id, dm_message_id, contest_message_id, poll_message_id,"
    " poll_channel_id, claim_expires_at, contest_expires_at, poll_expires_at,"
    " claim_log, created_at"
)


def _row_to_bounty(row) -> dict:
    return {
        "id": row[0],
        "guild_id": row[1],
        "channel_id": row[2],
        "message_id": row[3],
        "author_id": row[4],
        "amount": row[5],
        "condition": row[6],
        "status": row[7],
        "claimant_id": row[8],
        "dm_message_id": row[9],
        "contest_message_id": row[10],
        "poll_message_id": row[11],
        "poll_channel_id": row[12],
        "claim_expires_at": row[13],
        "contest_expires_at": row[14],
        "poll_expires_at": row[15],
        "claim_log": json.loads(row[16]) if row[16] else [],
        "created_at": row[17],
    }


async def insert_bounty(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    author_id: int,
    amount: int,
    condition: str,
) -> int:
    """Insert a fresh open bounty. Returns the inserted id."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO bounties (guild_id, channel_id, message_id, author_id,"
            " amount, `condition`, status, claim_log, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,'open',%s,%s)",
            (guild_id, channel_id, message_id, author_id, amount, condition,
             json.dumps([]), time.time()),
        )
        return cur.lastrowid


async def get_bounty_by_message(message_id: int) -> dict | None:
    """Fetch the bounty whose embed is `message_id`, or None."""
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_BOUNTY_COLS} FROM bounties WHERE message_id=%s",  # nosec B608 - _BOUNTY_COLS is a literal
            (message_id,),
        )
        row = await cur.fetchone()
    return _row_to_bounty(row) if row else None


async def get_bounty_by_dm(dm_message_id: int) -> dict | None:
    """Fetch the bounty whose author accept/reject DM is `dm_message_id`."""
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_BOUNTY_COLS} FROM bounties WHERE dm_message_id=%s",  # nosec B608 - _BOUNTY_COLS is a literal
            (dm_message_id,),
        )
        row = await cur.fetchone()
    return _row_to_bounty(row) if row else None


async def get_bounty_by_contest(contest_message_id: int) -> dict | None:
    """Fetch the bounty whose claimant contest-offer DM is `contest_message_id`."""
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_BOUNTY_COLS} FROM bounties WHERE contest_message_id=%s",  # nosec B608 - _BOUNTY_COLS is a literal
            (contest_message_id,),
        )
        row = await cur.fetchone()
    return _row_to_bounty(row) if row else None


async def get_bounty_by_poll(poll_message_id: int) -> dict | None:
    """Fetch the bounty whose @everyone contest poll is `poll_message_id`."""
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_BOUNTY_COLS} FROM bounties WHERE poll_message_id=%s",  # nosec B608 - _BOUNTY_COLS is a literal
            (poll_message_id,),
        )
        row = await cur.fetchone()
    return _row_to_bounty(row) if row else None


async def update_bounty(message_id: int, **fields) -> None:
    """Patch arbitrary columns on the bounty keyed by `message_id`.

    `claim_log` is JSON-encoded automatically if passed as a list. Only the
    keys present in `fields` are written, so callers can update just the bits a
    transition touches (e.g. status + claimant_id + claim_expires_at).
    """
    if not fields:
        return
    cols, params = [], []
    for k, v in fields.items():
        col = f"`{k}`" if k == "condition" else k
        cols.append(f"{col}=%s")
        if k == "claim_log" and not isinstance(v, (str, type(None))):
            v = json.dumps(v)
        params.append(v)
    params.append(message_id)
    sql = f"UPDATE bounties SET {', '.join(cols)} WHERE message_id=%s"  # nosec B608 - cols are literal "name=%s"
    async with with_cursor() as cur:
        await cur.execute(sql, tuple(params))


async def load_active_bounties() -> list[dict]:
    """All bounties not in a terminal state, for rehydrating in-memory state
    at boot. Terminal states (accepted/rejected/cancelled) stay in the DB as
    history but aren't loaded — nothing drives them anymore."""
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_BOUNTY_COLS} FROM bounties"  # nosec B608 - _BOUNTY_COLS is a literal
            " WHERE status IN ('open','pending','contesting','polling')"
        )
        rows = await cur.fetchall()
    return [_row_to_bounty(r) for r in rows]
