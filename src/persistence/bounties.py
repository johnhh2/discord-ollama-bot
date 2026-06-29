"""Persistence for honor-based bounties (!shop bounty / !bounty).

Two tables (see migrations 0031 + 0033):

* `bounties` — one row per posted bounty, keyed by the embed `message_id`. Holds
  the bounty-level fields (amount, condition, optional `expires_at`, status, and
  a JSON `claim_log` rendered at the bottom of the embed). status is one of
  open / accepted / cancelled / expired. The legacy per-claim columns from 0031
  are unused by this module.

* `bounty_claims` — one row per (bounty, claimant). A bounty stays open through
  many concurrent claims; each claim carries its own author-DM, contest offer,
  and @everyone poll message ids plus their expiries. The on_raw_reaction_add
  handler resolves a reaction by looking the claim up by dm/contest/poll message
  id, so reactions survive a reboot exactly like the issues table.

Escrow lives in the coin ledger, not here: the author's `amount` is deducted at
creation and re-added (90%) on cancel/expire, or paid to a claimant on accept /
poll win. These tables only track lifecycle, not balances.
"""
import json
import time

from src.db import with_cursor


_BOUNTY_COLS = (
    "id, guild_id, channel_id, message_id, author_id, amount, `condition`,"
    " status, expires_at, claim_log, created_at"
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
        "expires_at": row[8],
        "claim_log": json.loads(row[9]) if row[9] else [],
        "created_at": row[10],
        # Populated by load_active_bounties(); empty for single-row fetches.
        "claims": [],
    }


_CLAIM_COLS = (
    "id, bounty_id, claimant_id, status, dm_message_id, contest_message_id,"
    " poll_message_id, poll_channel_id, claim_expires_at, contest_expires_at,"
    " poll_expires_at, created_at"
)


def _row_to_claim(row) -> dict:
    return {
        "id": row[0],
        "bounty_id": row[1],
        "claimant_id": row[2],
        "status": row[3],
        "dm_message_id": row[4],
        "contest_message_id": row[5],
        "poll_message_id": row[6],
        "poll_channel_id": row[7],
        "claim_expires_at": row[8],
        "contest_expires_at": row[9],
        "poll_expires_at": row[10],
        "created_at": row[11],
    }


# ── bounties ──────────────────────────────────────────────────────────────────
async def insert_bounty(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    author_id: int,
    amount: int,
    condition: str,
    expires_at: "float | None" = None,
) -> int:
    """Insert a fresh open bounty. Returns the inserted id."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO bounties (guild_id, channel_id, message_id, author_id,"
            " amount, `condition`, status, expires_at, claim_log, created_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,'open',%s,%s,%s)",
            (guild_id, channel_id, message_id, author_id, amount, condition,
             expires_at, json.dumps([]), time.time()),
        )
        return cur.lastrowid


async def get_bounty_by_message(message_id: int) -> dict | None:
    """Fetch the bounty whose embed is `message_id` (claims not attached), or None."""
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_BOUNTY_COLS} FROM bounties WHERE message_id=%s",  # nosec B608 - _BOUNTY_COLS is a literal
            (message_id,),
        )
        row = await cur.fetchone()
    return _row_to_bounty(row) if row else None


async def update_bounty(message_id: int, **fields) -> None:
    """Patch bounty-level columns on the row keyed by `message_id`.

    `claim_log` is JSON-encoded automatically if passed as a list. Only the keys
    present in `fields` are written.
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


# ── bounty_claims ─────────────────────────────────────────────────────────────
async def insert_claim(*, bounty_id: int, claimant_id: int) -> int:
    """Insert a fresh pending claim. Returns the inserted id."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO bounty_claims (bounty_id, claimant_id, status, created_at)"
            " VALUES (%s,%s,'pending',%s)",
            (bounty_id, claimant_id, time.time()),
        )
        return cur.lastrowid


async def update_claim(claim_id: int, **fields) -> None:
    """Patch columns on the claim row keyed by its primary-key id."""
    if not fields:
        return
    cols = [f"{k}=%s" for k in fields]
    params = list(fields.values())
    params.append(claim_id)
    sql = f"UPDATE bounty_claims SET {', '.join(cols)} WHERE id=%s"  # nosec B608 - cols are literal "name=%s"
    async with with_cursor() as cur:
        await cur.execute(sql, tuple(params))


async def _claim_join_bounty(where: str, param) -> "tuple[dict, dict] | None":
    """Fetch (bounty, claim) for the claim matching `WHERE bc.<where>=param`.

    Returns None if no claim matches or its parent bounty is gone. Used by the
    reaction dispatcher to resolve a DM / contest / poll reaction back to its
    claim and the bounty it belongs to.
    """
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_CLAIM_COLS} FROM bounty_claims bc WHERE bc.{where}=%s",  # nosec B608 - col + cols are literals
            (param,),
        )
        crow = await cur.fetchone()
        if crow is None:
            return None
        claim = _row_to_claim(crow)
        await cur.execute(
            f"SELECT {_BOUNTY_COLS} FROM bounties WHERE id=%s",  # nosec B608 - _BOUNTY_COLS is a literal
            (claim["bounty_id"],),
        )
        brow = await cur.fetchone()
    if brow is None:
        return None
    return _row_to_bounty(brow), claim


async def get_claim_by_dm(dm_message_id: int):
    """(bounty, claim) for the author accept/reject DM, or None."""
    return await _claim_join_bounty("dm_message_id", dm_message_id)


async def get_claim_by_contest(contest_message_id: int):
    """(bounty, claim) for the claimant's contest-offer DM, or None."""
    return await _claim_join_bounty("contest_message_id", contest_message_id)


async def get_claim_by_poll(poll_message_id: int):
    """(bounty, claim) for the @everyone contest poll message, or None."""
    return await _claim_join_bounty("poll_message_id", poll_message_id)


# ── boot rehydration ──────────────────────────────────────────────────────────
async def load_active_bounties() -> list[dict]:
    """Every non-terminal bounty with its non-terminal claims attached, for
    rehydrating in-memory state at boot. Terminal bounties (accepted/cancelled/
    expired) and terminal claims (accepted/rejected/voided) stay in the DB as
    history but aren't loaded — nothing drives them anymore."""
    async with with_cursor() as cur:
        await cur.execute(
            f"SELECT {_BOUNTY_COLS} FROM bounties WHERE status='open'"  # nosec B608 - _BOUNTY_COLS is a literal
        )
        bounties = [_row_to_bounty(r) for r in await cur.fetchall()]
        by_id = {b["id"]: b for b in bounties}
        if by_id:
            await cur.execute(
                f"SELECT {_CLAIM_COLS} FROM bounty_claims"  # nosec B608 - _CLAIM_COLS is a literal
                " WHERE status IN ('pending','contesting','polling')"
            )
            for crow in await cur.fetchall():
                claim = _row_to_claim(crow)
                parent = by_id.get(claim["bounty_id"])
                if parent is not None:
                    parent["claims"].append(claim)
    return bounties
