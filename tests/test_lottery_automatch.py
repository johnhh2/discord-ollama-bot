"""Lottery automatch (src/cogs/lottery_cog.py, migration 0044).

`!lottery automatch <max>` opts a user in: whenever another player's ticket
total passes theirs, the bot auto-buys tickets on their behalf to tie that
total, never raising their own count above `<max>`. Opt-ins live in the
lottery_automatch table and last one lottery: `!lottery automatch off`
clears one user's, and the monthly draw wipes the whole guild's.
"""
import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import src.state as _state
import src.economy as _economy
import src.persistence as _persistence
from src.cogs.lottery_cog import (
    LotteryCog, max_affordable_tickets,
    TICKET_PRICE, DISCOUNT_TICKET_PRICE, DISCOUNT_DAILY_CAP, TICKET_CAP,
)
from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

TODAY = "2026-05-02"
# A mid-month noon — well clear of the 1st-of-month lock/draw windows.
NOW_CT = datetime.datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
GUILD_ID = 77


def _pin_clock(monkeypatch, today=TODAY, now=NOW_CT):
    monkeypatch.setattr("src.cogs.lottery_cog._ct_today", lambda: today)
    monkeypatch.setattr("src.cogs.lottery_cog._ct_now", lambda: now)


def _make_cog() -> LotteryCog:
    cog = LotteryCog(SimpleNamespace(user=None, guilds=[]))
    cog.lottery_scheduler.cancel()
    return cog


def _lottery_ctx(uid, guild_id=GUILD_ID, name="buyer"):
    author = FakeMember(uid=uid, display_name=name)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=guild_id), command_name="lottery")
    _state.guild_settings[str(guild_id)] = {"lottery_channel": 555}
    return ctx


def _automatch_embeds(ctx):
    """Embeds the automatch pass posted to the buyer's channel."""
    return [
        call.kwargs["embed"]
        for call in ctx.channel.send.call_args_list
        if call.kwargs.get("embed") is not None and call.kwargs["embed"].title == "🎯 Automatch"
    ]


# ── max_affordable_tickets ────────────────────────────────────────────────────

def test_affordable_all_half_price():
    # 40 coins, 10 discounts left: 8 half-price tickets, can't start full price.
    assert max_affordable_tickets(40, DISCOUNT_DAILY_CAP) == 8


def test_affordable_spans_discount_boundary():
    # 10 half price (50) + 7 full price (70) = 120 coins.
    assert max_affordable_tickets(120, DISCOUNT_DAILY_CAP) == 17


def test_affordable_no_discount_left():
    assert max_affordable_tickets(95, 0) == 9


def test_affordable_broke():
    assert max_affordable_tickets(4, DISCOUNT_DAILY_CAP) == 0


# ── !lottery automatch set / show / off ───────────────────────────────────────

@pytest.mark.asyncio
async def test_automatch_set_persists_row(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9301)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "automatch", "2.5k")

    assert ctx.sent_embeds[-1].title == "🎯 Automatch Enabled"
    assert await _persistence.load_lottery_automatch(GUILD_ID) == {"9301": 2500}


@pytest.mark.asyncio
async def test_automatch_max_clamped_to_ticket_cap(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9302)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "automatch", "1m")

    assert (await _persistence.load_lottery_automatch(GUILD_ID))["9302"] == TICKET_CAP


@pytest.mark.asyncio
async def test_automatch_off_deletes_row(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9303)
    cog = _make_cog()
    await cog.cmd_lottery.callback(cog, ctx, "automatch", "100")

    await cog.cmd_lottery.callback(cog, ctx, "automatch", "off")

    assert ctx.sent_embeds[-1].title == "🎯 Automatch Disabled"
    assert await _persistence.load_lottery_automatch(GUILD_ID) == {}


