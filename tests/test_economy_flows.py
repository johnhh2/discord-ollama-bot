"""Economy flow tests: savings interest, daily reset, balance ops.

These exercise the real persistence path (`db` fixture) so the assertions
validate both in-memory state AND what was written to the DB.
"""
import asyncio

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
from src.cogs.economy_cog import EconomyCog
from src.config import DAILY_REWARD

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio


# ── savings ───────────────────────────────────────────────────────────────────

async def test_savings_compound_interest_30_days(db, monkeypatch):
    uid = 8001
    await _economy.add_balance(uid, 1000)

    # Freeze "now" for the deposit, after the rate changeover so the whole
    # window accrues at the current rate.
    t0 = _economy.SAVINGS_RATE_CHANGE_TS + 86400.0
    times = [t0]
    def fake_time():
        return times[0]
    monkeypatch.setattr(_economy.time, "time", fake_time)

    ok = await _economy.add_savings(uid, 100)
    assert ok is True
    assert await _economy.get_balance(uid) == 900  # deducted

    # Advance 30 days at the current 0.3%/day rate.
    times[0] = t0 + 30 * 86400.0
    value = await _economy.get_savings_value(uid)
    expected = 100 * (1.003 ** 30)
    assert abs(value - expected) < 0.01


async def test_savings_rate_change_grandfathers_old_interest(db, monkeypatch):
    """A deposit made before the rate cut accrues 1%/day up to the changeover,
    then continues at the new rate — earned interest is never clawed back."""
    uid = 8006
    await _economy.add_balance(uid, 1000)

    change = _economy.SAVINGS_RATE_CHANGE_TS
    times = [change - 10 * 86400.0]
    monkeypatch.setattr(_economy.time, "time", lambda: times[0])
    await _economy.add_savings(uid, 100)

    # 10 days at the legacy 1% rate, then 5 days at the current 0.3% rate.
    times[0] = change + 5 * 86400.0
    value = await _economy.get_savings_value(uid)
    expected = 100 * (1.01 ** 10) * (1.003 ** 5)
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


async def test_partial_withdraw_then_redeposit_does_not_lose_value(db, monkeypatch):
    """Regression: with the old `int()` truncation in remove_savings, a 1000-coin
    deposit aged 1 day (val=1010.0, displayed 1010) followed by withdraw 500 +
    redeposit 500 dropped displayed savings from 1010 → 1009.

    With the float-remainder fix, displayed savings must never decrease across
    a withdraw-X / redeposit-X round trip."""
    uid = 8005
    await _economy.add_balance(uid, 5000)

    times = [0.0]
    monkeypatch.setattr(_economy.time, "time", lambda: times[0])

    # Deposit 1000, advance 1 day → val=1010.0, displayed 1010.
    await _economy.add_savings(uid, 1000)
    times[0] = 86400.0
    before = int(await _economy.get_savings_value(uid))
    assert before == 1010

    # Withdraw 500 → keeps a partial principal in the deposit.
    ok = await _economy.remove_savings(uid, 500)
    assert ok is True

    # Redeposit 500 at the same instant.
    ok = await _economy.add_savings(uid, 500)
    assert ok is True

    after = int(await _economy.get_savings_value(uid))
    assert after >= before, f"savings dropped {before} -> {after} after withdraw+redeposit of 500"


async def test_repeated_withdraw_redeposit_does_not_drain_savings(db, monkeypatch):
    """A user repeating withdraw-X / redeposit-X should not be able to bleed
    coins out of savings via cumulative rounding loss."""
    uid = 8006
    await _economy.add_balance(uid, 100000)

    times = [0.0]
    monkeypatch.setattr(_economy.time, "time", lambda: times[0])

    await _economy.add_savings(uid, 10000)
    # Age the deposit through several differently-rounded factors.
    for day in (1, 3, 7, 13, 29):
        times[0] = day * 86400.0
        before = int(await _economy.get_savings_value(uid))
        # Pick a partial amount that forces the partial-cut branch.
        partial = before // 3
        ok = await _economy.remove_savings(uid, partial)
        assert ok is True
        ok = await _economy.add_savings(uid, partial)
        assert ok is True
        after = int(await _economy.get_savings_value(uid))
        assert after >= before, (
            f"day {day}: withdraw+redeposit of {partial} dropped {before} -> {after}"
        )


async def test_full_withdraw_then_redeposit_preserves_value(db, monkeypatch):
    """Withdrawing the entire displayed savings and redepositing must not lose
    coins either (this path always worked, but lock it in)."""
    uid = 8007
    await _economy.add_balance(uid, 5000)

    times = [0.0]
    monkeypatch.setattr(_economy.time, "time", lambda: times[0])

    await _economy.add_savings(uid, 1000)
    times[0] = 5 * 86400.0
    before = int(await _economy.get_savings_value(uid))

    ok = await _economy.remove_savings(uid, before)
    assert ok is True
    ok = await _economy.add_savings(uid, before)
    assert ok is True

    after = int(await _economy.get_savings_value(uid))
    assert after == before


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


# ── !daily race condition ────────────────────────────────────────────────────

class _StubBotUser:
    def __init__(self, uid: int = 999_999_999):
        self.id = uid


class _StubBot:
    def __init__(self):
        self.user = _StubBotUser()


async def test_concurrent_daily_invocations_grant_once(monkeypatch):
    """Spamming !daily concurrently must only credit DAILY_REWARD once.

    Previously, `cmd_daily` checked daily_date == today, then awaited
    add_balance() before stamping daily_date. Two concurrent invocations
    could each pass the gate and both award the reward.

    Forces real event-loop yielding via patched add_balance — without that,
    the conftest noop stubs return synchronously and never expose the
    interleaving the bug needs.
    """
    cog = EconomyCog(bot=_StubBot())
    author = FakeMember(uid=7777, display_name="dailyspammer")
    guild = FakeGuild(gid=1)

    await _economy._ensure_user(author.id)
    starting_balance = _state.economy["users"][str(author.id)]["balance"]

    grant_count = [0]

    async def _yielding_add_balance(uid, n, **kwargs):
        # Let the event loop schedule another invocation here — this is the
        # exact spot where the pre-fix code yielded between the daily-date
        # check and the daily-date set.
        await asyncio.sleep(0)
        _state.economy["users"][str(uid)]["balance"] += n
        grant_count[0] += 1
        return False

    async def _noop_save(*args, **kwargs):
        return None

    monkeypatch.setattr("src.cogs.economy_cog.add_balance", _yielding_add_balance)
    # The cogs/economy_cog module imports save_economy by name, so the global
    # conftest stubs (on src.persistence and src.economy) don't reach this
    # binding. Patch the module-local one too.
    monkeypatch.setattr("src.cogs.economy_cog.save_economy", _noop_save)

    async def _invoke():
        ctx = FakeCtx(author=author, guild=guild)
        ctx.bot = _StubBot()
        await cog.cmd_daily.callback(cog, ctx)

    # Three concurrent claims — only one should succeed.
    await asyncio.gather(_invoke(), _invoke(), _invoke())

    final_balance = _state.economy["users"][str(author.id)]["balance"]
    assert grant_count[0] == 1, (
        f"daily granted {grant_count[0]} times across 3 concurrent invocations "
        f"(expected 1)"
    )
    assert final_balance == starting_balance + DAILY_REWARD, (
        f"daily double-claim: balance went {starting_balance} -> {final_balance} "
        f"(expected +{DAILY_REWARD})"
    )
