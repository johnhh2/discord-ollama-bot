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

    # Advance 30 days at the current 0.6%/day rate.
    times[0] = t0 + 30 * 86400.0
    value = await _economy.get_savings_value(uid)
    expected = 100 * (1.006 ** 30)
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

    # 10 days at the legacy 1% rate, then 5 days at the current 0.6% rate.
    times[0] = change + 5 * 86400.0
    value = await _economy.get_savings_value(uid)
    expected = 100 * (1.01 ** 10) * (1.006 ** 5)
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


# ── insurance subscriptions: charged by the 5am sweep, not the daily claim ────

async def test_daily_claim_does_not_charge_insurance(db):
    """Premiums are the 5am sweep's job — !daily with an active subscription
    pays the plain reward and leaves coverage untouched."""
    import time as _t

    uid = 8101
    await _economy.add_balance(uid, 5000)
    _state.insurance_subs.add(uid)
    expiry = int(_t.time() + 3600)
    _state.insurance[uid] = {"expires_at": expiry, "protected_from": ["steal"]}

    cog = EconomyCog(bot=_StubBot())
    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=1))
    ctx.bot = _StubBot()
    await cog.cmd_daily.callback(cog, ctx)

    assert await _economy.get_balance(uid) == 5000 + DAILY_REWARD
    assert _state.insurance[uid]["expires_at"] == expiry
    assert "insurance" not in ctx.sent_embeds[-1].description


async def test_insurance_sweep_charges_and_extends_once_per_day(db):
    """The sweep deducts one premium and extends coverage 24h; a same-day
    second run is a no-op."""
    import time as _t
    from src.config import SHOP_INSURANCE_COST, SHOP_INSURANCE_DURATION_SECS
    from src.economy import sweep_insurance_subs, _ct_today

    uid = 8102
    await _economy.add_balance(uid, 5000)
    _state.insurance_subs.add(uid)
    expiry = int(_t.time() + 3600)
    _state.insurance[uid] = {"expires_at": expiry, "protected_from": ["steal"]}
    _state.economy["last_insurance_sweep"] = "2020-01-01"

    await sweep_insurance_subs()

    assert await _economy.get_balance(uid) == 5000 - SHOP_INSURANCE_COST
    assert _state.insurance[uid]["expires_at"] == expiry + SHOP_INSURANCE_DURATION_SECS
    assert _state.economy["last_insurance_sweep"] == _ct_today()
    # The charge accrues for the next daily claim's message.
    assert _state.economy["users"][str(uid)]["ins_paid_since_claim"] == SHOP_INSURANCE_COST

    await sweep_insurance_subs()  # same gameplay-day: no second charge
    assert await _economy.get_balance(uid) == 5000 - SHOP_INSURANCE_COST
    assert _state.insurance[uid]["expires_at"] == expiry + SHOP_INSURANCE_DURATION_SECS
    assert _state.economy["users"][str(uid)]["ins_paid_since_claim"] == SHOP_INSURANCE_COST


async def test_insurance_sweep_first_run_stamps_without_charging(db):
    """The very first sweep (no marker yet — the deploy that ships it) only
    stamps the day: subscribers were charged by the old claim-time flow."""
    import time as _t
    from src.economy import sweep_insurance_subs, _ct_today

    uid = 8103
    await _economy.add_balance(uid, 5000)
    _state.insurance_subs.add(uid)
    expiry = int(_t.time() + 3600)
    _state.insurance[uid] = {"expires_at": expiry, "protected_from": ["steal"]}
    assert _state.economy.get("last_insurance_sweep") is None

    await sweep_insurance_subs()

    assert await _economy.get_balance(uid) == 5000       # not charged
    assert _state.insurance[uid]["expires_at"] == expiry  # not extended
    assert _state.economy["last_insurance_sweep"] == _ct_today()


async def test_insurance_sweep_lapse_keeps_sub_and_tallies(db):
    """A subscriber who can't cover the premium keeps the subscription, but
    coverage doesn't extend — the lapse is tallied for the next claim
    message (the sweep itself sends nothing)."""
    import time as _t
    from src.economy import sweep_insurance_subs

    uid = 8104
    await _economy._ensure_user(uid)  # exists, balance 0 — can't pay
    _state.insurance_subs.add(uid)
    expiry = int(_t.time() + 3600)
    _state.insurance[uid] = {"expires_at": expiry, "protected_from": ["steal"]}
    _state.economy["last_insurance_sweep"] = "2020-01-01"

    await sweep_insurance_subs()

    assert _state.insurance[uid]["expires_at"] == expiry  # not extended
    assert uid in _state.insurance_subs                   # sub retained
    user = _state.economy["users"][str(uid)]
    assert user.get("ins_paid_since_claim", 0) == 0
    assert user["ins_lapsed_since_claim"] == 1


