"""Shop tests: money actually moves, persists, and refunds on action failure.

Strategy: drive the helpers (`shop_charge`) and a representative cog method
(`shop_insurance`, `shop_removenickname`) directly. ShopCog's __init__ does no
work beyond stashing `bot`, so we can instantiate it with a dummy bot.
"""
import asyncio
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.persistence as _persistence
import src.cogs.shop_cog as _shop_cog
from src.economy import add_balance, get_balance
from src.helpers import shop_charge

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio


async def _read_db_balance(uid: int) -> int | None:
    """Read the user's persisted balance directly from the DB (bypasses cache)."""
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT balance FROM economy_users WHERE user_id=?", (uid,)
            )
            row = await cur.fetchone()
    return row[0] if row else None


# ── shop_charge ───────────────────────────────────────────────────────────────

async def test_shop_charge_deducts_and_persists(db):
    uid = 5001
    await add_balance(uid, 100)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    ok = await shop_charge(ctx, uid, 30, cost_label="30")
    assert ok is True
    assert await get_balance(uid) == 70
    # Persisted: read straight from SQLite.
    assert await _read_db_balance(uid) == 70


async def test_shop_charge_insufficient_funds_no_deduction(db):
    uid = 5002
    await add_balance(uid, 10)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    ok = await shop_charge(ctx, uid, 100, cost_label="100")
    assert ok is False
    assert await get_balance(uid) == 10
    assert await _read_db_balance(uid) == 10
    # Sent the "Insufficient Funds" embed
    assert len(ctx.sent_embeds) == 1
    assert "Insufficient Funds" in ctx.sent_embeds[0].title


async def test_shop_charge_godmode_skips_deduction(db):
    uid = 5003
    await add_balance(uid, 50)
    _state.godmode_users.add(uid)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    ok = await shop_charge(ctx, uid, 9999, cost_label="9999")
    assert ok is True
    # Balance is untouched; godmode bypassed deduct.
    assert await get_balance(uid) == 50
    assert await _read_db_balance(uid) == 50


async def test_shop_charge_zero_cost_is_free(db):
    uid = 5004
    await add_balance(uid, 25)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    ok = await shop_charge(ctx, uid, 0, cost_label="0")
    assert ok is True
    assert await get_balance(uid) == 25


# ── shop insurance flow (full cog method) ─────────────────────────────────────

async def test_shop_insurance_purchase_persists_to_state_and_db(db):
    """Buying insurance: deducts balance, sets state.insurance, writes to DB."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST

    cog = ShopCog(bot=None)
    uid = 6001
    await add_balance(uid, SHOP_INSURANCE_COST + 1000)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx)

    # Balance deducted
    assert await get_balance(uid) == 1000
    assert await _read_db_balance(uid) == 1000

    # In-memory state populated
    assert str(uid) in _state.insurance
    entry = _state.insurance[str(uid)]
    assert "nickname" in entry["protected_from"]
    assert "tax" in entry["protected_from"]

    # Persisted to DB — read shop_insurance row directly
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id, expires_at FROM shop_insurance WHERE user_id=?",
                (uid,),
            )
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == uid
    assert row[1] > 0


async def test_shop_insurance_insufficient_funds(db):
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST

    cog = ShopCog(bot=None)
    uid = 6002
    await add_balance(uid, SHOP_INSURANCE_COST - 1)  # one short

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx)

    # Balance untouched (refunded effectively — or never deducted)
    assert await get_balance(uid) == SHOP_INSURANCE_COST - 1
    assert str(uid) not in _state.insurance


# ── nickname refund on Forbidden ──────────────────────────────────────────────

async def test_shop_removenickname_refunds_on_forbidden(db):
    """If Discord refuses the nickname change, the cost should be refunded."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_NICKNAME_REMOVE_COST

    cog = ShopCog(bot=None)
    uid = 7001
    starting_balance = SHOP_NICKNAME_REMOVE_COST + 500
    await add_balance(uid, starting_balance)

    member = FakeMember(uid=uid)
    # Make the .edit raise Forbidden — simulates Discord rejecting the change.
    member.edit = AsyncMock(side_effect=discord.Forbidden(
        response=type("R", (), {"status": 403, "reason": "no"})(),
        message="forbidden",
    ))
    ctx = FakeCtx(author=member, guild=FakeGuild(gid=42))

    await cog.shop_removenickname.callback(cog, ctx)

    # Balance should have been deducted then refunded back to starting.
    assert await get_balance(uid) == starting_balance
    # The DB row should also reflect the final (refunded) balance.
    assert await _read_db_balance(uid) == starting_balance
    # An error embed was sent.
    assert any("No Permission" in (e.title or "") for e in ctx.sent_embeds)


# ── concurrent invocation races ──────────────────────────────────────────────

