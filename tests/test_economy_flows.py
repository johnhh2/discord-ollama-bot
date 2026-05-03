"""Economy flow tests: savings interest, daily reset, balance ops.

These exercise the real persistence path (`db` fixture) so the assertions
validate both in-memory state AND what was written to the DB.
"""
import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy


pytestmark = pytest.mark.asyncio


# ── savings ───────────────────────────────────────────────────────────────────

async def test_savings_compound_interest_30_days(db, monkeypatch):
    uid = 8001
    await _economy.add_balance(uid, 1000)

    # Freeze "now" for the deposit at t=0.
    times = [0.0]
    def fake_time():
        return times[0]
    monkeypatch.setattr(_economy.time, "time", fake_time)

    ok = await _economy.add_savings(uid, 100)
    assert ok is True
    assert await _economy.get_balance(uid) == 900  # deducted

    # Advance 30 days.
    times[0] = 30 * 86400.0
    value = await _economy.get_savings_value(uid)
    expected = 100 * (1.01 ** 30)
    assert abs(value - expected) < 0.01


async def test_savings_insufficient_funds_returns_false(db, monkeypatch):
    uid = 8002
    await _economy.add_balance(uid, 50)

    monkeypatch.setattr(_economy.time, "time", lambda: 0.0)
    ok = await _economy.add_savings(uid, 100)
    assert ok is False
    assert await _economy.get_balance(uid) == 50  # untouched


async def test_remove_savings_partial_fifo(db, monkeypatch):
    uid = 8003
    await _economy.add_balance(uid, 1000)

    times = [0.0]
    monkeypatch.setattr(_economy.time, "time", lambda: times[0])

    # Two deposits.
    await _economy.add_savings(uid, 100)
    times[0] = 86400.0  # one day later
    await _economy.add_savings(uid, 100)

    # Withdraw 50 — should drain from the older deposit first.
    times[0] = 2 * 86400.0
    ok = await _economy.remove_savings(uid, 50)
    assert ok is True

    deposits = _state.economy["users"][str(uid)]["savings"]
    # Older deposit (deposited_at=0.0) should be partially drained or gone;
    # newer one (deposited_at=86400.0) should still be intact.
    newer = [d for d in deposits if d["deposited_at"] == 86400.0]
    assert len(newer) == 1
    assert newer[0]["amount"] == 100


async def test_remove_savings_more_than_available_returns_false(db, monkeypatch):
    uid = 8004
    await _economy.add_balance(uid, 1000)
    monkeypatch.setattr(_economy.time, "time", lambda: 0.0)
    await _economy.add_savings(uid, 100)

    ok = await _economy.remove_savings(uid, 99999)
    assert ok is False
    # Deposits still there.
    assert len(_state.economy["users"][str(uid)]["savings"]) == 1


# ── daily reset ───────────────────────────────────────────────────────────────

async def test_do_daily_reset_resets_user_fields_and_persists(db, monkeypatch):
    # Three users in stale post-claim state.
    for uid in (9001, 9002, 9003):
        await _economy._ensure_user(uid)
        u = _state.economy["users"][str(uid)]
        u["daily_date"] = "2026-01-01"
        u["scratch_used"] = 5
        u["scratch_date"] = "2026-01-01"
        u["jailbreak_used"] = True

    # Stub Ollama check (do_daily_reset awaits it).
    async def _ollama_up():
        return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ollama_up)

    await _economy.do_daily_reset()

    # In-memory: every user's fields reset.
    today = _economy._ct_today()
    for uid in (9001, 9002, 9003):
        u = _state.economy["users"][str(uid)]
        assert u["daily_date"] is None
        assert u["scratch_used"] == 0
        assert u["scratch_date"] == today
        assert u["jailbreak_used"] is False
    assert _state.economy["last_daily_reset"] == today

    # Persisted: clear in-memory state and reload.
    _state.economy["users"].clear()
    _state.economy["last_daily_reset"] = None
    await _persistence.init_db_state()

    for uid in (9001, 9002, 9003):
        u = _state.economy["users"][str(uid)]
        assert u["daily_date"] is None
        assert u["scratch_used"] == 0
        assert u["jailbreak_used"] is False
    assert _state.economy["last_daily_reset"] == today


# ── balance operations ────────────────────────────────────────────────────────

async def test_add_balance_persists_immediately(db):
    uid = 1234
    await _economy.add_balance(uid, 500)
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT balance FROM economy_users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
    assert row[0] == 500


async def test_deduct_balance_returns_false_on_insufficient_funds(db):
    uid = 1235
    await _economy.add_balance(uid, 100)
    ok = await _economy.deduct_balance(uid, 200)
    assert ok is False
    assert await _economy.get_balance(uid) == 100


async def test_guild_house_drain_into_lottery(db):
    """drain_bot_balance_into_lottery: house emptied, prize_pool gained."""
    await _economy.add_guild_house(11, 5000)
    lottery = {"prize_pool": 1000}

    transferred = await _economy.drain_bot_balance_into_lottery(lottery, 11)
    assert transferred == 5000
    assert lottery["prize_pool"] == 6000
    assert _economy.get_guild_house_balance(11) == 0
    # And persisted: re-load
    _state.economy["guild_house"].clear()
    await _persistence.init_db_state()
    assert _state.economy["guild_house"].get("11", 0) == 0
