"""Payout + record-setting when a human beats Stockfish.

Daily-resetting highwater: each user has a "highest bot Elo defeated today"
field on their economy row. Each win pays 20 coins per NEW Elo point beyond
that highwater. Daily reset (5am CT) zeroes it out so the same Elo can be
beaten again tomorrow for full credit.

This module is intentionally thin: pure calculation + state mutation + DB
write. The cog (_finalize_game) decides WHEN to call award_bot_defeat. The
record-setting category lives in src/helpers.py:RECORD_LABELS for display.
"""
from __future__ import annotations

from src import state
from src.economy import _ct_today, add_balance, _ensure_user
from src.persistence import save_economy, try_set_record


# Per-elo-point payout. 20 coins per new Elo defeated, scoped by daily reset.
COINS_PER_NEW_ELO = 20

# Record category for !records. Matches the snake_case convention used by
# existing categories (see RECORD_LABELS in src/helpers.py).
RECORD_CATEGORY = "highest_bot_chess_elo_defeated"


def _todays_highwater(user: dict, today: str) -> int:
    """Read the user's highest bot-Elo-defeated for `today`, treating a
    stale stored date as 0. Self-heals without depending on do_daily_reset
    having fired."""
    if user.get("bot_chess_elo_max_date") != today:
        return 0
    return int(user.get("bot_chess_elo_max_today", 0) or 0)


async def award_bot_defeat(
    *, user_id: int, guild_id: int | None, holder_name: str, bot_elo: int,
) -> tuple[int, bool]:
    """Apply the daily-highwater payout + try to set the global record.

    Returns (payout_coins, record_broken):
      payout_coins: 0 if no new ground was gained today.
      record_broken: True iff this win set a new per-guild high.

    No payout for sub-1 Elo gains; idempotent if called twice with the same
    bot_elo on the same day. Caller is responsible for not invoking this on
    losses, draws, or human-vs-human games.
    """
    if bot_elo <= 0:
        return 0, False
    await _ensure_user(user_id)
    today = _ct_today()
    user = state.economy["users"][str(user_id)]

    prior = _todays_highwater(user, today)
    if bot_elo <= prior:
        # Already beat an equal-or-stronger bot today. Update the date in
        # case it was stale (self-healing read) but don't pay out.
        if user.get("bot_chess_elo_max_date") != today:
            user["bot_chess_elo_max_today"] = prior
            user["bot_chess_elo_max_date"] = today
            await save_economy(uid=user_id)
        payout = 0
    else:
        payout = (bot_elo - prior) * COINS_PER_NEW_ELO
        user["bot_chess_elo_max_today"] = bot_elo
        user["bot_chess_elo_max_date"] = today
        await save_economy(uid=user_id)
        if payout > 0:
            await add_balance(user_id, payout)

    # Try to set the per-guild record. try_set_record short-circuits on
    # guild_id=None and on non-improving values, so this is safe to always call.
    record_broken = False
    if guild_id is not None:
        record_broken = await try_set_record(
            guild_id, RECORD_CATEGORY, bot_elo, user_id, holder_name,
        )

    return payout, record_broken
