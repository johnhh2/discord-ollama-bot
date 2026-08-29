"""Lottery ticket rework (src/cogs/lottery_cog.py, migration 0054).

Tickets now come from exactly three places:
- one 1,000 🪙 daily ticket per user per server, from the dailies 🎟️ button
  (buy_daily_ticket — the reaction path itself is covered in test_dailies.py)
  or the confirm prompt !lottery shows while today's ticket is unbought
- one free ticket per ISO week for beating a 500+ Elo chess bot
- one more free weekly ticket if the win was at 1100+ Elo

Gates live in state.lottery_ticket_grants (lottery_ticket_grants table),
claimed synchronously per the CLAUDE.md concurrency rules.
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
    LotteryCog, DAILY_TICKET_PRICE, TICKET_POOL_SHARE, TICKET_HOUSE_SHARE,
    NEW_PLAYER_POOL_BONUS,
)
from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

TODAY = "2026-05-02"
YESTERDAY = "2026-05-01"
WEEK = "2026-W18"
LAST_WEEK = "2026-W17"
# A mid-month noon — well clear of the 1st-of-month lock/draw windows.
NOW_CT = datetime.datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
GUILD_ID = 77


def _pin_clock(monkeypatch, today=TODAY, now=NOW_CT, week=WEEK):
    monkeypatch.setattr("src.cogs.lottery_cog._ct_today", lambda: today)
    monkeypatch.setattr("src.cogs.lottery_cog._ct_now", lambda: now)
    monkeypatch.setattr("src.cogs.lottery_cog.lottery_week_key", lambda: week)


def _make_cog() -> LotteryCog:
    cog = LotteryCog(SimpleNamespace(user=None, guilds=[]))
    cog.lottery_scheduler.cancel()
    return cog


def _lottery_ctx(uid=9201, guild_id=GUILD_ID):
    author = FakeMember(uid=uid, display_name="buyer")
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=guild_id), command_name="lottery")
    _state.guild_settings[str(guild_id)] = {"lottery_channel": 555}
    return ctx


def _channel_embeds(ctx):
    """Embeds buy_daily_ticket posted to ctx.channel (an AsyncMock)."""
    return [c.kwargs.get("embed") for c in ctx.channel.send.call_args_list]


def _all_titles(ctx):
    embeds = ctx.sent_embeds + [e for e in _channel_embeds(ctx) if e is not None]
    return [e.title for e in embeds]


# ── buy_daily_ticket ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_ticket_purchase_money_flow(db, monkeypatch):
    """One ticket for 1,000 🪙: pool +700 (+1,000 new player), house +300."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx()
    uid = ctx.author.id
    await _economy.add_balance(uid, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(uid) == 5_000 - DAILY_TICKET_PRICE
    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(uid)] == 1
    assert lot["prize_pool"] == TICKET_POOL_SHARE + NEW_PLAYER_POOL_BONUS
    assert _economy.get_guild_house_balance(GUILD_ID) == TICKET_HOUSE_SHARE
    assert _state.lottery_ticket_grants[(GUILD_ID, uid)]["daily_day"] == TODAY


@pytest.mark.asyncio
async def test_daily_ticket_second_buy_same_day_refused(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9202)
    uid = ctx.author.id
    await _economy.add_balance(uid, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)
    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(uid) == 5_000 - DAILY_TICKET_PRICE
    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(uid)] == 1
    titles = [e.title for e in _channel_embeds(ctx)]
    assert "🎟️ Daily Ticket Already Bought" in titles


@pytest.mark.asyncio
async def test_daily_ticket_gate_is_per_guild(db, monkeypatch):
    """Once per day per SERVER — buying in guild A doesn't block guild B."""
    _pin_clock(monkeypatch)
    ctx_a = _lottery_ctx(uid=9203, guild_id=77)
    ctx_b = _lottery_ctx(uid=9203, guild_id=88)
    uid = 9203
    await _economy.add_balance(uid, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx_a.author, ctx_a.channel, ctx_a.guild)
    await cog.buy_daily_ticket(ctx_b.author, ctx_b.channel, ctx_b.guild)

    assert await _economy.get_balance(uid) == 5_000 - 2 * DAILY_TICKET_PRICE
    assert (await _persistence.load_lottery(77))["players"][str(uid)] == 1
    assert (await _persistence.load_lottery(88))["players"][str(uid)] == 1


