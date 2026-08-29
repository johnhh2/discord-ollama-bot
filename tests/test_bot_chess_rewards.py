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
async def test_first_win_pays_full_elo_times_low_rate(db):
    payout, record_broken, _ = await br.award_bot_defeat(
        user_id=5001, guild_id=42, holder_name="Alice", bot_elo=400,
    )
    # 400 Elo all below the 1100 threshold → 400 * COINS_PER_NEW_ELO_LOW.
    expected = 400 * br.COINS_PER_NEW_ELO_LOW
    assert payout == expected
    assert record_broken is True
    assert await get_balance(5001) == expected

    u = await _user_dict(5001)
    assert u["bot_chess_elo_max_today"] == 400
    assert u["bot_chess_elo_max_date"] == _ct_today()


@_aio
async def test_second_win_higher_elo_pays_only_delta(db):
    """After beating 400, beating 500 the same day pays just (500-400) *
    COINS_PER_NEW_ELO_LOW (both below the threshold)."""
    await br.award_bot_defeat(user_id=5002, guild_id=42, holder_name="Alice", bot_elo=400)
    bal_after_first = await get_balance(5002)

    payout, _, _ = await br.award_bot_defeat(
        user_id=5002, guild_id=42, holder_name="Alice", bot_elo=500,
    )
    expected = (500 - 400) * br.COINS_PER_NEW_ELO_LOW
    assert payout == expected
    assert await get_balance(5002) == bal_after_first + expected

    u = await _user_dict(5002)
    assert u["bot_chess_elo_max_today"] == 500


