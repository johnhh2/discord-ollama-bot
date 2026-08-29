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

TODAY = "2026-10-02"
YESTERDAY = "2026-10-01"
WEEK = "2026-W40"
LAST_WEEK = "2026-W39"
# A mid-month noon — well clear of the 1st-of-month lock/draw windows and
# past the one-time TICKET_SALES_START_CT launch gate (9/1/2026).
NOW_CT = datetime.datetime(2026, 10, 2, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
# Before the launch gate (and clear of the lock/draw windows).
PRE_LAUNCH_NOW_CT = datetime.datetime(2026, 8, 15, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
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
    locked_now = datetime.datetime(2026, 11, 1, 17, 30, tzinfo=ZoneInfo("America/Chicago"))
    _pin_clock(monkeypatch, now=locked_now)
    ctx = _lottery_ctx(uid=9208)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(ctx.author.id) == 5_000
    titles = [e.title for e in _channel_embeds(ctx)]
    assert "🔒 Lottery Locked" in titles
    assert _state.lottery_ticket_grants.get((GUILD_ID, ctx.author.id), {}).get("daily_day") is None


# ── one-time launch gate: no tickets until the 9/1/2026 draw ─────────────────

@pytest.mark.asyncio
async def test_daily_ticket_paused_before_september_relaunch(db, monkeypatch):
    """The pre-rework pot is full of old 10-coin bulk tickets — no 1,000 🪙
    sales into it. Gate unburned, nothing charged."""
    _pin_clock(monkeypatch, now=PRE_LAUNCH_NOW_CT)
    ctx = _lottery_ctx(uid=9240)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(ctx.author.id) == 5_000
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}
    assert _state.lottery_ticket_grants.get((GUILD_ID, ctx.author.id), {}).get("daily_day") is None
    titles = [e.title for e in _channel_embeds(ctx)]
    assert "🔒 Ticket Sales Paused" in titles


@pytest.mark.asyncio
async def test_chess_tickets_paused_before_september_relaunch(db, monkeypatch):
    """Free chess tickets are held back too, without burning the weekly gates."""
    _pin_clock(monkeypatch, now=PRE_LAUNCH_NOW_CT)
    ctx = _lottery_ctx(uid=9241)
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, ctx.author.id, 1500) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}
    assert _state.lottery_ticket_grants.get((GUILD_ID, ctx.author.id)) is None


@pytest.mark.asyncio
async def test_cmd_lottery_paused_shows_info_without_confirm(db, monkeypatch):
    _pin_clock(monkeypatch, now=PRE_LAUNCH_NOW_CT)
    ctx = _lottery_ctx(uid=9242)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    confirm_calls = []

    async def _spy_confirm(*args, **kwargs):
        confirm_calls.append(1)
        return True
    monkeypatch.setattr("src.cogs.lottery_cog.confirm_purchase", _spy_confirm)

    await cog.cmd_lottery.callback(cog, ctx)

    info = ctx.sent_embeds[-1]
    assert info.title.startswith("🎰 Current Lottery")
    assert "🔒 paused" in info.description
    assert confirm_calls == []
    assert await _economy.get_balance(ctx.author.id) == 5_000


@pytest.mark.asyncio
async def test_tickets_flow_normally_after_relaunch_moment(db, monkeypatch):
    """The instant the 9/1/2026 6pm CT draw time passes, sales are open (the
    scheduler's draw runs in the same minute and resets the pot)."""
    just_after = datetime.datetime(2026, 9, 1, 19, 0, tzinfo=ZoneInfo("America/Chicago"))
    _pin_clock(monkeypatch, now=just_after)
    ctx = _lottery_ctx(uid=9243)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(ctx.author.id)] == 1
    assert await cog.award_chess_tickets(ctx.guild, ctx.author.id, 1200) == 3


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
    assert "**Your Tickets:** 4 / 4 total" in info.description


# ── weekly chess-win tickets ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_any_chess_win_grants_one_free_ticket(db, monkeypatch):
    """A low-Elo bot win tops the winner up to ceiling 1, with no pool share
    beyond the new-player bonus and no house cut."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9220)
    uid = ctx.author.id
    cog = _make_cog()

    granted = await cog.award_chess_tickets(ctx.guild, uid, 100)

    assert granted == 1
    lot = await _persistence.load_lottery(GUILD_ID)
    assert lot["players"][str(uid)] == 1
    assert lot["prize_pool"] == NEW_PLAYER_POOL_BONUS
    assert _economy.get_guild_house_balance(GUILD_ID) == 0
    row = _state.lottery_ticket_grants[(GUILD_ID, uid)]
    assert row["chess_week"] == WEEK
    assert row["chess_tickets"] == 1


@pytest.mark.asyncio
async def test_pvp_chess_win_grants_one_free_ticket(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9221)
    uid = ctx.author.id
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, uid, None) == 1
    assert await cog.award_chess_tickets(ctx.guild, uid, None) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 1


@pytest.mark.asyncio
async def test_chess_bot_tier_ceilings(db, monkeypatch):
    """600+ tops up to 2, 1100+ to 3 — a first 1100+ win pays all 3 at once."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9222)
    uid = ctx.author.id
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, uid, 1500) == 3
    assert await cog.award_chess_tickets(ctx.guild, uid, 1500) == 0
    assert await cog.award_chess_tickets(ctx.guild, uid, 100) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 3
    assert _state.lottery_ticket_grants[(GUILD_ID, uid)]["chess_tickets"] == 3


@pytest.mark.asyncio
async def test_chess_wins_top_up_not_stack(db, monkeypatch):
    """Beating 100 Elo then 600 Elo pays 1 + 1 (not 1 + 2); a later 1100+
    win adds only the last 1. Total never passes 3/week."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9223)
    uid = ctx.author.id
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, uid, 100) == 1
    assert await cog.award_chess_tickets(ctx.guild, uid, 600) == 1
    assert await cog.award_chess_tickets(ctx.guild, uid, 900) == 0
    assert await cog.award_chess_tickets(ctx.guild, uid, 1100) == 1
    assert await cog.award_chess_tickets(ctx.guild, uid, 1900) == 0

    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 3


@pytest.mark.asyncio
async def test_chess_tickets_reset_on_new_week(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9224)
    uid = ctx.author.id
    cog = _make_cog()
    _state.lottery_ticket_grants[(GUILD_ID, uid)] = {
        "daily_day": None, "chess_week": LAST_WEEK, "chess_tickets": 3,
    }

    assert await cog.award_chess_tickets(ctx.guild, uid, 1200) == 3
    row = _state.lottery_ticket_grants[(GUILD_ID, uid)]
    assert row["chess_week"] == WEEK
    assert row["chess_tickets"] == 3


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
    assert row == {"daily_day": TODAY, "chess_week": WEEK, "chess_tickets": 3}
