"""Shop tests: money actually moves, persists, and refunds on action failure.

Strategy: drive the helpers (`shop_charge`) and a representative cog method
(`shop_insurance`, `shop_removenickname`) directly. ShopCog's __init__ does no
work beyond stashing `bot`, so we can instantiate it with a dummy bot.
"""
import asyncio
import time as _time
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
    assert uid in _state.insurance
    entry = _state.insurance[uid]
    assert "nickname" in entry["protected_from"]
    assert "tax" in entry["protected_from"]

    # Persisted to DB — read the insurance row from shop_effects directly
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT guild_id, user_id, expires_at FROM shop_effects"
                " WHERE effect_type='insurance' AND user_id=?",
                (uid,),
            )
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0  # bot-wide sentinel guild_id
    assert row[1] == uid
    assert row[2] > 0


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
    assert uid not in _state.insurance


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

async def test_concurrent_shop_insurance_purchases_lose_no_days(monkeypatch):
    """Two concurrent !shop insurance purchases are two legitimate buys
    (charge twice), but neither paid day may be lost.

    extend_insurance stamps synchronously before shop_charge yields, so the
    second invocation must stack its day on top of the first's stamped
    expiry — not overwrite it with its own now+24h.
    """
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST, SHOP_INSURANCE_DURATION_SECS

    cog = ShopCog(bot=None)
    uid = 6010
    starting_balance = SHOP_INSURANCE_COST * 3
    _state.economy.setdefault("users", {})[str(uid)] = {
        "balance": starting_balance, "savings": [],
    }

    charge_count = [0]

    async def _yielding_charge(ctx, charge_uid, cost, **kwargs):
        # Yield once so a concurrent invocation can interleave between
        # the stamp and the charge.
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

    before = _time.time()
    await asyncio.gather(_invoke(), _invoke())

    assert charge_count[0] == 2
    assert _state.economy["users"][str(uid)]["balance"] == starting_balance - 2 * SHOP_INSURANCE_COST
    # Both paid days present: expiry ≈ now + 2 × 24h.
    expiry = _state.insurance[uid]["expires_at"]
    assert expiry >= before + 2 * SHOP_INSURANCE_DURATION_SECS - 5


async def test_shop_insurance_prepay_days_stack(db):
    """`!shop insurance 3` charges 3× the daily premium and grants 3 days;
    a follow-up prepay extends from the current expiry."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST, SHOP_INSURANCE_DURATION_SECS

    cog = ShopCog(bot=None)
    uid = 6020
    await add_balance(uid, SHOP_INSURANCE_COST * 5)

    before = _time.time()
    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx, "3")

    assert await get_balance(uid) == SHOP_INSURANCE_COST * 2
    expiry = _state.insurance[uid]["expires_at"]
    assert expiry >= before + 3 * SHOP_INSURANCE_DURATION_SECS - 5

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx, "2")
    assert await get_balance(uid) == 0
    assert _state.insurance[uid]["expires_at"] == expiry + 2 * SHOP_INSURANCE_DURATION_SECS


async def test_shop_insurance_prepay_respects_max_days_cap(db):
    """Prepaying past SHOP_INSURANCE_MAX_DAYS of total coverage is refused
    without charging."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST, SHOP_INSURANCE_DURATION_SECS, SHOP_INSURANCE_MAX_DAYS

    cog = ShopCog(bot=None)
    uid = 6021
    await add_balance(uid, SHOP_INSURANCE_COST * 100)
    existing_expiry = int(_time.time() + (SHOP_INSURANCE_MAX_DAYS - 1) * SHOP_INSURANCE_DURATION_SECS)
    _state.insurance[uid] = {"expires_at": existing_expiry, "protected_from": ["steal"]}

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx, "2")

    assert await get_balance(uid) == SHOP_INSURANCE_COST * 100  # not charged
    assert _state.insurance[uid]["expires_at"] == existing_expiry  # not extended
    assert any("Capped" in (e.title or "") for e in ctx.sent_embeds)