@_aio
async def test_second_win_lower_elo_pays_nothing(db):
    """After beating 500, beating 400 same day pays 0."""
    await br.award_bot_defeat(user_id=5003, guild_id=42, holder_name="Alice", bot_elo=500)
    bal_after_first = await get_balance(5003)

    payout, _, _ = await br.award_bot_defeat(
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
    p1, _, _ = await br.award_bot_defeat(
        user_id=5004, guild_id=42, holder_name="Alice", bot_elo=600,
    )
    p2, _, _ = await br.award_bot_defeat(
        user_id=5004, guild_id=42, holder_name="Alice", bot_elo=600,
    )
    assert p1 == 600 * br.COINS_PER_NEW_ELO_LOW
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

    payout, _, _ = await br.award_bot_defeat(
        user_id=5005, guild_id=42, holder_name="Alice", bot_elo=600,
    )
    # 600 from a treated-as-0 baseline pays 600*10 (all below 1000).
    assert payout == 600 * br.COINS_PER_NEW_ELO_LOW
    u = await _user_dict(5005)
    assert u["bot_chess_elo_max_today"] == 600
    assert u["bot_chess_elo_max_date"] == _ct_today()


@_aio
async def test_zero_or_negative_elo_pays_nothing(db):
    """Defensive: nonsensical bot_elo values are no-ops."""
    p1, r1, b1 = await br.award_bot_defeat(user_id=5006, guild_id=42, holder_name="A", bot_elo=0)
    p2, r2, b2 = await br.award_bot_defeat(user_id=5006, guild_id=42, holder_name="A", bot_elo=-100)
    assert p1 == 0 and r1 is False and b1 == 0
    assert p2 == 0 and r2 is False and b2 == 0
    assert await get_balance(5006) == 0


@_aio
async def test_record_broken_signaled_on_new_high(db):
    """try_set_record returns True on a new per-guild high, and that bubbles
    up as the second tuple element."""
    _, broken_first, _ = await br.award_bot_defeat(
        user_id=5007, guild_id=99, holder_name="A", bot_elo=1000,
    )
    assert broken_first is True

    # Same guild, lower elo from a different user — not a new record.
    _, broken_lower, _ = await br.award_bot_defeat(
        user_id=5008, guild_id=99, holder_name="B", bot_elo=500,
    )
    assert broken_lower is False

    # Same guild, higher elo from another user — new record.
    _, broken_higher, _ = await br.award_bot_defeat(
        user_id=5009, guild_id=99, holder_name="C", bot_elo=1500,
    )
    assert broken_higher is True


@_aio
async def test_payout_splits_across_low_and_high_threshold(db):
    """First win at 1200 from a zero baseline pays LOW_ELO_THRESHOLD points
    at the low rate plus the remainder at the high rate."""
    win_elo = 1200
    payout, _, bonus = await br.award_bot_defeat(
        user_id=5020, guild_id=42, holder_name="A", bot_elo=win_elo,
    )
    assert bonus == br.FIRST_DEFEAT_BONUS  # first-ever 1200 win
    expected = (
        br.LOW_ELO_THRESHOLD * br.COINS_PER_NEW_ELO_LOW
        + (win_elo - br.LOW_ELO_THRESHOLD) * br.COINS_PER_NEW_ELO
    )
    assert payout == expected


@_aio
async def test_payout_above_threshold_only_uses_high_rate(db):
    """After beating 1200, beating 1500 same day pays the delta at the high
    rate only (both above the threshold)."""
    await br.award_bot_defeat(user_id=5021, guild_id=42, holder_name="A", bot_elo=1200)
    bal = await get_balance(5021)
    payout, _, bonus = await br.award_bot_defeat(
        user_id=5021, guild_id=42, holder_name="A", bot_elo=1500,
    )
    assert payout == 300 * br.COINS_PER_NEW_ELO
    # Balance also gains the first-ever-1500 defeat bonus alongside the bounty.
    assert bonus == br.FIRST_DEFEAT_BONUS
    assert await get_balance(5021) == bal + payout + bonus


@_aio
async def test_payout_delta_straddles_threshold(db):
    """A delta that straddles the threshold pays the portion below it at the
    low rate, the portion above at the high rate."""
    prior_elo = 800
    new_elo = 1300
    await br.award_bot_defeat(user_id=5022, guild_id=42, holder_name="A", bot_elo=prior_elo)
    bal = await get_balance(5022)
    payout, _, bonus = await br.award_bot_defeat(
        user_id=5022, guild_id=42, holder_name="A", bot_elo=new_elo,
    )
    low_gain = br.LOW_ELO_THRESHOLD - prior_elo
    high_gain = new_elo - br.LOW_ELO_THRESHOLD
    expected = low_gain * br.COINS_PER_NEW_ELO_LOW + high_gain * br.COINS_PER_NEW_ELO
    assert payout == expected
    # 1300 is a first-ever 1100+ bin defeat, so the bonus lands too.
    assert bonus == br.FIRST_DEFEAT_BONUS
    assert await get_balance(5022) == bal + expected + bonus


@_aio
async def test_no_guild_skips_record_no_crash(db):
    """guild_id=None (e.g., DM context) doesn't crash; payout still works."""
    payout, broken, _ = await br.award_bot_defeat(
        user_id=5010, guild_id=None, holder_name="Solo", bot_elo=300,
    )
    assert payout == 300 * br.COINS_PER_NEW_ELO_LOW
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


# ─────────────────────────────────────────────────────────────────────────────
# Chess-only ranks + first-defeat bonuses (chess_user_stats)
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_ranks_accumulate_max_and_total(db):
    """max = best single Elo defeated; total = cumulative sum over all wins."""
    await br.award_bot_defeat(user_id=5200, guild_id=42, holder_name="A", bot_elo=800)
    await br.award_bot_defeat(user_id=5200, guild_id=42, holder_name="A", bot_elo=1500)
    await br.award_bot_defeat(user_id=5200, guild_id=42, holder_name="A", bot_elo=1200)
    assert br.chess_ranks(5200) == (1500, 800 + 1500 + 1200)


@_aio
async def test_first_defeat_bonus_once_per_bin(db):
    """Each 1100+ bin pays FIRST_DEFEAT_BONUS exactly once, ever — including
    a lower bin beaten after a higher one (which earns no daily bounty)."""
    _, _, b1 = await br.award_bot_defeat(user_id=5201, guild_id=42, holder_name="A", bot_elo=1200)
    _, _, b2 = await br.award_bot_defeat(user_id=5201, guild_id=42, holder_name="A", bot_elo=1200)
    bal_before = await get_balance(5201)
    p3, _, b3 = await br.award_bot_defeat(user_id=5201, guild_id=42, holder_name="A", bot_elo=1100)
    assert b1 == br.FIRST_DEFEAT_BONUS
    assert b2 == 0
    # 1100 is below today's highwater (no bounty) but a fresh bin (bonus).
    assert p3 == 0 and b3 == br.FIRST_DEFEAT_BONUS
    assert await get_balance(5201) == bal_before + br.FIRST_DEFEAT_BONUS
    assert br.claimed_bonus_bins(5201) == {1100, 1200}


@_aio
async def test_no_first_defeat_bonus_below_1100(db):
    _, _, bonus = await br.award_bot_defeat(user_id=5202, guild_id=42, holder_name="A", bot_elo=1000)
    assert bonus == 0
    assert br.claimed_bonus_bins(5202) == set()
    assert br.chess_ranks(5202) == (1000, 1000)


@_aio
async def test_concurrent_first_defeat_bonus_claimed_once(db, monkeypatch):
    """Two interleaved wins at the same 1100+ bin pay the bonus once: the bin
    is claimed synchronously before the first await in the claim window."""
    import asyncio

    real_save = br.save_chess_user_stats

    async def _yielding_save(uid):
        await asyncio.sleep(0)  # force a real event-loop yield
        await real_save(uid)

    monkeypatch.setattr(br, "save_chess_user_stats", _yielding_save)
    results = await asyncio.gather(
        br.award_bot_defeat(user_id=5203, guild_id=42, holder_name="A", bot_elo=1200),
        br.award_bot_defeat(user_id=5203, guild_id=42, holder_name="A", bot_elo=1200),
    )
    assert sorted(r[2] for r in results) == [0, br.FIRST_DEFEAT_BONUS]
    assert br.chess_ranks(5203) == (1200, 2400)


@_aio
async def test_chess_user_stats_roundtrip(db):
    """Stats survive a state wipe + init_db_state reload."""
    await br.award_bot_defeat(user_id=5204, guild_id=42, holder_name="A", bot_elo=1200)
    await br.award_bot_defeat(user_id=5204, guild_id=42, holder_name="A", bot_elo=1100)

    _state.chess_user_stats.clear()
    _persistence._init_db_state_done = False
    await _persistence.init_db_state()

    row = _state.chess_user_stats["5204"]
    assert row["max_elo_defeated"] == 1200
    assert row["total_elo_defeated"] == 2300
    assert row["bonus_bins"] == {1100, 1200}


def test_rank_badges_formatting():
    """No stats → empty string; otherwise medal=max, trophy=compacted total."""
    assert br.rank_badges(777001) == ""
    _state.chess_user_stats["777002"] = {
        "max_elo_defeated": 1900, "total_elo_defeated": 45300, "bonus_bins": set(),
    }
    assert br.rank_badges(777002) == f"{br.RANK_MAX_EMOJI}1,900 {br.RANK_TOTAL_EMOJI}45.3k"
    _state.chess_user_stats["777003"] = {
        "max_elo_defeated": 400, "total_elo_defeated": 400, "bonus_bins": set(),
    }
    assert br.rank_badges(777003) == f"{br.RANK_MAX_EMOJI}400 {br.RANK_TOTAL_EMOJI}400"
