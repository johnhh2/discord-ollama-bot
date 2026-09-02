"""Payout + record-setting when a human beats the chess bot.

Daily-resetting highwater: each user has a "highest bot Elo defeated today"
field on their economy row. The payout rate doubles at the sub-Maia / Maia
boundary (Elo 1100):
  - Sub-Maia tier (Elo 100-1000): 5 coins per new Elo point.
  - Maia / Stockfish tier (Elo 1100+): 10 coins per new Elo point.

Daily reset (5am CT) zeroes it out so the same Elo can be beaten again
tomorrow for full credit.

Also the home of the all-time chess-only ranks (max single Elo defeated,
cumulative Elo defeated — see chess_ranks/rank_badges) and the once-ever
first-defeat bonus per 1100+ Elo bin, both persisted in chess_user_stats.

This module is intentionally thin: pure calculation + state mutation + DB
write. The cog (_finalize_game) decides WHEN to call award_bot_defeat. The
record-setting category lives in src/helpers.py:RECORD_LABELS for display.
"""
from __future__ import annotations

from src import state
from src.economy import _ct_today, add_balance, _ensure_user
from src.persistence import save_chess_user_stats, save_economy, try_set_record


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

# One-time first-defeat bonus, per Elo bin at/above the Maia boundary: the
# first time a user EVER beats the bot at a given bin (1100, 1200, ...) they
# earn FIRST_DEFEAT_BONUS once. Unlike the daily highwater above, claimed
# bins never reset — they persist in chess_user_stats.bonus_bins.
FIRST_DEFEAT_BONUS = 100_000
FIRST_DEFEAT_BONUS_MIN_ELO = 1100

# Chess-only rank emoji (shown in !profile and, compactly, in match embeds).
# Unicode has no red trophy, so the sports medal (red ribbon) stands in for
# "max single Elo defeated" next to the gold trophy's "cumulative Elo
# defeated".
RANK_MAX_EMOJI = "🏅"
RANK_TOTAL_EMOJI = "🏆"


def _stats_row(user_id: int) -> dict:
    """Materialize (or return) the user's chess_user_stats row in state."""
    return state.chess_user_stats.setdefault(str(user_id), {
        "max_elo_defeated": 0,
        "total_elo_defeated": 0,
        "bonus_bins": set(),
        "elo_spent": 0,
    })


def chess_ranks(user_id: int) -> tuple[int, int]:
    """(max_elo_defeated, total_elo_defeated), both 0 for a user who has
    never beaten the bot. Read-only: does not materialize a row."""
    row = state.chess_user_stats.get(str(user_id))
    if not row:
        return 0, 0
    return int(row.get("max_elo_defeated", 0)), int(row.get("total_elo_defeated", 0))


def chess_elo_balance(user_id: int) -> int:
    """Spendable chess Elo: lifetime total_elo_defeated minus what the user
    has consumed unlocking chess-shop items. total_elo_defeated itself is a
    monotonic lifetime stat (ranks, records, profile) and is never
    decremented — spending only ever raises elo_spent."""
    row = state.chess_user_stats.get(str(user_id))
    if not row:
        return 0
    return int(row.get("total_elo_defeated", 0)) - int(row.get("elo_spent", 0))


def spend_chess_elo(user_id: int, amount: int) -> bool:
    """Consume `amount` spendable Elo. Synchronous gate-and-claim (no await
    between the balance check and the mutation), so concurrent purchases
    can't double-spend — see CLAUDE.md on per-user command races. The caller
    persists with save_chess_user_stats."""
    if amount < 0:
        return False
    if chess_elo_balance(user_id) < amount:
        return False
    _stats_row(user_id)["elo_spent"] = (
        int(_stats_row(user_id).get("elo_spent", 0)) + amount
    )
    return True


def refund_chess_elo(user_id: int, amount: int) -> None:
    """Roll back a spend_chess_elo claim after a failed purchase step.
    Never drives elo_spent negative."""
    row = _stats_row(user_id)
    row["elo_spent"] = max(0, int(row.get("elo_spent", 0)) - max(0, amount))


