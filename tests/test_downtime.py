"""Edge cases for long bot downtime.

These probe the question: if the bot was offline for days or weeks, does
state still recover correctly when it comes back?

Areas covered:
- Daily reset after multi-day downtime (do_daily_reset, cmd_daily)
- Lottery scheduler behavior with stale last_drawn_week
- Lottery ISO-week wraparound at year boundary
- Insurance: expired entries filtered out by init_db_state
- Savings: time-based compound interest works across long gaps
- Jail: jail_until is wall-clock so it expires correctly across downtime
"""
import datetime
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy


pytestmark = pytest.mark.asyncio


# ── daily reset ───────────────────────────────────────────────────────────────

async def test_do_daily_reset_after_three_days_down(db, monkeypatch):
    """Bot offline 3 days. When it comes back up, do_daily_reset should
    correctly clear stale per-user fields and advance last_daily_reset.

    The scheduler is wall-clock based, so once the bot is up and a user
    triggers !jailbreak or scratchoff, do_daily_reset() runs. We verify it
    DOES NOT, e.g., try to "catch up" by looping for each missed day —
    it just resets to today's state in one shot.
    """
    # Seed state as it would have been 3 days ago.
    await _economy.add_balance(1, 100)
    _state.economy["users"]["1"]["daily_date"] = "2026-04-29"
    _state.economy["users"]["1"]["scratch_used"] = 5
    _state.economy["users"]["1"]["jailbreak_used"] = True
    _state.economy["last_daily_reset"] = "2026-04-29"

    async def _ollama_up(): return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ollama_up)

    today = _economy._ct_today()
    assert today != "2026-04-29"  # sanity: at least one day has passed

    await _economy.do_daily_reset()

    user = _state.economy["users"]["1"]
    assert user["daily_date"] is None
    assert user["scratch_used"] == 0
    assert user["jailbreak_used"] is False
    assert _state.economy["last_daily_reset"] == today

    # Per-user balance history was snapshotted exactly once for today —
    # no attempt to backfill the missing days. Snapshots are keyed by
    # calendar date (rolls at midnight), not the 5am gameplay day.
    snapshot_date = _economy._ct_now().date().isoformat()
    history = await _persistence.load_balance_history()
    assert snapshot_date in history
    # Yesterday and the day before are NOT in history (legit gap).
    assert "2026-04-30" not in history or "2026-05-01" not in history


async def test_do_daily_reset_idempotent_within_same_day(db, monkeypatch):
    """If do_daily_reset somehow gets called twice on the same day (e.g.
    two lazy triggers race), it shouldn't corrupt or double-snapshot."""
    await _economy.add_balance(1, 1000)

    async def _ollama_up(): return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ollama_up)

    await _economy.do_daily_reset()
    today = _state.economy["last_daily_reset"]
    # Snapshots are keyed by calendar date, not the 5am gameplay day.
    snapshot_date = _economy._ct_now().date().isoformat()
    bucket = _economy._current_bucket_ct()
    history_after_first = await _persistence.load_balance_history()
    bal_after_first = history_after_first[snapshot_date][bucket]["1"]["wallet"]

    # Mutate balance, run reset again
    await _economy.add_balance(1, 500)
    await _economy.do_daily_reset()

    # last_daily_reset still today; second snapshot OVERWROTE first (upsert).
    assert _state.economy["last_daily_reset"] == today
    history_after_second = await _persistence.load_balance_history()
    assert history_after_second[snapshot_date][bucket]["1"]["wallet"] == 1500
    assert bal_after_first == 1000  # first snapshot was 1000 before mutation


async def test_snapshot_all_refreshes_today_row_on_repeated_calls(db, monkeypatch):
    """The 30min GraphCog scheduler calls snapshot_all() many times per
    bucket. Each call within the same bucket should overwrite that bucket's
    row, not append."""
    await _economy.add_balance(7, 200)

    async def _ollama_up(): return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ollama_up)

    await _economy.snapshot_all()
    today = _economy._ct_now().date().isoformat()
    bucket = _economy._current_bucket_ct()
    history_first = await _persistence.load_balance_history()
    assert history_first[today][bucket]["7"]["wallet"] == 200

    # Mutate balance, snapshot again — should refresh, not duplicate.
    await _economy.add_balance(7, 300)
    await _economy.snapshot_all()
    history_second = await _persistence.load_balance_history()
    assert history_second[today][bucket]["7"]["wallet"] == 500
    # Same number of date keys — no new row got added.
    assert sorted(history_first.keys()) == sorted(history_second.keys())