async def test_concurrent_shop_insurance_charges_once(monkeypatch):
    """Two concurrent !shop insurance purchases must charge once, not twice.

    Pre-fix: shop_insurance checked the 'half expired' gate, then awaited
    shop_charge, then wrote the new entry to state.insurance. Two
    concurrent invocations both passed the gate (no existing entry yet)
    and both paid.
    """
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST

    cog = ShopCog(bot=None)
    uid = 6010
    starting_balance = SHOP_INSURANCE_COST * 3
    _state.economy.setdefault("users", {})[str(uid)] = {
        "balance": starting_balance, "savings": [],
    }

    charge_count = [0]

    async def _yielding_charge(ctx, charge_uid, cost, **kwargs):
        # Yield once so a concurrent invocation can interleave between
        # the gate check and the state.insurance write.
        await asyncio.sleep(0)
        if cost == 0:
            return True
        user = _state.economy["users"][str(charge_uid)]
        if user["balance"] < cost:
            return False
        user["balance"] -= cost
        charge_count[0] += 1
        return True

    async def _noop_save(*a, **kw):
        return None

    monkeypatch.setattr(_shop_cog, "shop_charge", _yielding_charge)
    monkeypatch.setattr(_shop_cog, "save_insurance", _noop_save)

    async def _invoke():
        ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
        await cog.shop_insurance.callback(cog, ctx)

    await asyncio.gather(_invoke(), _invoke())

    assert charge_count[0] == 1, (
        f"shop_insurance double-charged: charged {charge_count[0]}× across 2 "
        f"concurrent invocations (expected 1)"
    )
    assert _state.economy["users"][str(uid)]["balance"] == starting_balance - SHOP_INSURANCE_COST


async def test_concurrent_shop_unoreverse_charges_once(monkeypatch):
    """Two concurrent !shop unoreverse calls must claim the effect once, not crash.

    Pre-fix: shop_unoreverse read `has_mock` etc. via `in`, then awaited
    is_insured / shop_charge, then called state.active_mocks.pop(uid). Two
    concurrent calls both saw `has_mock=True`, both paid, and the second
    one crashed on .pop(KeyError).
    """
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_UNOREVERSE_COST

    cog = ShopCog(bot=None)
    uid = 6020
    target_uid = 6021
    _state.economy.setdefault("users", {})[str(uid)] = {
        "balance": SHOP_UNOREVERSE_COST * 3, "savings": [],
    }
    _state.active_mocks[uid] = {"remaining": 5, "history": []}

    charge_count = [0]

    async def _yielding_charge(ctx, charge_uid, cost, **kwargs):
        await asyncio.sleep(0)
        if cost == 0:
            return True
        user = _state.economy["users"][str(charge_uid)]
        if user["balance"] < cost:
            return False
        user["balance"] -= cost
        charge_count[0] += 1
        return True

    async def _noop(*a, **kw):
        return None

    async def _not_insured(*a, **kw):
        return False

    monkeypatch.setattr(_shop_cog, "shop_charge", _yielding_charge)
    monkeypatch.setattr(_shop_cog, "is_insured", _not_insured)
    monkeypatch.setattr(_shop_cog, "save_mock", _noop)
    monkeypatch.setattr(_shop_cog, "save_ragebait", _noop)
    monkeypatch.setattr(_shop_cog, "save_curse", _noop)

    target = FakeMember(uid=target_uid)

    async def _invoke():
        ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
        # MemberConverter().convert reads ctx.message.content; stub by
        # invoking with a positional arg the cog parses directly.
        await cog.shop_unoreverse.callback(cog, ctx, f"<@{target_uid}>")

    # Stub MemberConverter so the test doesn't need a real Bot.
    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())

    # Must not raise — pre-fix code raised KeyError on the second .pop(uid).
    await asyncio.gather(_invoke(), _invoke())

    assert charge_count[0] == 1, (
        f"shop_unoreverse double-charged: charged {charge_count[0]}× across 2 "
        f"concurrent invocations (expected 1)"
    )
    # The effect should be on the target, not the original uid.
    assert target_uid in _state.active_mocks
    assert uid not in _state.active_mocks


# ── Insurance is honored at purchase time ────────────────────────────────────
#
# Insurance's purchase message claims protection from ragebait, mock, nickname,
# role assignments, steal, and tax. Pre-audit, three shop commands ignored it:
# shop_tax (charged the buyer; tax silently no-op'd at runtime), shop_mock
# (mock effect applied to insured user and runtime handler didn't check),
# and shop_unassignrole (roles could be stripped despite "role" protection).
# These tests pin the fix: a buyer hitting an insured target must NOT be
# charged and the effect must NOT be applied.

import time as _time


def _insure(uid: int, against: list[str]):
    """Helper: stamp insurance on uid for the given categories."""
    _state.insurance[str(uid)] = {
        "expires_at": _time.time() + 3600,
        "protected_from": against,
    }


