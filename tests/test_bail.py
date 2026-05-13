"""Tests for !bail / !bailout and the bail_amount tracking on jail sites.

The !bail command shows a Confirm/Cancel purchase window before charging.
For most cmd_bail tests we monkeypatch `src.cogs.economy_cog.confirm_purchase`
so we can deterministically simulate confirm/cancel/race-condition outcomes
without driving the real View.
"""
import asyncio
import random
import time

import pytest

import src.state as _state
import src.economy as _economy
import src.cogs.economy_cog as _economy_cog
from src.cogs.economy_cog import EconomyCog

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)


class _StubBotUser:
    def __init__(self, uid: int = 999_999_999):
        self.id = uid


class _StubBot:
    def __init__(self):
        self.user = _StubBotUser()


def _grant_level(uid: int, internal_level: int, gid: int = 42) -> None:
    """Mirror of test_money_flows._grant_level: clear the level-10 crime gate."""
    _state.leveling.setdefault(str(gid), {})[str(uid)] = {"xp": 0, "level": internal_level}
    if internal_level >= 9:
        _state.economy.setdefault("users", {}).setdefault(str(uid), {
            "balance": 0, "savings": [],
        })
        _state.economy["users"][str(uid)]["crime_eligible"] = True


def _seed_savings(uid: int, amount: int):
    _state.economy.setdefault("users", {}).setdefault(str(uid), {"balance": 0, "savings": []})
    _state.economy["users"][str(uid)].setdefault("savings", []).append(
        {"amount": amount, "deposited_at": time.time()},
    )


def _make_hstate(host, target, joiners: list):
    slots: list = [host, None, None, None]
    for i, j in enumerate(joiners[:3]):
        slots[i + 1] = j
    return {
        "host": host, "target": target, "slots": slots, "message": None,
        "opened_at": 0.0, "opened_at_wall": time.time(),
        "warned": False, "started": True, "cancelled": False,
    }


def _make_steal_ctx(thief: FakeMember, victim: FakeMember):
    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    # Explicit tier so we skip the interactive picker.
    ctx.message = FakeMessage(content="!steal @victim 1")
    return ctx


# ── bail_amount populated at jail sites ──────────────────────────────────────

async def test_steal_jail_records_bail_amount(db, monkeypatch):
    """Failed steal + jailed → thief's bail_amount is the attempted steal."""
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=1_000, display_name="thief")
    victim = FakeMember(uid=1_001, display_name="victim")
    await _economy.add_balance(thief.id, 5_000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 9)

    # Tier 1: steal_pct=0.10 → attempted = 1000. Steal fails (>= 0.10), jail hits (< 0.25).
    rolls = iter([0.50, 0.10])
    monkeypatch.setattr(random, "random", lambda: next(rolls))
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = _make_steal_ctx(thief, victim)
    await cog.cmd_steal.callback(cog, ctx, target=victim)

    assert _state.economy["users"][str(thief.id)]["bail_amount"] == 1_000


