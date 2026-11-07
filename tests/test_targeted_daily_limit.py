"""Ragebait, mock, curse and tax: one buy of each per buyer per gameplay day."""
import asyncio

import pytest

import src.state as _state
import src.persistence as _persistence
import src.cogs.shop_cog as _shop_cog
from src.cogs.shop_cog import ShopCog
from src.economy import add_balance, get_balance, _ct_today

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

pytestmark = pytest.mark.asyncio

ITEMS = ("ragebait", "mock", "curse", "tax")
BUYER = 8801


def _target_stub(monkeypatch, target):
    class _Stub:
        async def convert(self, ctx, arg):
            return target
    monkeypatch.setattr(_shop_cog, "MemberConverter", lambda: _Stub())
    monkeypatch.setattr(_shop_cog, "resolve_member", _Stub().convert)


def _ai_stub(monkeypatch, reply="you again?"):
    async def _stream(session, messages, placeholder, **kw):
        return reply

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(_shop_cog, "stream_ollama", _stream)
    monkeypatch.setattr(_shop_cog, "keep_typing", _noop)
    monkeypatch.setattr(_shop_cog, "finalize", _noop)


async def _buy(cog, item, target_uid=8802):
    ctx = FakeCtx(author=FakeMember(uid=BUYER), guild=FakeGuild(gid=42))
    ctx.invoked_with = item
    await getattr(cog, f"shop_{item}").callback(cog, ctx, f"<@{target_uid}>")
    return ctx


def _refused(ctx) -> bool:
    return any("Once a Day" in (e.title or "") for e in ctx.sent_embeds)


@pytest.mark.parametrize("item", ITEMS)
async def test_second_buy_same_day_is_refused(db, monkeypatch, item):
    _target_stub(monkeypatch, FakeMember(uid=8802, display_name="target"))
    _ai_stub(monkeypatch)
    cog = ShopCog(bot=None)
    await add_balance(BUYER, 1_000_000)

    assert not _refused(await _buy(cog, item))
    after_first = await get_balance(BUYER)
    assert after_first < 1_000_000

    _target_stub(monkeypatch, FakeMember(uid=8803, display_name="other"))
    assert _refused(await _buy(cog, item, target_uid=8803))
    assert await get_balance(BUYER) == after_first


async def test_items_are_limited_separately(db, monkeypatch):
    _target_stub(monkeypatch, FakeMember(uid=8802, display_name="target"))
    cog = ShopCog(bot=None)
    await add_balance(BUYER, 1_000_000)

    assert not _refused(await _buy(cog, "mock"))
    assert not _refused(await _buy(cog, "curse"))


async def test_yesterdays_buy_does_not_count(db, monkeypatch):
    _target_stub(monkeypatch, FakeMember(uid=8802, display_name="target"))
    cog = ShopCog(bot=None)
    await add_balance(BUYER, 1_000_000)
    _state.economy["users"][str(BUYER)]["targeted_buy_days"] = {"mock": "2000-01-01"}

    assert not _refused(await _buy(cog, "mock"))


async def test_claim_persists(db, monkeypatch):
    _target_stub(monkeypatch, FakeMember(uid=8802, display_name="target"))
    cog = ShopCog(bot=None)
    await add_balance(BUYER, 1_000_000)
    await _buy(cog, "curse")

    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT targeted_buy_days FROM economy_users WHERE user_id=?", (BUYER,))
            (raw,) = await cur.fetchone()
    assert _ct_today() in raw and "curse" in raw


async def test_failed_charge_releases_the_claim(db, monkeypatch):
    _target_stub(monkeypatch, FakeMember(uid=8802, display_name="target"))
    cog = ShopCog(bot=None)
    await add_balance(BUYER, 1)

    await _buy(cog, "mock")
    assert not _shop_cog._targeted_buy_used(BUYER, "mock")


async def test_ragebait_ai_failure_releases_the_claim(db, monkeypatch):
    _target_stub(monkeypatch, FakeMember(uid=8802, display_name="target"))
    _ai_stub(monkeypatch, reply="")
    cog = ShopCog(bot=None)
    await add_balance(BUYER, 1_000_000)

    await _buy(cog, "ragebait")
    assert await get_balance(BUYER) == 1_000_000
    assert not _shop_cog._targeted_buy_used(BUYER, "ragebait")


async def test_concurrent_buys_charge_once(db, monkeypatch):
    _target_stub(monkeypatch, FakeMember(uid=8802, display_name="target"))
    cog = ShopCog(bot=None)
    await add_balance(BUYER, 1_000_000)
    charges = [0]
    real_charge = _shop_cog.shop_charge

    async def _yielding_charge(*a, **kw):
        await asyncio.sleep(0)
        charges[0] += 1
        return await real_charge(*a, **kw)

    async def _yielding_confirm(*a, **kw):
        await asyncio.sleep(0)
        return True
    monkeypatch.setattr(_shop_cog, "shop_charge", _yielding_charge)
    monkeypatch.setattr(_shop_cog, "confirm_purchase", _yielding_confirm)

    ctxs = await asyncio.gather(_buy(cog, "mock"), _buy(cog, "mock"))

    assert charges[0] == 1
    assert sum(_refused(c) for c in ctxs) == 1