async def test_shop_tax_refuses_against_insured_target(db, monkeypatch):
    """!shop tax @insured must NOT charge the buyer and must NOT activate
    a tax entry. Pre-fix the buyer paid SHOP_TAX_COST for a tax that the
    runtime handler silently swallowed every message."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_TAX_COST

    cog = ShopCog(bot=None)
    buyer_uid = 8001
    target_uid = 8002
    await add_balance(buyer_uid, SHOP_TAX_COST + 1000)
    target = FakeMember(uid=target_uid, display_name="insured")
    _insure(target_uid, ["tax"])

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())

    ctx = FakeCtx(author=FakeMember(uid=buyer_uid), guild=FakeGuild(gid=42))
    ctx.invoked_with = "tax"
    await cog.shop_tax.callback(cog, ctx, f"<@{target_uid}>")

    # Buyer's balance untouched.
    assert await get_balance(buyer_uid) == SHOP_TAX_COST + 1000
    # No tax activated against the insured target.
    assert target_uid not in _state.active_taxes
    # User saw the "Protected" embed.
    assert any("Protected" in (e.title or "") for e in ctx.sent_embeds)


async def test_shop_mock_refuses_against_insured_target(db, monkeypatch):
    """!shop mock @insured must NOT charge the buyer and must NOT add to
    state.active_mocks. Pre-fix the mock effect was applied and runtime
    _handle_mock had no insurance check, so it fired every message until
    the counter ticked down."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_MOCK_COST

    cog = ShopCog(bot=None)
    buyer_uid = 8003
    target_uid = 8004
    await add_balance(buyer_uid, SHOP_MOCK_COST + 1000)
    target = FakeMember(uid=target_uid, display_name="insured")
    _insure(target_uid, ["mock"])

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())

    ctx = FakeCtx(author=FakeMember(uid=buyer_uid), guild=FakeGuild(gid=42))
    await cog.shop_mock.callback(cog, ctx, f"<@{target_uid}>")

    assert await get_balance(buyer_uid) == SHOP_MOCK_COST + 1000
    assert target_uid not in _state.active_mocks
    assert any("Protected" in (e.title or "") for e in ctx.sent_embeds)


async def test_shop_mock_still_works_on_self(db, monkeypatch):
    """Self-mocking is allowed even when insured — insurance protects from
    others, not from voluntary self-effects. (Mirrors shop_nickname's
    target.id != uid guard.)"""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_MOCK_COST

    cog = ShopCog(bot=None)
    uid = 8005
    await add_balance(uid, SHOP_MOCK_COST + 1000)
    self_member = FakeMember(uid=uid, display_name="me")
    _insure(uid, ["mock"])

    class _StubConverter:
        async def convert(self, ctx, arg):
            return self_member

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())

    ctx = FakeCtx(author=self_member, guild=FakeGuild(gid=42))
    await cog.shop_mock.callback(cog, ctx, f"<@{uid}>")

    # Charged and mock activated despite own insurance.
    assert await get_balance(uid) == 1000
    assert uid in _state.active_mocks


async def test_shop_unassignrole_refuses_against_insured_target(db, monkeypatch):
    """!shop unassignrole @insured @role must NOT charge or strip the role.
    Insurance claims "role assignments" coverage; pre-fix createrole/
    assignrole/deleterole all honored it but unassignrole did not."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_ROLE_REMOVE_COST

    cog = ShopCog(bot=None)
    buyer_uid = 8006
    target_uid = 8007
    await add_balance(buyer_uid, SHOP_ROLE_REMOVE_COST + 1000)

    from tests.fakes.discord import FakeRole
    role = FakeRole(role_id=999, name="Protected Role")
    _state.bot_roles.add(role.id)

    target = FakeMember(uid=target_uid, display_name="insured")
    target.roles = [role]
    target.remove_roles = AsyncMock()
    _insure(target_uid, ["role"])

    guild = FakeGuild(gid=42)
    guild.roles = [role]

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())

    ctx = FakeCtx(author=FakeMember(uid=buyer_uid), guild=guild)
    await cog.shop_unassignrole.callback(cog, ctx, f"<@{target_uid}>", f"<@&{role.id}>")

    assert await get_balance(buyer_uid) == SHOP_ROLE_REMOVE_COST + 1000
    target.remove_roles.assert_not_awaited()
    assert any("Protected" in (e.title or "") for e in ctx.sent_embeds)

    _state.bot_roles.discard(role.id)


async def test_shop_removenickname_works_while_self_insured(db):
    """The owner of an insured nickname must still be able to clear their
    own nickname — insurance protects from others, never from self.
    Pre-fix shop_removenickname blocked the OWNER, calling itself out as
    'they can't change their own nickname'."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_NICKNAME_REMOVE_COST

    cog = ShopCog(bot=None)
    uid = 8008
    starting = SHOP_NICKNAME_REMOVE_COST + 500
    await add_balance(uid, starting)
    _insure(uid, ["nickname"])

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_removenickname.callback(cog, ctx)

    # Charged, edit attempted, success embed sent.
    assert await get_balance(uid) == starting - SHOP_NICKNAME_REMOVE_COST
    ctx.author.edit.assert_awaited_once()
    assert not any("Protected" in (e.title or "") for e in ctx.sent_embeds)