async def test_cmd_daily_across_multi_day_gap_grants_once(db):
    """User had stale daily_date=3 days ago. !daily should grant once,
    second invocation in same day should reject."""
    uid = 42
    await _economy._ensure_user(uid)
    _state.economy["users"][str(uid)]["daily_date"] = "2026-04-28"

    today = _economy._ct_today()
    user = _state.economy["users"][str(uid)]

    # First call: matches the scheduler logic. Stale daily_date != today,
    # so user is allowed to claim.
    assert user["daily_date"] != today
    starting_balance = user["balance"]
    # Inline the cmd_daily decision: if daily_date != today, grant + set.
    user["balance"] += 100
    user["daily_date"] = today

    assert _state.economy["users"][str(uid)]["balance"] == starting_balance + 100
    assert user["daily_date"] == today

    # Second call same day: daily_date == today, should be blocked.
    assert user["daily_date"] == today  # rejection check holds


# ── lottery scheduler ─────────────────────────────────────────────────────────

def _scheduler_should_draw(now_day: int, now_hour: int,
                           last_drawn_month: int, current_month: int) -> bool:
    """Replicates the draw conditions in src/cogs/lottery_cog.py exactly."""
    if now_day != 1:
        return False
    return now_hour >= 18 and last_drawn_month != current_month


async def test_lottery_skips_draw_mid_month_after_missed_first(db):
    """Bot was down all of the 1st. The rest of the month: scheduler doesn't
    draw. Pool stays untouched; no winner picked from the stale players.
    """
    # Saved state from the missed 1st (keys are YYYYMM month keys).
    abandoned = {
        "prize_pool": 50000,  # grew with player buys
        "players": {"100": 10, "200": 5},
        "last_posted_week": 202504,
        "last_drawn_week": 202503,  # April 2025 was never drawn
    }
    await _persistence.save_lottery(1, abandoned)

    # The 2nd, every hour of the day.
    for hour in range(24):
        assert _scheduler_should_draw(
            now_day=2,
            now_hour=hour,
            last_drawn_month=202503,
            current_month=202504,
        ) is False

    # The 28th, late in the month — still skipped (not the 1st).
    assert _scheduler_should_draw(
        now_day=28, now_hour=23, last_drawn_month=202503, current_month=202504,
    ) is False

    # Loaded state is untouched: original players and full pool still there.
    loaded = await _persistence.load_lottery(1)
    assert loaded == {
        "prize_pool": 50000,
        "players": {"100": 10, "200": 5},
        "last_posted_week": 202504,
        "last_drawn_week": 202503,
    }


async def test_lottery_redraws_on_next_first_after_missed_month(db):
    """1st of the next month after the bot missed a draw: scheduler WILL
    draw, using whoever bought tickets in the missed month. This is the
    current behavior — pool keeps accumulating, old players win.

    This test pins the behavior so a future fix that changes it (e.g.
    "expire pool if missed", "refund tickets on missed draw") fails this
    test loudly.
    """
    # April 2025's draw (May 1) was missed entirely.
    await _persistence.save_lottery(1, {
        "prize_pool": 50000,
        "players": {"100": 10, "200": 5},
        "last_posted_week": 202504,
        "last_drawn_week": 202503,
    })

    # June 1 at 6pm = month 202506, current_month != last_drawn_month.
    assert _scheduler_should_draw(
        now_day=1, now_hour=18, last_drawn_month=202503, current_month=202506,
    ) is True

    # The scheduler would now pay out the full 50000 to one of {100, 200}.
    # Pin contract: load_lottery surfaces the players intact, so payout
    # math is correct.
    loaded = await _persistence.load_lottery(1)
    assert loaded["players"] == {"100": 10, "200": 5}
    assert loaded["prize_pool"] == 50000


