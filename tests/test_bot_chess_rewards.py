"""Bot-defeat bounty + per-day highwater tests."""
import pytest

import src.state as _state
import src.persistence as _persistence
from src.games import bot_chess_rewards as br
from src.economy import _ct_today, get_balance


_aio = pytest.mark.asyncio


async def _user_dict(uid: int) -> dict:
    return _state.economy["users"][str(uid)]


# ─────────────────────────────────────────────────────────────────────────────
# Pure unit tests for award_bot_defeat
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_first_win_pays_full_elo_times_20(db):
    payout, record_broken = await br.award_bot_defeat(
        user_id=5001, guild_id=42, holder_name="Alice", bot_elo=400,
    )
    assert payout == 400 * br.COINS_PER_NEW_ELO  # 8000
    assert record_broken is True
    assert await get_balance(5001) == 8000

    u = await _user_dict(5001)
    assert u["bot_chess_elo_max_today"] == 400
    assert u["bot_chess_elo_max_date"] == _ct_today()


@_aio
async def test_second_win_higher_elo_pays_only_delta(db):
    """After beating 400, beating 500 the same day pays just (500-400)*20."""
    await br.award_bot_defeat(user_id=5002, guild_id=42, holder_name="Alice", bot_elo=400)
    bal_after_first = await get_balance(5002)

    payout, _ = await br.award_bot_defeat(
        user_id=5002, guild_id=42, holder_name="Alice", bot_elo=500,
    )
    assert payout == (500 - 400) * br.COINS_PER_NEW_ELO  # 2000
    assert await get_balance(5002) == bal_after_first + 2000

    u = await _user_dict(5002)
    assert u["bot_chess_elo_max_today"] == 500


@_aio
async def test_second_win_lower_elo_pays_nothing(db):
    """After beating 500, beating 400 same day pays 0."""
    await br.award_bot_defeat(user_id=5003, guild_id=42, holder_name="Alice", bot_elo=500)
    bal_after_first = await get_balance(5003)

    payout, _ = await br.award_bot_defeat(
        user_id=5003, guild_id=42, holder_name="Alice", bot_elo=400,
    )
    assert payout == 0
    assert await get_balance(5003) == bal_after_first
    # Highwater unchanged.
    u = await _user_dict(5003)
    assert u["bot_chess_elo_max_today"] == 500


@_aio
async def test_idempotent_same_elo_same_day(db):
    """Beating 600 twice in a row pays once."""
    p1, _ = await br.award_bot_defeat(
        user_id=5004, guild_id=42, holder_name="Alice", bot_elo=600,
    )
    p2, _ = await br.award_bot_defeat(
        user_id=5004, guild_id=42, holder_name="Alice", bot_elo=600,
    )
    assert p1 == 600 * br.COINS_PER_NEW_ELO
    assert p2 == 0


@_aio
async def test_stale_date_self_heals_to_zero(db):
    """If the stored date is yesterday (or older), the read treats max as 0,
    so the next win pays out fully."""
    # Pre-seed a stale row (yesterday's date, prior highwater 800).
    await _persistence.init_done.wait() if False else None  # no-op; just for shape parity
    from src.economy import _ensure_user
    await _ensure_user(5005)
    u = _state.economy["users"]["5005"]
    u["bot_chess_elo_max_today"] = 800
    u["bot_chess_elo_max_date"] = "1999-01-01"  # ancient

    payout, _ = await br.award_bot_defeat(
        user_id=5005, guild_id=42, holder_name="Alice", bot_elo=600,
    )
    # 600 from a treated-as-0 baseline pays 600*20.
    assert payout == 600 * br.COINS_PER_NEW_ELO
    u = await _user_dict(5005)
    assert u["bot_chess_elo_max_today"] == 600
    assert u["bot_chess_elo_max_date"] == _ct_today()


@_aio
async def test_zero_or_negative_elo_pays_nothing(db):
    """Defensive: nonsensical bot_elo values are no-ops."""
    p1, r1 = await br.award_bot_defeat(user_id=5006, guild_id=42, holder_name="A", bot_elo=0)
    p2, r2 = await br.award_bot_defeat(user_id=5006, guild_id=42, holder_name="A", bot_elo=-100)
    assert p1 == 0 and r1 is False
    assert p2 == 0 and r2 is False
    assert await get_balance(5006) == 0


@_aio
async def test_record_broken_signaled_on_new_high(db):
    """try_set_record returns True on a new per-guild high, and that bubbles
    up as the second tuple element."""
    _, broken_first = await br.award_bot_defeat(
        user_id=5007, guild_id=99, holder_name="A", bot_elo=1000,
    )
    assert broken_first is True

    # Same guild, lower elo from a different user — not a new record.
    _, broken_lower = await br.award_bot_defeat(
        user_id=5008, guild_id=99, holder_name="B", bot_elo=500,
    )
    assert broken_lower is False

    # Same guild, higher elo from another user — new record.
    _, broken_higher = await br.award_bot_defeat(
        user_id=5009, guild_id=99, holder_name="C", bot_elo=1500,
    )
    assert broken_higher is True


@_aio
async def test_no_guild_skips_record_no_crash(db):
    """guild_id=None (e.g., DM context) doesn't crash; payout still works."""
    payout, broken = await br.award_bot_defeat(
        user_id=5010, guild_id=None, holder_name="Solo", bot_elo=300,
    )
    assert payout == 300 * br.COINS_PER_NEW_ELO
    assert broken is False  # no guild → no record


# ─────────────────────────────────────────────────────────────────────────────
# Persistence: columns round-trip + daily-reset clears them
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_bot_chess_elo_columns_roundtrip(db):
    """Save with non-default values, clear state, reload via init_db_state,
    and assert the columns come back."""
    today = _ct_today()
    from src.economy import _ensure_user
    await _ensure_user(5100)
    u = _state.economy["users"]["5100"]
    u["bot_chess_elo_max_today"] = 1200
    u["bot_chess_elo_max_date"] = today
    await _persistence.save_economy(uid=5100)

    _state.economy["users"].clear()
    _persistence._init_db_state_done = False
    await _persistence.init_db_state()

    reloaded = _state.economy["users"]["5100"]
    assert reloaded["bot_chess_elo_max_today"] == 1200
    assert reloaded["bot_chess_elo_max_date"] == today


@_aio
async def test_daily_reset_clears_bot_chess_highwater(db):
    """do_daily_reset zeroes the highwater and stamps today's date."""
    from src.economy import _ensure_user, do_daily_reset
    await _ensure_user(5101)
    u = _state.economy["users"]["5101"]
    u["bot_chess_elo_max_today"] = 2000
    u["bot_chess_elo_max_date"] = "2020-01-01"  # ancient

    await do_daily_reset()

    u = _state.economy["users"]["5101"]
    assert u["bot_chess_elo_max_today"] == 0
    assert u["bot_chess_elo_max_date"] == _ct_today()