async def test_mug_jail_records_bail_amount(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    thief = FakeMember(uid=1_010, display_name="thief")
    victim = FakeMember(uid=1_011, display_name="victim")
    await _economy.add_balance(thief.id, 5_000)
    await _economy.add_balance(victim.id, 10_000)
    _grant_level(victim.id, 12)

    # random < 0.5 → jailed.
    monkeypatch.setattr(random, "random", lambda: 0.10)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    ctx = FakeCtx(author=thief, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog.cmd_mug.callback(cog, ctx, target=victim, amount="1500")

    assert _state.economy["users"][str(thief.id)]["bail_amount"] == 1_500


async def test_bankheist_failure_records_zero_bail_amount(db, monkeypatch):
    """Chance-miss bankheist failure: jailed participant gets bail_amount=0
    (legacy 10k bail), since no savings were even queried."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=1_020, display_name="host")
    target = FakeMember(uid=1_021, display_name="target")
    _seed_savings(target.id, 10_000)

    # 0.99 → chance roll fails (no success), then 0.0 → jail roll hits.
    rolls = iter([0.99, 0.0])
    monkeypatch.setattr(random, "random", lambda: next(rolls))

    hstate = _make_hstate(host, target, [])
    ctx = FakeCtx(author=host, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog._bankheist_resolve(ctx, hstate)

    assert _state.economy["users"][str(host.id)]["bail_amount"] == 0


async def test_bankheist_success_records_share_as_bail_amount(db, monkeypatch):
    """Successful bankheist + jail roll: bail_amount equals the cut share."""
    cog = EconomyCog(bot=_StubBot())
    host = FakeMember(uid=1_030, display_name="host")
    j1 = FakeMember(uid=1_031, display_name="j1")
    target = FakeMember(uid=1_032, display_name="target")
    _seed_savings(target.id, 10_000)  # 20% = 2000, split 2 = 1000 each

    monkeypatch.setattr(random, "random", lambda: 0.0)

    hstate = _make_hstate(host, target, [j1])
    ctx = FakeCtx(author=host, guild=FakeGuild(gid=42))
    ctx.bot = _StubBot()
    await cog._bankheist_resolve(ctx, hstate)

    assert _state.economy["users"][str(host.id)]["bail_amount"] == 1_000
    assert _state.economy["users"][str(j1.id)]["bail_amount"] == 1_000


# ── cmd_bail behavior ────────────────────────────────────────────────────────

async def _set_jailed(uid: int, jail_until: float | None = None, bail_amount: int = 0):
    """Force a user into jail with the given bail_amount."""
    await _economy._ensure_user(uid)
    user = _state.economy["users"][str(uid)]
    user["jail_until"] = jail_until if jail_until is not None else time.time() + 3_600
    user["jail_reason"] = "Tried to steal from someone"
    user["bail_amount"] = bail_amount


def _patch_confirm(monkeypatch, return_value: bool, *, before_return=None):
    """Replace confirm_purchase with a deterministic stub. Optionally runs
    `before_return(ctx, payer)` before returning so tests can simulate state
    drift during the confirm window."""
    calls = []

    async def _fake(ctx, *, title, description, cost, payer, timeout=30.0):
        calls.append({"title": title, "cost": cost, "payer": payer})
        if before_return is not None:
            await before_return(ctx, payer)
        return return_value

    monkeypatch.setattr(_economy_cog, "confirm_purchase", _fake)
    return calls


async def test_bail_self_legacy_jail_costs_10k(db, monkeypatch):
    """Legacy jail (bail_amount=0) → flat 10k cost; balance debited and jail cleared."""
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=2_000, display_name="user")
    await _economy.add_balance(user.id, 50_000)
    await _set_jailed(user.id, bail_amount=0)
    calls = _patch_confirm(monkeypatch, return_value=True)

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_bail.callback(cog, ctx, target=None)

    assert calls and calls[0]["cost"] == 10_000
    assert await _economy.get_balance(user.id) == 40_000
    assert _state.economy["users"][str(user.id)]["jail_until"] == 0
    assert _state.economy["users"][str(user.id)]["bail_amount"] == 0


async def test_bail_with_recorded_amount_costs_half_plus_10k(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=2_001, display_name="user")
    await _economy.add_balance(user.id, 100_000)
    await _set_jailed(user.id, bail_amount=50_000)
    calls = _patch_confirm(monkeypatch, return_value=True)

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_bail.callback(cog, ctx, target=None)

    assert calls and calls[0]["cost"] == 35_000  # 10k + 25k
    assert await _economy.get_balance(user.id) == 65_000


async def test_bail_other_user_payer_charged(db, monkeypatch):
    """!bail @other → payer's wallet charged, jailed user freed but wallet unchanged."""
    cog = EconomyCog(bot=_StubBot())
    payer = FakeMember(uid=2_010, display_name="payer")
    jailed = FakeMember(uid=2_011, display_name="jailed")
    await _economy.add_balance(payer.id, 50_000)
    await _economy.add_balance(jailed.id, 5_000)
    await _set_jailed(jailed.id, bail_amount=0)
    _patch_confirm(monkeypatch, return_value=True)

    ctx = FakeCtx(author=payer, guild=FakeGuild(gid=42))
    await cog.cmd_bail.callback(cog, ctx, target=jailed)

    assert await _economy.get_balance(payer.id) == 40_000
    assert await _economy.get_balance(jailed.id) == 5_000  # untouched
    assert _state.economy["users"][str(jailed.id)]["jail_until"] == 0


async def test_bail_insufficient_funds_rejected(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=2_020, display_name="user")
    await _economy.add_balance(user.id, 5_000)  # under the 10k base
    await _set_jailed(user.id, bail_amount=0)
    calls = _patch_confirm(monkeypatch, return_value=True)

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_bail.callback(cog, ctx, target=None)

    # confirm_purchase never called, balance unchanged, still jailed.
    assert calls == []
    assert await _economy.get_balance(user.id) == 5_000
    assert _state.economy["users"][str(user.id)]["jail_until"] > time.time()
    assert any("Insufficient" in (e.title or "") for e in ctx.sent_embeds)


async def test_bail_not_jailed_errors(db, monkeypatch):
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=2_030, display_name="user")
    await _economy.add_balance(user.id, 50_000)
    # not jailed
    calls = _patch_confirm(monkeypatch, return_value=True)

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_bail.callback(cog, ctx, target=None)

    assert calls == []
    assert await _economy.get_balance(user.id) == 50_000
    assert any("Not Jailed" in (e.title or "") for e in ctx.sent_embeds)


async def test_bail_declined_no_charge(db, monkeypatch):
    """Cancel button → no debit, still jailed."""
    cog = EconomyCog(bot=_StubBot())
    user = FakeMember(uid=2_040, display_name="user")
    await _economy.add_balance(user.id, 50_000)
    await _set_jailed(user.id, bail_amount=20_000)
    _patch_confirm(monkeypatch, return_value=False)

    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    await cog.cmd_bail.callback(cog, ctx, target=None)

    assert await _economy.get_balance(user.id) == 50_000
    assert _state.economy["users"][str(user.id)]["jail_until"] > time.time()
    assert _state.economy["users"][str(user.id)]["bail_amount"] == 20_000


async def test_bail_released_during_confirm_no_charge(db, monkeypatch):
    """If the jailed user is released between cost-quote and confirm, no charge."""
    cog = EconomyCog(bot=_StubBot())
    payer = FakeMember(uid=2_050, display_name="payer")
    jailed = FakeMember(uid=2_051, display_name="jailed")
    await _economy.add_balance(payer.id, 50_000)
    await _set_jailed(jailed.id, bail_amount=0)

    async def _free_during_confirm(ctx, payer_arg):
        _state.economy["users"][str(jailed.id)]["jail_until"] = 0

    _patch_confirm(monkeypatch, return_value=True, before_return=_free_during_confirm)

    ctx = FakeCtx(author=payer, guild=FakeGuild(gid=42))
    await cog.cmd_bail.callback(cog, ctx, target=jailed)

    assert await _economy.get_balance(payer.id) == 50_000
    assert any("Already Free" in (e.title or "") for e in ctx.sent_embeds)


async def test_bail_balance_drops_during_confirm(db, monkeypatch):
    """If the payer's balance drops below cost during the confirm window, no charge."""
    cog = EconomyCog(bot=_StubBot())
    payer = FakeMember(uid=2_060, display_name="payer")
    jailed = FakeMember(uid=2_061, display_name="jailed")
    await _economy.add_balance(payer.id, 50_000)
    await _set_jailed(jailed.id, bail_amount=0)  # cost = 10k

    async def _drain_during_confirm(ctx, payer_arg):
        await _economy.deduct_balance(payer.id, 45_000)  # leaves 5k, < 10k

    _patch_confirm(monkeypatch, return_value=True, before_return=_drain_during_confirm)

    ctx = FakeCtx(author=payer, guild=FakeGuild(gid=42))
    await cog.cmd_bail.callback(cog, ctx, target=jailed)

    # Drain ran (50k → 5k); cmd_bail must NOT additionally debit the bail cost.
    assert await _economy.get_balance(payer.id) == 5_000
    assert _state.economy["users"][str(jailed.id)]["jail_until"] > time.time()
    assert any("Insufficient Funds" in (e.title or "") for e in ctx.sent_embeds)


async def test_concurrent_bail_confirms_charge_once(monkeypatch):
    """Two concurrent !bail confirmations for the same jailed user must charge
    the payer once, not twice.

    Previously, cmd_bail checked `jail_until > now` then awaited deduct_balance
    before clearing jail_until — so two confirmations both passed the gate and
    both deducted the cost. The fix clears jail_until synchronously up front
    so the second confirmation sees jail_until=0 and bails out on the
    'Already Free' branch.
    """
    cog = EconomyCog(bot=_StubBot())
    payer = FakeMember(uid=2_070, display_name="payer")
    jailed = FakeMember(uid=2_071, display_name="jailed")
    await _economy._ensure_user(payer.id)
    _state.economy["users"][str(payer.id)]["balance"] = 50_000
    await _set_jailed(jailed.id, bail_amount=0)  # cost = 10k
    _patch_confirm(monkeypatch, return_value=True)

    # Force deduct_balance to yield to the event loop so the second
    # invocation can interleave at exactly the spot where the unfixed code
    # raced. Use the real balance math but insert an awaitable break.
    async def _yielding_deduct(uid, n):
        await asyncio.sleep(0)
        user = _state.economy["users"][str(uid)]
        if user["balance"] < n:
            return False
        user["balance"] -= n
        return True

    async def _noop_save(*a, **kw):
        return None

    monkeypatch.setattr(_economy_cog, "deduct_balance", _yielding_deduct)
    # cmd_bail also imports save_economy / get_balance by name; the global
    # conftest stubs don't reach those bindings.
    monkeypatch.setattr(_economy_cog, "save_economy", _noop_save)

    async def _get_balance(uid):
        return _state.economy["users"][str(uid)]["balance"]

    monkeypatch.setattr(_economy_cog, "get_balance", _get_balance)

    async def _invoke():
        ctx = FakeCtx(author=payer, guild=FakeGuild(gid=42))
        await cog.cmd_bail.callback(cog, ctx, target=jailed)

    await asyncio.gather(_invoke(), _invoke())

    # 50k - 10k = 40k. With the race, balance would be 30k.
    assert _state.economy["users"][str(payer.id)]["balance"] == 40_000, (
        f"bail double-charged: balance is "
        f"{_state.economy['users'][str(payer.id)]['balance']}, expected 40000"
    )
    assert _state.economy["users"][str(jailed.id)]["jail_until"] == 0