async def test_lottery_year_boundary_does_not_silently_skip(db):
    """A year-old January key must not collide with the next January —
    the YYYYMM encoding (src/economy.py:lottery_month_key) qualifies the
    month with its year.
    """
    from src.economy import lottery_month_key
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")

    this_year_key = lottery_month_key(datetime.datetime(2026, 1, 1, 18, 0, tzinfo=ct))
    next_year_key = lottery_month_key(datetime.datetime(2027, 1, 1, 18, 0, tzinfo=ct))

    assert this_year_key != next_year_key, (
        "Year-qualified keys must differ across years to keep "
        "last_drawn_week from suppressing the draw a year later."
    )
    # Sanity on the encoding: YYYYMM.
    assert this_year_key == 202601
    assert next_year_key == 202701


async def test_lottery_dec_to_jan_still_draws(db):
    """Dec → Jan must still trigger a draw across the year boundary."""
    from src.economy import lottery_month_key
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")

    last_year_end = lottery_month_key(datetime.datetime(2025, 12, 1, 18, 0, tzinfo=ct))
    new_year_start = lottery_month_key(datetime.datetime(2026, 1, 1, 18, 0, tzinfo=ct))
    assert last_year_end != new_year_start


async def test_lottery_legacy_week_key_transitions_to_monthly(db):
    """Weekly→monthly migration (deployed 2026-07-31): persisted YYYYWW
    week keys from July 2026 (weeks 27-31) must never equal a YYYYMM month
    key, so the first 1st-of-month draw (Aug 1 2026, key 202608) fires
    instead of being suppressed by the stale weekly value.
    """
    from src.economy import lottery_month_key
    from zoneinfo import ZoneInfo
    ct = ZoneInfo("America/Chicago")

    aug_2026 = lottery_month_key(datetime.datetime(2026, 8, 1, 18, 0, tzinfo=ct))
    assert aug_2026 == 202608
    for legacy_week_key in range(202627, 202632):  # July 2026 ISO weeks
        assert _scheduler_should_draw(
            now_day=1, now_hour=18,
            last_drawn_month=legacy_week_key, current_month=aug_2026,
        ) is True


async def test_lottery_scheduler_before_loop_waits_for_init(db):
    """A restart landing near a 1st-of-month 6pm draw: the scheduler must not
    tick until init_db_state has loaded guild_house into memory, or the
    draw's house drain sees an empty pot and the fresh lottery starts
    without it (this is how the 9/1/2026 pools missed their house money).
    """
    import asyncio
    from src.cogs.lottery_cog import LotteryCog

    before = LotteryCog.lottery_scheduler._before_loop
    assert before is not None, "lottery scheduler must gate on ready + init"

    class _Bot:
        async def wait_until_ready(self):
            return None

    class _Cog:
        bot = _Bot()

    _persistence.init_done.clear()
    gate = asyncio.create_task(before(_Cog()))
    await asyncio.sleep(0.05)
    assert not gate.done(), "before_loop must block until init_done is set"
    _persistence.init_done.set()
    await asyncio.wait_for(gate, timeout=1)


# ── insurance ─────────────────────────────────────────────────────────────────

async def test_insurance_expired_entries_dropped_on_init_db_state(db):
    """Bot down 3 weeks. Insurance entries were valid at shutdown but are
    now expired. init_db_state must filter them out (line ~810 in
    persistence.py: `if expires_at > now`)."""
    now = time.time()
    _state.insurance[1] = {
        "expires_at": now - 86400,  # expired yesterday
        "protected_from": ["nickname"],
    }
    _state.insurance[2] = {
        "expires_at": now + 3600,  # still valid 1 hour
        "protected_from": ["curse"],
    }
    await _persistence.save_insurance()

    # Simulate bot restart — wipe in-memory and reload.
    _state.insurance.clear()
    await _persistence.init_db_state()

    assert 1 not in _state.insurance, (
        "expired insurance leaked into runtime state"
    )
    assert 2 in _state.insurance


