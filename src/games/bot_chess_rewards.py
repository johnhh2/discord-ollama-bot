"""Payout + record-setting when a human beats the chess bot.

Daily-resetting highwater: each user has a "highest bot Elo defeated today"
field on their economy row. The payout rate doubles at the sub-Maia / Maia
boundary (Elo 1100):
  - Sub-Maia tier (Elo 100-1000): 5 coins per new Elo point.
  - Maia / Stockfish tier (Elo 1100+): 10 coins per new Elo point.

Daily reset (5am CT) zeroes it out so the same Elo can be beaten again
tomorrow for full credit.

This module is intentionally thin: pure calculation + state mutation + DB
write. The cog (_finalize_game) decides WHEN to call award_bot_defeat. The
record-setting category lives in src/helpers.py:RECORD_LABELS for display.
"""
from __future__ import annotations

from src import state
from src.economy import _ct_today, add_balance, _ensure_user
from src.persistence import save_economy, try_set_record


# Per-elo-point payout. Sub-Maia tier (Elo 100-1000) pays COINS_PER_NEW_ELO_LOW;
# Maia/Stockfish tier (Elo 1100+) pays double via COINS_PER_NEW_ELO. Boundary
# matches the engine-tier handoff in chess_bot.py (MAIA_ELO_MIN = 1100).
COINS_PER_NEW_ELO = 10
COINS_PER_NEW_ELO_LOW = 5
LOW_ELO_THRESHOLD = 1100


def _payout_for_range(prior: int, new_high: int) -> int:
    """Coins owed for raising the daily highwater from `prior` to `new_high`.
    Elo gained below LOW_ELO_THRESHOLD pays COINS_PER_NEW_ELO_LOW; at/above
    pays COINS_PER_NEW_ELO (which is double — the full-Maia tier earns 2x
    the sub-Maia rate per Elo point)."""
    if new_high <= prior:
        return 0
    low_end = min(new_high, LOW_ELO_THRESHOLD)
    low_gain = max(0, low_end - prior)
    high_gain = max(0, new_high - max(prior, LOW_ELO_THRESHOLD))
    return low_gain * COINS_PER_NEW_ELO_LOW + high_gain * COINS_PER_NEW_ELO

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
        payout = _payout_for_range(prior, bot_elo)
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