@pytest.mark.asyncio
async def test_automatch_show_reflects_state(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9304)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "automatch", None)
    assert "Automatch is **off**" in ctx.sent_embeds[-1].description

    await cog.cmd_lottery.callback(cog, ctx, "automatch", "150")
    await cog.cmd_lottery.callback(cog, ctx, "automatch", None)
    assert "Automatch is **on**" in ctx.sent_embeds[-1].description
    assert "**150**" in ctx.sent_embeds[-1].description


@pytest.mark.asyncio
async def test_automatch_rejects_garbage_amount(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9305)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "automatch", "banana")

    assert ctx.sent_embeds[-1].title == "❌ Invalid Amount"
    assert await _persistence.load_lottery_automatch(GUILD_ID) == {}


# ── auto-buy trigger ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purchase_triggers_automatch_to_tie_buyer(db, monkeypatch):
    _pin_clock(monkeypatch)
    matcher_uid = 9310
    await _economy.add_balance(matcher_uid, 10_000)
    await _economy._ensure_user(matcher_uid)
    await _persistence.save_lottery_automatch(GUILD_ID, matcher_uid, 500)

    ctx = _lottery_ctx(uid=9311)
    await _economy.add_balance(ctx.author.id, 10_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "30")

    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(ctx.author.id)] == 30
    assert lot["players"][str(matcher_uid)] == 30
    # 10 half price + 20 full price = 250 coins.
    assert await _economy.get_balance(matcher_uid) == 10_000 - (
        DISCOUNT_DAILY_CAP * DISCOUNT_TICKET_PRICE + 20 * TICKET_PRICE
    )
    embeds = _automatch_embeds(ctx)
    assert len(embeds) == 1
    assert f"<@{matcher_uid}>" in embeds[0].description


@pytest.mark.asyncio
async def test_automatch_capped_at_users_max(db, monkeypatch):
    _pin_clock(monkeypatch)
    matcher_uid = 9320
    await _economy.add_balance(matcher_uid, 100_000)
    await _persistence.save_lottery_automatch(GUILD_ID, matcher_uid, 50)

    ctx = _lottery_ctx(uid=9321)
    await _economy.add_balance(ctx.author.id, 100_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "200")

    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(matcher_uid)] == 50


@pytest.mark.asyncio
async def test_automatch_partial_when_balance_runs_out(db, monkeypatch):
    _pin_clock(monkeypatch)
    matcher_uid = 9330
    # 10 half price (50) + 5 full price (50) = 100 coins → 15 tickets max.
    await _economy.add_balance(matcher_uid, 100)
    await _persistence.save_lottery_automatch(GUILD_ID, matcher_uid, 500)

    ctx = _lottery_ctx(uid=9331)
    await _economy.add_balance(ctx.author.id, 10_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "100")

    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(matcher_uid)] == 15
    assert await _economy.get_balance(matcher_uid) == 0
    assert "short" in _automatch_embeds(ctx)[0].description


@pytest.mark.asyncio
async def test_automatch_reports_broke_matcher_without_buying(db, monkeypatch):
    _pin_clock(monkeypatch)
    matcher_uid = 9340
    await _economy.add_balance(matcher_uid, 3)  # can't afford even one half-price ticket
    await _persistence.save_lottery_automatch(GUILD_ID, matcher_uid, 500)

    ctx = _lottery_ctx(uid=9341)
    await _economy.add_balance(ctx.author.id, 10_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "10")

    lot = await _persistence.load_lottery(GUILD_ID)
    assert str(matcher_uid) not in lot["players"]
    assert await _economy.get_balance(matcher_uid) == 3
    assert "couldn't afford" in _automatch_embeds(ctx)[0].description


@pytest.mark.asyncio
async def test_buyers_own_automatch_does_not_self_trigger(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9350)
    await _economy.add_balance(ctx.author.id, 10_000)
    await _persistence.save_lottery_automatch(GUILD_ID, ctx.author.id, 500)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "20")

    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(ctx.author.id)] == 20
    assert _automatch_embeds(ctx) == []