@pytest.mark.asyncio
async def test_daily_ticket_gate_rolls_over_next_day(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9204)
    uid = ctx.author.id
    await _economy.add_balance(uid, 5_000)
    cog = _make_cog()
    _state.lottery_ticket_grants[(GUILD_ID, uid)] = {
        "daily_day": YESTERDAY, "chess_week_500": None, "chess_week_1100": None,
    }

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 1
    assert _state.lottery_ticket_grants[(GUILD_ID, uid)]["daily_day"] == TODAY


@pytest.mark.asyncio
async def test_daily_ticket_insufficient_funds_rolls_back_gate(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9205)
    uid = ctx.author.id
    await _economy.add_balance(uid, DAILY_TICKET_PRICE - 1)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(uid) == DAILY_TICKET_PRICE - 1
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}
    # The gate is rolled back — an afternoon payday still buys today's ticket.
    assert _state.lottery_ticket_grants[(GUILD_ID, uid)]["daily_day"] is None
    titles = [e.title for e in _channel_embeds(ctx)]
    assert "💸 Insufficient Funds" in titles


@pytest.mark.asyncio
async def test_concurrent_daily_ticket_buys_grant_one(db, monkeypatch):
    """Two racing invocations must produce one ticket and one charge — the
    gate is claimed synchronously before the first await (CLAUDE.md rules).
    deduct_balance is wrapped with a real event-loop yield so the fake DB's
    synchronous saves can't mask the interleave."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9206)
    uid = ctx.author.id
    await _economy.add_balance(uid, 5_000)
    cog = _make_cog()

    real_deduct = _economy.deduct_balance

    async def _yielding_deduct(*args, **kwargs):
        await asyncio.sleep(0)
        return await real_deduct(*args, **kwargs)

    monkeypatch.setattr("src.cogs.lottery_cog.deduct_balance", _yielding_deduct)

    await asyncio.gather(
        cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild),
        cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild),
    )

    assert await _economy.get_balance(uid) == 5_000 - DAILY_TICKET_PRICE
    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 1


@pytest.mark.asyncio
async def test_daily_ticket_without_lottery_channel_refused(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9207)
    _state.guild_settings[str(GUILD_ID)] = {}
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(ctx.author.id) == 5_000
    titles = [e.title for e in _channel_embeds(ctx)]
    assert "🎰 Lottery Disabled" in titles


@pytest.mark.asyncio
async def test_daily_ticket_locked_final_hour_before_draw(db, monkeypatch):
    locked_now = datetime.datetime(2026, 5, 1, 17, 30, tzinfo=ZoneInfo("America/Chicago"))
    _pin_clock(monkeypatch, now=locked_now)
    ctx = _lottery_ctx(uid=9208)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(ctx.author.id) == 5_000
    titles = [e.title for e in _channel_embeds(ctx)]
    assert "🔒 Lottery Locked" in titles
    assert _state.lottery_ticket_grants.get((GUILD_ID, ctx.author.id), {}).get("daily_day") is None


# ── !lottery: info + confirm prompt ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_lottery_confirm_buys_daily_ticket(db, monkeypatch):
    """!lottery with today's ticket unbought shows the info embed, then the
    (auto-accepted) confirm prompt buys the ticket."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9210)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx)

    info = ctx.sent_embeds[0]
    assert info.title.startswith("🎰 Current Lottery")
    assert "available" in info.description
    assert await _economy.get_balance(ctx.author.id) == 5_000 - DAILY_TICKET_PRICE
    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(ctx.author.id)] == 1
    assert "🎰 Daily Ticket Purchased" in _all_titles(ctx)


@pytest.mark.asyncio
async def test_cmd_lottery_confirm_declined_buys_nothing(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9211)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    async def _decline(*args, **kwargs):
        return False
    monkeypatch.setattr("src.cogs.lottery_cog.confirm_purchase", _decline)

    await cog.cmd_lottery.callback(cog, ctx)

    assert await _economy.get_balance(ctx.author.id) == 5_000
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}
    assert _state.lottery_ticket_grants.get((GUILD_ID, ctx.author.id), {}).get("daily_day") is None


