"""Daily half-price lottery tickets (src/cogs/lottery_cog.py).

Each user's first DISCOUNT_DAILY_CAP tickets per gameplay-day cost
DISCOUNT_TICKET_PRICE instead of TICKET_PRICE, on every purchase path
(!lottery <n>, !lottery match, and the dailies 🎟️ reaction — the latter is
covered in tests/test_dailies.py). Tracked per user in the
lottery_disc_used/lottery_disc_date economy columns (migration 0043).
"""
import asyncio
import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import src.state as _state
import src.economy as _economy
import src.persistence as _persistence
from src.cogs.lottery_cog import (
    LotteryCog, discount_tickets_remaining, ticket_cost,
    TICKET_PRICE, DISCOUNT_TICKET_PRICE, DISCOUNT_DAILY_CAP, TICKET_CAP,
)
from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

TODAY = "2026-05-02"
YESTERDAY = "2026-05-01"
# A mid-month noon — well clear of the 1st-of-month lock/draw windows.
NOW_CT = datetime.datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("America/Chicago"))


def _pin_clock(monkeypatch, today=TODAY, now=NOW_CT):
    monkeypatch.setattr("src.cogs.lottery_cog._ct_today", lambda: today)
    monkeypatch.setattr("src.cogs.lottery_cog._ct_now", lambda: now)


def _make_cog() -> LotteryCog:
    cog = LotteryCog(SimpleNamespace(user=None, guilds=[]))
    cog.lottery_scheduler.cancel()
    return cog


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_discount_remaining_fresh_user_gets_full_allotment():
    user = {}
    assert discount_tickets_remaining(user, TODAY) == DISCOUNT_DAILY_CAP
    assert user["lottery_disc_date"] == TODAY
    assert user["lottery_disc_used"] == 0


def test_discount_remaining_rolls_over_on_new_day():
    user = {"lottery_disc_date": YESTERDAY, "lottery_disc_used": DISCOUNT_DAILY_CAP}
    assert discount_tickets_remaining(user, TODAY) == DISCOUNT_DAILY_CAP
    assert user["lottery_disc_used"] == 0


def test_discount_remaining_counts_down_same_day():
    user = {"lottery_disc_date": TODAY, "lottery_disc_used": 7}
    assert discount_tickets_remaining(user, TODAY) == DISCOUNT_DAILY_CAP - 7


def test_ticket_cost_all_discounted():
    cost, discounted = ticket_cost(4, DISCOUNT_DAILY_CAP)
    assert (cost, discounted) == (4 * DISCOUNT_TICKET_PRICE, 4)


def test_ticket_cost_split_across_the_cap():
    cost, discounted = ticket_cost(12, DISCOUNT_DAILY_CAP)
    assert discounted == DISCOUNT_DAILY_CAP
    assert cost == DISCOUNT_DAILY_CAP * DISCOUNT_TICKET_PRICE + 2 * TICKET_PRICE


def test_ticket_cost_no_discount_left():
    cost, discounted = ticket_cost(5, 0)
    assert (cost, discounted) == (5 * TICKET_PRICE, 0)


# ── _execute_purchase ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purchase_applies_discount_and_splits_pool(db, monkeypatch):
    _pin_clock(monkeypatch)
    uid = 9101
    await _economy.add_balance(uid, 1_000)
    cog = _make_cog()

    result = await cog._execute_purchase(1, uid, 12)

    # 10 half price + 2 full price.
    assert result["discounted"] == DISCOUNT_DAILY_CAP
    assert result["cost"] == 10 * 5 + 2 * 10
    assert await _economy.get_balance(uid) == 1_000 - 70
    user = _state.economy["users"][str(uid)]
    assert user["lottery_disc_used"] == DISCOUNT_DAILY_CAP
    assert user["lottery_disc_date"] == TODAY
    # Pool: half-price tickets +4, full-price +7, +1,000 new-player bonus.
    lot = await _persistence.load_lottery(1)
    assert lot["prize_pool"] == 10 * 4 + 2 * 7 + 1_000
    assert lot["players"][str(uid)] == 12
    # House: half-price tickets +1, full-price +3.
    assert _economy.get_guild_house_balance(1) == 10 * 1 + 2 * 3


@pytest.mark.asyncio
async def test_purchase_second_buy_same_day_is_full_price(db, monkeypatch):
    _pin_clock(monkeypatch)
    uid = 9102
    await _economy.add_balance(uid, 1_000)
    cog = _make_cog()

    first = await cog._execute_purchase(1, uid, DISCOUNT_DAILY_CAP)
    second = await cog._execute_purchase(1, uid, 5)

    assert first["discounted"] == DISCOUNT_DAILY_CAP
    assert second["discounted"] == 0
    assert second["cost"] == 5 * TICKET_PRICE