@pytest.mark.asyncio
async def test_automatch_idle_when_matcher_already_ahead(db, monkeypatch):
    _pin_clock(monkeypatch)
    matcher_uid = 9360
    await _economy.add_balance(matcher_uid, 10_000)
    await _persistence.save_lottery_automatch(GUILD_ID, matcher_uid, 500)
    await _persistence.save_lottery(GUILD_ID, {
        "prize_pool": 0, "last_posted_week": 0, "last_drawn_week": 0,
        "players": {str(matcher_uid): 100},
    })

    ctx = _lottery_ctx(uid=9361)
    await _economy.add_balance(ctx.author.id, 10_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "40")

    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(matcher_uid)] == 100
    assert await _economy.get_balance(matcher_uid) == 10_000
    assert _automatch_embeds(ctx) == []


@pytest.mark.asyncio
async def test_automatch_is_per_guild(db, monkeypatch):
    _pin_clock(monkeypatch)
    matcher_uid = 9370
    await _economy.add_balance(matcher_uid, 10_000)
    await _persistence.save_lottery_automatch(99, matcher_uid, 500)  # other guild

    ctx = _lottery_ctx(uid=9371)
    await _economy.add_balance(ctx.author.id, 10_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx, "25")

    lot = await _persistence.load_lottery(GUILD_ID)
    assert str(matcher_uid) not in lot["players"]
    assert await _economy.get_balance(matcher_uid) == 10_000


@pytest.mark.asyncio
async def test_draw_clears_all_automatch_optins(db, monkeypatch):
    """The monthly draw resets the pool AND wipes the guild's automatch rows."""
    from unittest.mock import AsyncMock
    from tests.fakes.discord import FakeChannel

    await _persistence.save_lottery_automatch(GUILD_ID, 9390, 500)
    await _persistence.save_lottery_automatch(GUILD_ID, 9391, 50)
    await _persistence.save_lottery_automatch(99, 9392, 100)  # other guild untouched

    guild = FakeGuild(gid=GUILD_ID)
    _state.guild_settings[str(GUILD_ID)] = {"lottery_channel": 555}
    cog = _make_cog()
    cog.bot = SimpleNamespace(
        user=None, guilds=[guild],
        fetch_channel=AsyncMock(return_value=FakeChannel(guild=guild)),
    )
    # 1st of the month, 6pm CT — draw fires; empty player pool skips the payout.
    draw_now = datetime.datetime(2026, 6, 1, 18, 5, tzinfo=ZoneInfo("America/Chicago"))

    await cog._run_guild_schedule(guild, draw_now)

    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["last_drawn_week"] == 202606
    assert await _persistence.load_lottery_automatch(GUILD_ID) == {}
    assert await _persistence.load_lottery_automatch(99) == {"9392": 100}


@pytest.mark.asyncio
async def test_dailies_discount_buy_triggers_automatch(db, monkeypatch):
    """The dailies 🎟️ reaction path (buy_discounted_tickets) also triggers it."""
    _pin_clock(monkeypatch)
    matcher_uid = 9380
    await _economy.add_balance(matcher_uid, 10_000)
    await _persistence.save_lottery_automatch(GUILD_ID, matcher_uid, 500)

    buyer = FakeMember(uid=9381, display_name="daily-buyer")
    await _economy.add_balance(buyer.id, 10_000)
    guild = FakeGuild(gid=GUILD_ID)
    _state.guild_settings[str(GUILD_ID)] = {"lottery_channel": 555}
    from tests.fakes.discord import FakeChannel
    channel = FakeChannel(guild=guild)
    cog = _make_cog()

    await cog.buy_discounted_tickets(buyer, channel, guild)

    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(buyer.id)] == DISCOUNT_DAILY_CAP
    assert lot["players"][str(matcher_uid)] == DISCOUNT_DAILY_CAP