async def test_shop_insurance_subscribe_charges_first_day_and_persists(db):
    """`!shop insurance sub` with no active coverage charges one day up front,
    grants it, and persists the subscription row."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST

    cog = ShopCog(bot=None)
    uid = 6022
    await add_balance(uid, SHOP_INSURANCE_COST * 2)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx, "sub")

    assert await get_balance(uid) == SHOP_INSURANCE_COST
    assert uid in _state.insurance_subs
    assert uid in _state.insurance

    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT guild_id FROM shop_effects"
                " WHERE effect_type='insurance_sub' AND user_id=?",
                (uid,),
            )
            row = await cur.fetchone()
    assert row is not None and row[0] == 0  # bot-wide sentinel guild_id

    # Unsubscribe removes the sub (coverage stays until expiry).
    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx, "unsub")
    assert uid not in _state.insurance_subs
    assert uid in _state.insurance


async def test_shop_insurance_subscribe_insufficient_funds_rolls_back(db):
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST

    cog = ShopCog(bot=None)
    uid = 6023
    await add_balance(uid, SHOP_INSURANCE_COST - 1)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx, "sub")

    assert await get_balance(uid) == SHOP_INSURANCE_COST - 1
    assert uid not in _state.insurance_subs
    assert uid not in _state.insurance


async def test_renew_insurance_subs_charges_and_extends():
    """The daily-claim hook: a subscribed user is charged one premium and
    coverage extends 24h from the current expiry; an unaffordable renewal
    lapses without touching the subscription."""
    from src.economy import renew_insurance_subs, _ensure_user
    from src.config import SHOP_INSURANCE_COST, SHOP_INSURANCE_DURATION_SECS

    uid = 6024
    await _ensure_user(uid)
    _state.economy["users"][str(uid)]["balance"] = SHOP_INSURANCE_COST + 100
    _state.insurance_subs.add(uid)
    existing_expiry = int(_time.time() + 3600)
    _state.insurance[uid] = {"expires_at": existing_expiry, "protected_from": ["steal"]}

    charged, lapsed = await renew_insurance_subs(uid)
    assert charged == SHOP_INSURANCE_COST
    assert lapsed == 0
    assert _state.economy["users"][str(uid)]["balance"] == 100
    assert _state.insurance[uid]["expires_at"] == existing_expiry + SHOP_INSURANCE_DURATION_SECS
    assert "steal" in _state.insurance[uid]["protected_from"]
    assert "mock" in _state.insurance[uid]["protected_from"]

    # Second renewal: can't afford — coverage untouched, sub retained.
    charged, lapsed = await renew_insurance_subs(uid)
    assert charged == 0
    assert lapsed == 1
    assert _state.economy["users"][str(uid)]["balance"] == 100
    assert _state.insurance[uid]["expires_at"] == existing_expiry + SHOP_INSURANCE_DURATION_SECS
    assert uid in _state.insurance_subs


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
    _state.active_mocks[(42, uid)] = {"remaining": 5, "history": []}

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
    assert (42, target_uid) in _state.active_mocks
    assert (42, uid) not in _state.active_mocks


# ── Insurance is honored at purchase time ────────────────────────────────────
#
# Insurance's purchase message claims protection from ragebait, mock, nickname,
# role assignments, steal, and tax. Pre-audit, three shop commands ignored it:
# shop_tax (charged the buyer; tax silently no-op'd at runtime), shop_mock
# (mock effect applied to insured user and runtime handler didn't check),
# and shop_unassignrole (roles could be stripped despite "role" protection).
# These tests pin the fix: a buyer hitting an insured target must NOT be
# charged and the effect must NOT be applied.


def _insure(uid: int, against: list[str]):
    """Helper: stamp bot-wide insurance on uid for the given categories."""
    _state.insurance[uid] = {
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


async def test_shop_spellcheck_purchase_charges_per_day_and_persists(db, monkeypatch):
    """!shop spellcheck @user 3 charges 3× the per-day cost, activates the
    effect with the day count, and persists it to shop_effects."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_SPELLCHECK_COST

    cog = ShopCog(bot=None)
    buyer_uid = 8101
    target_uid = 8102
    await add_balance(buyer_uid, SHOP_SPELLCHECK_COST * 5)
    target = FakeMember(uid=target_uid, display_name="typo-haver")

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())

    async def _yes_confirm(*a, **k):
        return True
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _yes_confirm)

    ctx = FakeCtx(author=FakeMember(uid=buyer_uid), guild=FakeGuild(gid=42))
    await cog.shop_spellcheck.callback(cog, ctx, f"<@{target_uid}>", "3")

    # Charged 3 days' worth.
    assert await get_balance(buyer_uid) == SHOP_SPELLCHECK_COST * 5 - SHOP_SPELLCHECK_COST * 3
    # Effect activated with the day count.
    assert (42, target_uid) in _state.active_spellchecks
    assert _state.active_spellchecks[(42, target_uid)]["days"] == 3
    assert _state.active_spellchecks[(42, target_uid)]["started_by"] == buyer_uid

    # Persisted to shop_effects with remaining == days.
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT remaining, master_id FROM shop_effects"
                " WHERE effect_type='spellcheck' AND user_id=?",
                (target_uid,),
            )
            row = await cur.fetchone()
    assert row is not None
    assert row[0] == 3
    assert row[1] == buyer_uid