@pytest.mark.asyncio
async def test_cmd_lottery_after_purchase_shows_bought_and_skips_confirm(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9212)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()
    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    confirm_calls = []

    async def _spy_confirm(*args, **kwargs):
        confirm_calls.append(1)
        return True
    monkeypatch.setattr("src.cogs.lottery_cog.confirm_purchase", _spy_confirm)

    await cog.cmd_lottery.callback(cog, ctx)

    info = ctx.sent_embeds[-1]
    assert "✅ bought" in info.description
    assert confirm_calls == []
    assert await _economy.get_balance(ctx.author.id) == 5_000 - DAILY_TICKET_PRICE


@pytest.mark.asyncio
async def test_cmd_lottery_shows_ticket_counts(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9213)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()
    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)
    await cog.award_chess_tickets(ctx.guild, ctx.author.id, 1200)

    await cog.cmd_lottery.callback(cog, ctx)

    info = ctx.sent_embeds[-1]
    assert "**Your Tickets:** 3 / 3 total" in info.description


# ── weekly chess-win tickets ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chess_win_500_grants_one_free_ticket(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9220)
    uid = ctx.author.id
    cog = _make_cog()

    granted = await cog.award_chess_tickets(ctx.guild, uid, 500)

    assert granted == 1
    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(uid)] == 1
    # Free ticket: no pool share beyond the new-player bonus, no house cut.
    assert lot["prize_pool"] == NEW_PLAYER_POOL_BONUS
    assert _economy.get_guild_house_balance(GUILD_ID) == 0
    row = _state.lottery_ticket_grants[(GUILD_ID, uid)]
    assert row["chess_week_500"] == WEEK
    assert row["chess_week_1100"] is None


@pytest.mark.asyncio
async def test_chess_win_1100_grants_both_tiers(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9221)
    uid = ctx.author.id
    cog = _make_cog()

    granted = await cog.award_chess_tickets(ctx.guild, uid, 1100)

    assert granted == 2
    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 2
    row = _state.lottery_ticket_grants[(GUILD_ID, uid)]
    assert row["chess_week_500"] == WEEK
    assert row["chess_week_1100"] == WEEK


@pytest.mark.asyncio
async def test_chess_win_below_500_grants_nothing(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9222)
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, ctx.author.id, 400) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}


@pytest.mark.asyncio
async def test_chess_tiers_claim_once_per_week(db, monkeypatch):
    """A second 500+ win in the same week grants nothing; a later 1100+ win
    still claims the unclaimed 1100 tier."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9223)
    uid = ctx.author.id
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, uid, 700) == 1
    assert await cog.award_chess_tickets(ctx.guild, uid, 900) == 0
    assert await cog.award_chess_tickets(ctx.guild, uid, 1500) == 1
    assert await cog.award_chess_tickets(ctx.guild, uid, 1500) == 0

    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 2


@pytest.mark.asyncio
async def test_chess_tickets_reset_on_new_week(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9224)
    uid = ctx.author.id
    cog = _make_cog()
    _state.lottery_ticket_grants[(GUILD_ID, uid)] = {
        "daily_day": None, "chess_week_500": LAST_WEEK, "chess_week_1100": LAST_WEEK,
    }

    assert await cog.award_chess_tickets(ctx.guild, uid, 1200) == 2
    row = _state.lottery_ticket_grants[(GUILD_ID, uid)]
    assert row["chess_week_500"] == WEEK
    assert row["chess_week_1100"] == WEEK


@pytest.mark.asyncio
async def test_chess_tickets_skipped_when_lottery_disabled(db, monkeypatch):
    """No lottery channel → no grant, and the weekly gate isn't burned."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9225)
    _state.guild_settings[str(GUILD_ID)] = {}
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, ctx.author.id, 1500) == 0
    assert _state.lottery_ticket_grants.get((GUILD_ID, ctx.author.id)) is None


# ── persistence round-trip ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ticket_grants_survive_reboot(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9230)
    uid = ctx.author.id
    await _economy.add_balance(uid, 5_000)
    cog = _make_cog()
    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)
    await cog.award_chess_tickets(ctx.guild, uid, 1100)

    _state.lottery_ticket_grants = {}
    await _persistence.init_db_state()

    row = _state.lottery_ticket_grants[(GUILD_ID, uid)]
    assert row == {"daily_day": TODAY, "chess_week_500": WEEK, "chess_week_1100": WEEK}