@pytest.mark.asyncio
async def test_purchase_discount_resets_next_day(db, monkeypatch):
    _pin_clock(monkeypatch, today=YESTERDAY)
    uid = 9103
    await _economy.add_balance(uid, 1_000)
    cog = _make_cog()
    await cog._execute_purchase(1, uid, DISCOUNT_DAILY_CAP)

    _pin_clock(monkeypatch, today=TODAY)
    result = await cog._execute_purchase(1, uid, 3)

    assert result["discounted"] == 3
    assert result["cost"] == 3 * DISCOUNT_TICKET_PRICE


@pytest.mark.asyncio
async def test_purchase_funds_failure_rolls_back_discount_claim(db, monkeypatch):
    _pin_clock(monkeypatch)
    uid = 9104
    await _economy.add_balance(uid, 20)  # can't afford 10 tickets even at half price
    cog = _make_cog()

    result = await cog._execute_purchase(1, uid, DISCOUNT_DAILY_CAP)

    assert result == {"error": "funds", "cost": DISCOUNT_DAILY_CAP * DISCOUNT_TICKET_PRICE}
    user = _state.economy["users"][str(uid)]
    assert user["lottery_disc_used"] == 0  # claim rolled back
    assert await _economy.get_balance(uid) == 20


@pytest.mark.asyncio
async def test_purchase_over_cap_touches_nothing(db, monkeypatch):
    _pin_clock(monkeypatch)
    uid = 9105
    await _economy.add_balance(uid, 1_000)
    await _persistence.save_lottery(1, {
        "prize_pool": 0, "last_posted_week": 0, "last_drawn_week": 0,
        "players": {str(uid): TICKET_CAP},
    })
    cog = _make_cog()

    result = await cog._execute_purchase(1, uid, 1)

    assert result == {"error": "cap"}
    assert await _economy.get_balance(uid) == 1_000
    assert _state.economy["users"][str(uid)].get("lottery_disc_used", 0) == 0


@pytest.mark.asyncio
async def test_concurrent_cross_guild_purchases_share_one_discount_pool(db, monkeypatch):
    """The per-user discount counter is global while the purchase lock is
    per-guild, so two concurrent purchases in different guilds interleave at
    the charge await. The synchronous claim-before-charge must hand the
    discount to exactly one of them (CLAUDE.md concurrency pattern)."""
    _pin_clock(monkeypatch)
    uid = 9106
    await _economy.add_balance(uid, 10_000)

    async def _yielding_deduct(target_uid, n):
        await asyncio.sleep(0)  # force a real event-loop yield mid-purchase
        return await _economy.deduct_balance(target_uid, n)

    monkeypatch.setattr("src.cogs.lottery_cog.deduct_balance", _yielding_deduct)
    cog = _make_cog()

    r1, r2 = await asyncio.gather(
        cog._execute_purchase(1, uid, DISCOUNT_DAILY_CAP),
        cog._execute_purchase(2, uid, DISCOUNT_DAILY_CAP),
    )

    assert sorted([r1["discounted"], r2["discounted"]]) == [0, DISCOUNT_DAILY_CAP]
    assert _state.economy["users"][str(uid)]["lottery_disc_used"] == DISCOUNT_DAILY_CAP
    # One order at 50, the other at full 100.
    assert await _economy.get_balance(uid) == 10_000 - 50 - 100


# ── !lottery command integration ──────────────────────────────────────────────

def _lottery_ctx(uid=9201, guild_id=77):
    author = FakeMember(uid=uid, display_name="buyer")
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=guild_id), command_name="lottery")
    _state.guild_settings[str(guild_id)] = {"lottery_channel": 555}
    return ctx


@pytest.mark.asyncio
async def test_cmd_lottery_purchase_shows_half_price_breakdown(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx()
    await _economy.add_balance(ctx.author.id, 1_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "4")

    embed = ctx.sent_embeds[-1]
    assert embed.title == "🎰 Tickets Purchased"
    assert f"**{4 * DISCOUNT_TICKET_PRICE:,} 🪙** (4 at half price)" in embed.description
    assert _state.economy["users"][str(ctx.author.id)]["lottery_disc_used"] == 4


@pytest.mark.asyncio
async def test_cmd_lottery_info_shows_remaining_discounts(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9202)
    await _economy.add_balance(ctx.author.id, 1_000)
    cog = _make_cog()
    await cog.cmd_lottery.callback(cog, ctx, "3")

    await cog.cmd_lottery.callback(cog, ctx, None)

    info = ctx.sent_embeds[-1]
    assert f"(**{DISCOUNT_DAILY_CAP - 3}** left today)" in info.description