async def test_shop_spellcheck_defaults_to_one_day(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_SPELLCHECK_COST

    cog = ShopCog(bot=None)
    buyer_uid = 8103
    target_uid = 8104
    await add_balance(buyer_uid, SHOP_SPELLCHECK_COST + 500)
    target = FakeMember(uid=target_uid, display_name="t")

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())
    monkeypatch.setattr(_shop_cog, "confirm_purchase", lambda *a, **k: _async_true())

    ctx = FakeCtx(author=FakeMember(uid=buyer_uid), guild=FakeGuild(gid=42))
    await cog.shop_spellcheck.callback(cog, ctx, f"<@{target_uid}>")

    assert await get_balance(buyer_uid) == 500
    assert _state.active_spellchecks[(42, target_uid)]["days"] == 1


async def test_shop_spellcheck_declined_confirm_no_charge(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_SPELLCHECK_COST

    cog = ShopCog(bot=None)
    buyer_uid = 8105
    target_uid = 8106
    await add_balance(buyer_uid, SHOP_SPELLCHECK_COST + 500)
    target = FakeMember(uid=target_uid, display_name="t")

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())

    async def _no_confirm(*a, **k):
        return False
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _no_confirm)

    ctx = FakeCtx(author=FakeMember(uid=buyer_uid), guild=FakeGuild(gid=42))
    await cog.shop_spellcheck.callback(cog, ctx, f"<@{target_uid}>")

    assert await get_balance(buyer_uid) == SHOP_SPELLCHECK_COST + 500
    assert (42, target_uid) not in _state.active_spellchecks


async def test_shop_spellcheck_refuses_against_insured_target(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_SPELLCHECK_COST

    cog = ShopCog(bot=None)
    buyer_uid = 8107
    target_uid = 8108
    await add_balance(buyer_uid, SHOP_SPELLCHECK_COST + 500)
    target = FakeMember(uid=target_uid, display_name="insured")
    _insure(target_uid, ["spellcheck"])

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())
    monkeypatch.setattr(_shop_cog, "confirm_purchase", lambda *a, **k: _async_true())

    ctx = FakeCtx(author=FakeMember(uid=buyer_uid), guild=FakeGuild(gid=42))
    await cog.shop_spellcheck.callback(cog, ctx, f"<@{target_uid}>")

    assert await get_balance(buyer_uid) == SHOP_SPELLCHECK_COST + 500
    assert (42, target_uid) not in _state.active_spellchecks
    assert any("Protected" in (e.title or "") for e in ctx.sent_embeds)


async def _async_true():
    return True


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
    assert (42, uid) in _state.active_mocks


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


# ── !shop buyxp ──────────────────────────────────────────────────────────────
#
# Buys the FULL _xp_cost(current_level) at SHOP_XP_COST_PER_XP coins per XP.
# The quote is confirmed via confirm_purchase (a long await), so the command
# gates re-entry with ShopCog._buyxp_active and re-validates the level after
# the confirm before charging.

from src.leveling import _ensure_lvl_record, _xp_cost, xp_for_level  # noqa: E402
from src.config import SHOP_XP_COST_PER_XP  # noqa: E402


def _seed_level(gid: int, uid: int, internal_level: int, extra_xp: int = 0) -> dict:
    rec = _ensure_lvl_record(gid, uid)
    rec["xp"] = xp_for_level(internal_level) + extra_xp
    rec["level"] = internal_level
    return rec


async def _yes_confirm(*a, **k):
    return True