async def award_pvp_defeat(*, user_id: int, opponent_est_elo: int) -> int:
    """Credit a PvP win with spendable chess Elo: the defeated opponent's
    engine-estimated strength this game goes into total_elo_defeated (the
    🏆 cumulative rank, spendable in !chess shop) but never
    max_elo_defeated — the 🏅 rank stays a bot-ladder achievement.
    Returns the amount credited (0 when there's no usable estimate)."""
    gain = int(opponent_est_elo or 0)
    if gain <= 0:
        return 0
    row = _stats_row(user_id)
    row["total_elo_defeated"] = int(row.get("total_elo_defeated", 0)) + gain
    await save_chess_user_stats(user_id)
    return gain


def claimed_bonus_bins(user_id: int) -> set[int]:
    """Elo bins whose one-time first-defeat bonus this user has claimed."""
    row = state.chess_user_stats.get(str(user_id))
    if not row:
        return set()
    return set(row.get("bonus_bins", ()))


def _compact_int(n: int) -> str:
    """45,300 → '45.3k' style compaction for the in-match badge line."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"
    if n >= 10_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n:,}"


def rank_badges(user_id: int) -> str:
    """Very compact rank string for the match embeds ('🏅1,900 🏆45.3k'),
    or '' for a user who has never beaten the bot."""
    max_elo, total_elo = chess_ranks(user_id)
    if max_elo <= 0:
        return ""
    return f"{RANK_MAX_EMOJI}{max_elo:,} {RANK_TOTAL_EMOJI}{_compact_int(total_elo)}"


def _todays_highwater(user: dict, today: str) -> int:
    """Read the user's highest bot-Elo-defeated for `today`, treating a
    stale stored date as 0. Self-heals without depending on do_daily_reset
    having fired."""
    if user.get("bot_chess_elo_max_date") != today:
        return 0
    return int(user.get("bot_chess_elo_max_today", 0) or 0)


async def award_bot_defeat(
    *, user_id: int, guild_id: int | None, holder_name: str, bot_elo: int,
) -> tuple[int, bool, int]:
    """Apply the daily-highwater payout + rank updates + first-defeat bonus,
    then try to set the global record.

    Returns (payout_coins, record_broken, first_defeat_bonus):
      payout_coins: 0 if no new ground was gained today.
      record_broken: True iff this win set a new per-guild high.
      first_defeat_bonus: FIRST_DEFEAT_BONUS the first time this user ever
        beats the bot at this Elo bin (1100+), else 0.

    No payout for sub-1 Elo gains; idempotent if called twice with the same
    bot_elo on the same day (the bonus is once-ever per bin). Caller is
    responsible for not invoking this on losses, draws, or human-vs-human
    games.
    """
    if bot_elo <= 0:
        return 0, False, 0
    await _ensure_user(user_id)
    today = _ct_today()
    user = state.economy["users"][str(user_id)]

    # ── Chess-only ranks + first-defeat bonus ────────────────────────────
    # Gate-and-claim runs synchronously (no await between the bin check and
    # the add), so two concurrent finalizes can't both claim the same bin.
    stats = _stats_row(user_id)
    stats["max_elo_defeated"] = max(int(stats.get("max_elo_defeated", 0)), bot_elo)
    stats["total_elo_defeated"] = int(stats.get("total_elo_defeated", 0)) + bot_elo
    bins = stats.setdefault("bonus_bins", set())
    first_defeat_bonus = 0
    if bot_elo >= FIRST_DEFEAT_BONUS_MIN_ELO and bot_elo not in bins:
        bins.add(bot_elo)
        first_defeat_bonus = FIRST_DEFEAT_BONUS
    await save_chess_user_stats(user_id)
    if first_defeat_bonus > 0:
        await add_balance(user_id, first_defeat_bonus)

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

    return payout, record_broken, first_defeat_bonus
