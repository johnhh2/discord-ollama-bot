"""Shop tests: money actually moves, persists, and refunds on action failure.

Strategy: drive the helpers (`shop_charge`) and a representative cog method
(`shop_insurance`, `shop_removenickname`) directly. ShopCog's __init__ does no
work beyond stashing `bot`, so we can instantiate it with a dummy bot.
"""
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.persistence as _persistence
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