async def test_shop_buyxp_charges_and_grants_exactly_one_level(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog

    cog = ShopCog(bot=None)
    uid = 9001
    rec = _seed_level(42, uid, 3, extra_xp=50)  # mid-band progress
    cost = _xp_cost(3) * SHOP_XP_COST_PER_XP
    await add_balance(uid, cost + 500)
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _yes_confirm)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_buyxp.callback(cog, ctx)

    # Exactly one level up, in-band progress preserved.
    assert rec["level"] == 4
    assert rec["xp"] == xp_for_level(4) + 50
    # Charged and persisted.
    assert await get_balance(uid) == 500
    assert await _read_db_balance(uid) == 500
    # Leveling row persisted.
    import json
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT data FROM leveling WHERE guild_id=? AND user_id=?",
                (42, uid),
            )
            row = await cur.fetchone()
    assert row is not None
    assert json.loads(row[0])["level"] == 4
    assert any("XP Purchased" in (e.title or "") for e in ctx.sent_embeds)


async def test_shop_buyxp_declined_confirm_no_charge(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog

    cog = ShopCog(bot=None)
    uid = 9002
    rec = _seed_level(42, uid, 3)
    cost = _xp_cost(3) * SHOP_XP_COST_PER_XP
    await add_balance(uid, cost + 500)

    async def _no_confirm(*a, **k):
        return False
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _no_confirm)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_buyxp.callback(cog, ctx)

    assert rec["level"] == 3
    assert rec["xp"] == xp_for_level(3)
    assert await get_balance(uid) == cost + 500
    assert (42, uid) not in cog._buyxp_active


async def test_shop_buyxp_insufficient_funds_never_prompts(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog

    cog = ShopCog(bot=None)
    uid = 9003
    rec = _seed_level(42, uid, 3)
    cost = _xp_cost(3) * SHOP_XP_COST_PER_XP
    await add_balance(uid, cost - 1)

    confirm_calls = [0]

    async def _counting_confirm(*a, **k):
        confirm_calls[0] += 1
        return True
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _counting_confirm)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_buyxp.callback(cog, ctx)

    assert confirm_calls[0] == 0
    assert rec["level"] == 3
    assert await get_balance(uid) == cost - 1
    assert any("Insufficient Funds" in (e.title or "") for e in ctx.sent_embeds)


async def test_shop_buyxp_aborts_without_charge_if_level_changed_during_confirm(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog
    from src.leveling import level_from_xp

    cog = ShopCog(bot=None)
    uid = 9004
    rec = _seed_level(42, uid, 3)
    cost = _xp_cost(3) * SHOP_XP_COST_PER_XP
    await add_balance(uid, cost + 500)

    async def _levelup_mid_confirm(*a, **k):
        # Organic XP lands while the buttons are up: the user crosses into
        # the next band, making the quoted price stale.
        rec["xp"] += _xp_cost(3)
        rec["level"] = level_from_xp(rec["xp"])
        return True
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _levelup_mid_confirm)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_buyxp.callback(cog, ctx)

    # No charge, no purchased XP on top of the organic gain.
    assert await get_balance(uid) == cost + 500
    assert rec["level"] == 4
    assert rec["xp"] == xp_for_level(4)
    assert any("Price Changed" in (e.title or "") for e in ctx.sent_embeds)


async def test_concurrent_shop_buyxp_charges_once(monkeypatch):
    """Two concurrent !shop buyxp calls: one buys, the other bails on the
    in-flight gate. Exactly one charge, exactly one level gained."""
    from src.cogs.shop_cog import ShopCog

    cog = ShopCog(bot=None)
    uid = 9005
    rec = _seed_level(42, uid, 3)
    cost = _xp_cost(3) * SHOP_XP_COST_PER_XP
    _state.economy.setdefault("users", {})[str(uid)] = {
        "balance": cost * 3, "savings": [],
    }

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

    async def _yielding_confirm(*a, **k):
        await asyncio.sleep(0)  # the real confirm is a long await
        return True

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(_shop_cog, "shop_charge", _yielding_charge)
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _yielding_confirm)
    monkeypatch.setattr(_shop_cog, "save_leveling", _noop)
    monkeypatch.setattr(_shop_cog, "record_levelup", _noop)

    ctxs = []

    async def _invoke():
        ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
        ctxs.append(ctx)
        await cog.shop_buyxp.callback(cog, ctx)

    await asyncio.gather(_invoke(), _invoke())

    assert charge_count[0] == 1, (
        f"shop_buyxp double-charged: charged {charge_count[0]}× across 2 "
        f"concurrent invocations (expected 1)"
    )
    assert rec["level"] == 4
    assert rec["xp"] == xp_for_level(4)
    assert _state.economy["users"][str(uid)]["balance"] == cost * 3 - cost
    # The loser saw the in-flight gate.
    all_titles = [e.title or "" for ctx in ctxs for e in ctx.sent_embeds]
    assert any("Purchase Pending" in t for t in all_titles)
    assert (42, uid) not in cog._buyxp_active