async def test_daily_claim_reports_and_resets_insurance_paid(db):
    """!daily shows what the 5am sweeps charged (and any lapses) since the
    last claim on its own line, then zeroes the counters."""
    uid = 8108
    await _economy.add_balance(uid, 5000)
    user = _state.economy["users"][str(uid)]
    user["ins_paid_since_claim"] = 3000
    user["ins_lapsed_since_claim"] = 2

    cog = EconomyCog(bot=_StubBot())
    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=1))
    ctx.bot = _StubBot()
    await cog.cmd_daily.callback(cog, ctx)

    desc = ctx.sent_embeds[-1].description
    assert "Insurance paid since your last claim: **3,000 🪙**" in desc
    assert "2 insurance renewals couldn't be paid" in desc
    assert user["ins_paid_since_claim"] == 0
    assert user["ins_lapsed_since_claim"] == 0
    # Reported, not re-charged: balance only moved by the reward itself.
    assert await _economy.get_balance(uid) == 5000 + DAILY_REWARD


async def test_auto_daily_reports_and_resets_insurance_paid(db):
    """The auto-claim message carries the same insurance-paid line."""
    from src.events import _auto_daily
    from tests.fakes.discord import FakeChannel

    uid = 8109
    await _economy.add_balance(uid, 5000)
    user = _state.economy["users"][str(uid)]
    user["ins_paid_since_claim"] = 1000

    channel = FakeChannel()
    claimed, prop_rev = await _auto_daily(FakeMember(uid=uid), channel)

    assert claimed == DAILY_REWARD  # the info line never shrinks the stake
    assert user["ins_paid_since_claim"] == 0
    embed = channel.send.call_args.kwargs["embed"]
    assert "Insurance paid since your last claim: **1,000 🪙**" in embed.description


async def test_auto_daily_ignores_insurance_subscription(db):
    """_auto_daily sizes the dailies flip/slots stake — an active
    subscription no longer deducts from (or shows up in) the claim."""
    import time as _t
    from src.events import _auto_daily
    from tests.fakes.discord import FakeChannel

    uid = 8105
    await _economy.add_balance(uid, 5000)
    _state.insurance_subs.add(uid)
    expiry = int(_t.time() + 3600)
    _state.insurance[uid] = {"expires_at": expiry, "protected_from": ["steal"]}

    author = FakeMember(uid=uid)
    channel = FakeChannel()
    claimed, prop_rev = await _auto_daily(author, channel)

    assert claimed == DAILY_REWARD
    assert prop_rev == 0
    assert await _economy.get_balance(uid) == 5000 + DAILY_REWARD
    assert _state.insurance[uid]["expires_at"] == expiry
    embed = channel.send.call_args.kwargs["embed"]
    assert "insurance" not in embed.description


# ── !daily property toggle ────────────────────────────────────────────────────

async def test_daily_property_toggle_flips_flag(db):
    uid = 8106
    await _economy._ensure_user(uid)
    cog = EconomyCog(bot=_StubBot())
    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=1))
    ctx.bot = _StubBot()

    await cog.cmd_daily_property.callback(cog, ctx)
    assert _state.economy["users"][str(uid)]["daily_gamble_property"] is True
    assert "included" in ctx.sent_embeds[-1].description

    await cog.cmd_daily_property.callback(cog, ctx)
    assert _state.economy["users"][str(uid)]["daily_gamble_property"] is False
    assert "left out" in ctx.sent_embeds[-1].description


async def test_daily_property_flag_persists(db):
    uid = 8107
    await _economy._ensure_user(uid)
    _state.economy["users"][str(uid)]["daily_gamble_property"] = True
    await _persistence.save_economy(uid=uid)

    _state.economy["users"].clear()
    await _persistence.init_db_state()

    assert _state.economy["users"][str(uid)]["daily_gamble_property"] is True