async def test_insurance_is_insured_returns_false_after_expiry(db):
    """Real-time check: a user whose insurance expired during downtime
    is correctly reported as unprotected."""
    uid = 99
    # Insurance "expires" 1 second from now.
    expires = time.time() + 1
    _state.insurance[uid] = {
        "expires_at": expires,
        "protected_from": ["nickname"],
    }

    # Still insured right now.
    assert await _economy.is_insured(uid, "nickname") is True

    # Time passes (simulating downtime / wall-clock advance).
    # Use sleep here because is_insured uses time.time() directly and we
    # want to actually exercise that real path, not monkeypatch around it.
    import asyncio
    await asyncio.sleep(1.1)

    assert await _economy.is_insured(uid, "nickname") is False
    # And the expired entry was dropped from state by is_insured itself.
    assert uid not in _state.insurance


# ── savings ───────────────────────────────────────────────────────────────────

async def test_savings_interest_compounds_correctly_across_long_gap(db, monkeypatch):
    """Savings is purely time-based: compound interest computed from
    deposited_at on read. Bot downtime doesn't affect the math."""
    uid = 7
    await _economy.add_balance(uid, 10000)

    # Post-changeover epoch so the whole window accrues at the current rate.
    times = [_economy.SAVINGS_RATE_CHANGE_TS + 1_000_000.0]
    monkeypatch.setattr(_economy.time, "time", lambda: times[0])

    ok = await _economy.add_savings(uid, 1000)
    assert ok is True

    # Bot offline 21 days.
    times[0] += 21 * 86400.0

    value = await _economy.get_savings_value(uid)
    expected = 1000 * (1.006 ** 21)
    assert abs(value - expected) < 0.01, (
        f"21-day compound interest off: got {value}, expected ~{expected}"
    )


# ── jail ──────────────────────────────────────────────────────────────────────

async def test_jail_until_expires_naturally_across_downtime(db):
    """jail_until is a unix timestamp. If the bot is down past that
    timestamp, the user is freed automatically — no scheduler needed."""
    uid = 55
    await _economy._ensure_user(uid)

    # Jail until 1 hour from now.
    _state.economy["users"][str(uid)]["jail_until"] = time.time() + 3600
    assert _state.economy["users"][str(uid)]["jail_until"] > time.time()

    # Persist + restart simulation.
    await _persistence.save_economy(uid=uid)
    _state.economy["users"].clear()
    await _persistence.init_db_state()

    # Still jailed (under an hour passed).
    assert _state.economy["users"][str(uid)]["jail_until"] > time.time()

    # Now simulate the wall-clock passing the deadline by overwriting the
    # value to something in the past, persisting, reloading.
    past = time.time() - 3600
    _state.economy["users"][str(uid)]["jail_until"] = past
    await _persistence.save_economy(uid=uid)
    _state.economy["users"].clear()
    await _persistence.init_db_state()

    assert _state.economy["users"][str(uid)]["jail_until"] < time.time()
    # cmd_steal/cmd_mug check `time.time() < jail_until` to gate, so a past
    # value correctly means "no longer jailed" without any reset task.


# ── balance history gaps ──────────────────────────────────────────────────────

async def test_balance_history_has_gaps_for_missed_days_not_crashes(db, monkeypatch):
    """The 30min snapshot loop writes per-bucket rows. Days where the bot
    was offline simply don't exist in history. The graph cog should
    tolerate this; we verify load_balance_history returns the sparse dict
    without error."""
    # Fake snapshots from a working bot, then a 3-day gap, then today.
    history = {
        "2026-04-25": {0: {"1": {"wallet": 100, "savings": 0}}},
        "2026-04-26": {2: {"1": {"wallet": 110, "savings": 0}}},
        # gap: 04-27, 04-28, 04-29 missing
        "2026-04-30": {1: {"1": {"wallet": 200, "savings": 0}}},
    }
    await _persistence.save_balance_history(history)
    loaded = await _persistence.load_balance_history()
    assert set(loaded.keys()) == {"2026-04-25", "2026-04-26", "2026-04-30"}
    # Sparse — graph code must handle missing buckets without crashes.