async def test_shop_buyxp_rejected_in_dm(monkeypatch):
    from src.cogs.shop_cog import ShopCog

    cog = ShopCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=9006))
    ctx.guild = None  # FakeCtx substitutes a default guild for None
    await cog.shop_buyxp.callback(cog, ctx)

    assert any("Server Only" in (e.title or "") for e in ctx.sent_embeds)
    assert _state.leveling == {}


async def test_shop_buyxp_latches_crime_eligible_at_display_ten(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog

    cog = ShopCog(bot=None)
    uid = 9007
    rec = _seed_level(42, uid, 8)  # buying lands internal 9 = display 10
    cost = _xp_cost(8) * SHOP_XP_COST_PER_XP
    await add_balance(uid, cost + 100)
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _yes_confirm)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_buyxp.callback(cog, ctx)

    assert rec["level"] == 9
    assert _state.economy["users"][str(uid)].get("crime_eligible") is True


class _StubLevelingCog:
    def __init__(self):
        self._announce_levelup = AsyncMock()


class _StubBot:
    def __init__(self, leveling_cog):
        self._leveling_cog = leveling_cog

    def get_cog(self, name):
        return self._leveling_cog if name == "LevelingCog" else None


async def test_shop_buyxp_announces_levelup_for_paid_buy(monkeypatch):
    from src.cogs.shop_cog import ShopCog

    cog = ShopCog(bot=None)
    lvl_cog = _StubLevelingCog()
    cog.bot = _StubBot(lvl_cog)
    uid = 9008
    _seed_level(42, uid, 3)
    cost = _xp_cost(3) * SHOP_XP_COST_PER_XP
    _state.economy.setdefault("users", {})[str(uid)] = {
        "balance": cost + 500, "savings": [],
    }

    async def _true_charge(*a, **k):
        return True

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(_shop_cog, "shop_charge", _true_charge)
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _yes_confirm)
    monkeypatch.setattr(_shop_cog, "save_leveling", _noop)
    monkeypatch.setattr(_shop_cog, "record_levelup", _noop)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_buyxp.callback(cog, ctx)

    lvl_cog._announce_levelup.assert_awaited_once_with(ctx.author, 42)


async def test_shop_buyxp_godmode_skips_announcement_and_reward(monkeypatch):
    """Godmode buys free via shop_charge; skipping the announce keeps the
    levelup_coin_reward from being a free-money printer."""
    from src.cogs.shop_cog import ShopCog

    cog = ShopCog(bot=None)
    lvl_cog = _StubLevelingCog()
    cog.bot = _StubBot(lvl_cog)
    uid = 9009
    rec = _seed_level(42, uid, 3)
    _state.godmode_users.add(uid)

    async def _true_charge(*a, **k):
        return True

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(_shop_cog, "shop_charge", _true_charge)
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _yes_confirm)
    monkeypatch.setattr(_shop_cog, "save_leveling", _noop)
    monkeypatch.setattr(_shop_cog, "record_levelup", _noop)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_buyxp.callback(cog, ctx)

    # XP still granted, but no announcement (and thus no coin reward).
    assert rec["level"] == 4
    lvl_cog._announce_levelup.assert_not_awaited()


# ── universal confirm gating (fixed-price items) ─────────────────────────────
#
# Every shop purchase now opens a Confirm/Cancel prompt before charging.
# Representative decline/drift coverage — the conftest auto-accepts by
# default, so these patch confirm_purchase per-test.

async def _no_confirm(*a, **k):
    return False


async def test_shop_mock_declined_confirm_no_charge(db, monkeypatch):
    """Cancelling the confirm must not charge or activate the mock."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_MOCK_COST

    cog = ShopCog(bot=None)
    buyer_uid = 9101
    target_uid = 9102
    await add_balance(buyer_uid, SHOP_MOCK_COST + 500)
    target = FakeMember(uid=target_uid, display_name="t")

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _no_confirm)

    ctx = FakeCtx(author=FakeMember(uid=buyer_uid), guild=FakeGuild(gid=42))
    await cog.shop_mock.callback(cog, ctx, f"<@{target_uid}>")

    assert await get_balance(buyer_uid) == SHOP_MOCK_COST + 500
    assert (42, target_uid) not in _state.active_mocks


async def test_shop_insurance_prepay_declined_confirm_no_charge(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST

    cog = ShopCog(bot=None)
    uid = 9103
    await add_balance(uid, SHOP_INSURANCE_COST * 3)
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _no_confirm)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx, "2")

    assert await get_balance(uid) == SHOP_INSURANCE_COST * 3
    assert uid not in _state.insurance


async def test_shop_insurance_sub_declined_confirm_no_sub(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_INSURANCE_COST

    cog = ShopCog(bot=None)
    uid = 9104
    await add_balance(uid, SHOP_INSURANCE_COST * 3)
    monkeypatch.setattr(_shop_cog, "confirm_prompt", _no_confirm)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_insurance.callback(cog, ctx, "sub")

    assert await get_balance(uid) == SHOP_INSURANCE_COST * 3
    assert uid not in _state.insurance_subs
    assert uid not in _state.insurance


async def test_shop_artifact_declined_confirm_no_charge(db, monkeypatch):
    from src.cogs.shop_cog import ShopCog
    from src.artifacts import ARTIFACTS

    cog = ShopCog(bot=None)
    uid = 9105
    await add_balance(uid, ARTIFACTS[0]["cost"] + 500)
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _no_confirm)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_artifacts.callback(cog, ctx, "buy", "1")

    assert await get_balance(uid) == ARTIFACTS[0]["cost"] + 500
    assert uid not in _state.user_artifacts or not _state.user_artifacts[uid]


async def test_shop_lockchannel_taken_during_confirm_no_charge(db, monkeypatch):
    """A channel locked by someone else during the confirm wait must not be
    re-locked or charged — the post-confirm re-check catches the drift."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_LOCK_COST

    cog = ShopCog(bot=None)
    uid = 9106
    rival_uid = 9107
    await add_balance(uid, SHOP_LOCK_COST + 500)

    guild = FakeGuild(gid=42)
    channel_id = 777001

    class _FakeChannel:
        id = channel_id
        name = "contested"
        mention = "#contested"

    monkeypatch.setattr(
        ShopCog, "_resolve_channel_strict", lambda self, g, a: _FakeChannel()
    )

    async def _rival_locks_then_yes(*a, **k):
        _state.locked_channels[channel_id] = rival_uid
        return True

    monkeypatch.setattr(_shop_cog, "confirm_purchase", _rival_locks_then_yes)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=guild)
    await cog.shop_lockchannel.callback(cog, ctx, f"<#{channel_id}>")

    assert await get_balance(uid) == SHOP_LOCK_COST + 500
    assert _state.locked_channels[channel_id] == rival_uid
    assert any("Already Locked" in (e.title or "") for e in ctx.sent_embeds)


async def test_shop_unoreverse_effect_expired_during_confirm_no_charge(db, monkeypatch):
    """If the caller's active effect runs out during the confirm wait, the
    post-confirm pop finds nothing — no charge, no redirect."""
    from src.cogs.shop_cog import ShopCog
    from src.config import SHOP_UNOREVERSE_COST

    cog = ShopCog(bot=None)
    uid = 9108
    target_uid = 9109
    await add_balance(uid, SHOP_UNOREVERSE_COST + 500)
    _state.active_mocks[(42, uid)] = {"remaining": 3, "started_by": 1, "channel_id": 1}
    target = FakeMember(uid=target_uid, display_name="t")

    class _StubConverter:
        async def convert(self, ctx, arg):
            return target

    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _StubConverter())

    async def _effect_expires_then_yes(*a, **k):
        _state.active_mocks.pop((42, uid), None)
        return True

    monkeypatch.setattr(_shop_cog, "confirm_purchase", _effect_expires_then_yes)

    ctx = FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))
    await cog.shop_unoreverse.callback(cog, ctx, f"<@{target_uid}>")

    assert await get_balance(uid) == SHOP_UNOREVERSE_COST + 500
    assert (42, target_uid) not in _state.active_mocks
