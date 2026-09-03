"""Lottery ticket rework (src/cogs/lottery_cog.py, migration 0054).

Tickets now come from exactly two places:
- one 1,000 🪙 daily ticket per user per server, from the dailies 🎟️ button
  (buy_daily_ticket — the reaction path itself is covered in test_dailies.py)
  or the confirm prompt !lottery shows while today's ticket is unbought
- up to 2 free tickets per lottery for beating a 600+ Elo chess bot (a
  global cap — wins in any server count against it; PvP and sub-600 wins
  grant nothing). The window follows the lottery schedule: it resets at the
  1st-of-month 6pm CT draw (lottery_period_key).

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
# lottery_period_key for NOW_CT: the lottery that opened at the 10/1 draw.
PERIOD = "2026-10"
LAST_PERIOD = "2026-09"
# A mid-month noon — well clear of the 1st-of-month lock/draw windows and
# past the one-time TICKET_SALES_START_CT launch gate (9/1/2026).
NOW_CT = datetime.datetime(2026, 10, 2, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
# Before the launch gate (and clear of the lock/draw windows).
PRE_LAUNCH_NOW_CT = datetime.datetime(2026, 8, 15, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
GUILD_ID = 77


def _pin_clock(monkeypatch, today=TODAY, now=NOW_CT):
    monkeypatch.setattr("src.cogs.lottery_cog._ct_today", lambda: today)
    monkeypatch.setattr("src.cogs.lottery_cog._ct_now", lambda: now)


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
        "daily_day": YESTERDAY, "chess_period": None, "chess_tickets": 0,
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
    """Free chess tickets are held back too, without burning the monthly gates."""
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
    assert await cog.award_chess_tickets(ctx.guild, ctx.author.id, 1200) == 2


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


# ── monthly chess-win tickets ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_low_elo_chess_win_grants_nothing(db, monkeypatch):
    """Sub-600 bot wins are worth no tickets and don't touch the monthly gate."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9220)
    uid = ctx.author.id
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, uid, 100) == 0
    assert await cog.award_chess_tickets(ctx.guild, uid, 599) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}
    assert _state.lottery_ticket_grants.get((GUILD_ID, uid)) is None


@pytest.mark.asyncio
async def test_pvp_chess_win_grants_nothing(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9221)
    uid = ctx.author.id
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, uid, None) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}
    assert _state.lottery_ticket_grants.get((GUILD_ID, uid)) is None


@pytest.mark.asyncio
async def test_chess_bot_tier_ceilings(db, monkeypatch):
    """A 600+ win tops up to 2 at once; the lottery is then capped."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9222)
    uid = ctx.author.id
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, uid, 1500) == 2
    assert await cog.award_chess_tickets(ctx.guild, uid, 1500) == 0
    assert await cog.award_chess_tickets(ctx.guild, uid, 100) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 2
    assert _state.lottery_ticket_grants[(GUILD_ID, uid)]["chess_tickets"] == 2


@pytest.mark.asyncio
async def test_chess_wins_top_up_not_stack(db, monkeypatch):
    """A 600 win pays the full 2; further wins at any strength add nothing."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9223)
    uid = ctx.author.id
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, uid, 600) == 2
    assert await cog.award_chess_tickets(ctx.guild, uid, 900) == 0
    assert await cog.award_chess_tickets(ctx.guild, uid, 1900) == 0

    assert (await _persistence.load_lottery(GUILD_ID))["players"][str(uid)] == 2


@pytest.mark.asyncio
async def test_chess_monthly_cap_is_global_across_guilds(db, monkeypatch):
    """Tickets won in one server count against the monthly cap everywhere —
    a second server's 600+ win grants nothing more."""
    _pin_clock(monkeypatch)
    ctx_a = _lottery_ctx(uid=9226, guild_id=77)
    ctx_b = _lottery_ctx(uid=9226, guild_id=88)
    uid = 9226
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx_a.guild, uid, 700) == 2
    assert await cog.award_chess_tickets(ctx_b.guild, uid, 1500) == 0
    assert (await _persistence.load_lottery(77))["players"][str(uid)] == 2
    assert (await _persistence.load_lottery(88))["players"] == {}
    assert _state.lottery_ticket_grants.get((88, uid), {}).get("chess_tickets", 0) == 0


@pytest.mark.asyncio
async def test_chess_tickets_reset_on_new_lottery(db, monkeypatch):
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9224)
    uid = ctx.author.id
    cog = _make_cog()
    _state.lottery_ticket_grants[(GUILD_ID, uid)] = {
        "daily_day": None, "chess_period": LAST_PERIOD, "chess_tickets": 2,
    }

    assert await cog.award_chess_tickets(ctx.guild, uid, 1200) == 2
    row = _state.lottery_ticket_grants[(GUILD_ID, uid)]
    assert row["chess_period"] == PERIOD
    assert row["chess_tickets"] == 2


@pytest.mark.asyncio
async def test_chess_tickets_follow_the_draw_boundary(db, monkeypatch):
    """The window matches the lottery schedule: a win on the 1st before the
    6pm CT draw still counts against the outgoing lottery's ceiling (and pays
    into its pot); the draw itself opens a fresh ceiling."""
    ct = ZoneInfo("America/Chicago")
    ctx = _lottery_ctx(uid=9227)
    uid = ctx.author.id
    cog = _make_cog()
    _state.lottery_ticket_grants[(GUILD_ID, uid)] = {
        "daily_day": None, "chess_period": PERIOD, "chess_tickets": 2,
    }

    _pin_clock(monkeypatch, now=datetime.datetime(2026, 11, 1, 10, 0, tzinfo=ct))
    assert await cog.award_chess_tickets(ctx.guild, uid, 1500) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}

    _pin_clock(monkeypatch, now=datetime.datetime(2026, 11, 1, 18, 30, tzinfo=ct))
    assert await cog.award_chess_tickets(ctx.guild, uid, 1500) == 2
    row = _state.lottery_ticket_grants[(GUILD_ID, uid)]
    assert row["chess_period"] == "2026-11"
    assert row["chess_tickets"] == 2


@pytest.mark.asyncio
async def test_chess_tickets_skipped_when_lottery_disabled(db, monkeypatch):
    """No lottery channel → no grant, and the monthly gate isn't burned."""
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
    assert row == {"daily_day": TODAY, "chess_period": PERIOD, "chess_tickets": 2}


@pytest.mark.asyncio
async def test_chess_tickets_granted_this_lottery_sums_across_guilds(db, monkeypatch):
    """The !chessbot ladder's count: this lottery's grants in every guild,
    ignoring stale periods and other users."""
    from src.cogs.lottery_cog import chess_tickets_granted_this_lottery
    _pin_clock(monkeypatch)
    uid = 9230
    _state.lottery_ticket_grants[(77, uid)] = {"daily_day": None, "chess_period": PERIOD, "chess_tickets": 1}
    _state.lottery_ticket_grants[(88, uid)] = {"daily_day": None, "chess_period": PERIOD, "chess_tickets": 1}
    _state.lottery_ticket_grants[(99, uid)] = {"daily_day": None, "chess_period": LAST_PERIOD, "chess_tickets": 2}
    _state.lottery_ticket_grants[(77, 9231)] = {"daily_day": None, "chess_period": PERIOD, "chess_tickets": 2}

    assert chess_tickets_granted_this_lottery(uid) == 2
    assert chess_tickets_granted_this_lottery(9232) == 0


def test_lottery_period_key_flips_at_the_first_of_month_draw():
    """The chess-ticket window is the lottery itself: a new period opens at
    the 1st-of-month 6pm CT draw — not at midnight, not at the 5am reset —
    and December's pot (drawn 1/1) rolls the year."""
    from src.economy import lottery_period_key
    ct = ZoneInfo("America/Chicago")

    def key(*args):
        return lottery_period_key(datetime.datetime(*args, tzinfo=ct))

    # Mid-month: the lottery that opened on the 1st.
    assert key(2026, 10, 15, 12, 0) == "2026-10"
    # The 1st before the draw — past midnight and the 5am reset — is still
    # last month's pot.
    assert key(2026, 11, 1, 0, 30) == "2026-10"
    assert key(2026, 11, 1, 5, 30) == "2026-10"
    assert key(2026, 11, 1, 17, 59) == "2026-10"
    # The draw opens the new period.
    assert key(2026, 11, 1, 18, 0) == "2026-11"
    assert key(2026, 11, 1, 18, 30) == "2026-11"
    # Year rollover.
    assert key(2026, 12, 20, 12, 0) == "2026-12"
    assert key(2027, 1, 1, 17, 0) == "2026-12"
    assert key(2027, 1, 1, 18, 0) == "2027-01"


# -- Bots are shut out of every lottery function ------------------------------

def _bot_ctx(uid, guild_id=GUILD_ID):
    """A !lottery ctx whose author is a bot account, present in the guild's
    member cache (what _is_bot_user consults first)."""
    ctx = _lottery_ctx(uid=uid, guild_id=guild_id)
    ctx.author.bot = True
    ctx.guild.members.append(ctx.author)
    return ctx


@pytest.mark.asyncio
async def test_bot_user_cannot_buy_daily_ticket(db, monkeypatch):
    """buy_daily_ticket backs both the dailies reaction and !lottery: a bot
    member is refused before any charge, ticket, or gate row."""
    _pin_clock(monkeypatch)
    ctx = _bot_ctx(uid=9260)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    await cog.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)

    assert await _economy.get_balance(ctx.author.id) == 5_000
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}
    assert _state.lottery_ticket_grants.get((GUILD_ID, ctx.author.id)) is None
    assert any("Bots" in (e.description or "") for e in _channel_embeds(ctx) if e is not None)


@pytest.mark.asyncio
async def test_cmd_lottery_refuses_bot_author(db, monkeypatch):
    """!lottery from a bot account: refusal only -- no info embed, no confirm
    prompt, no purchase."""
    _pin_clock(monkeypatch)
    ctx = _bot_ctx(uid=9261)
    await _economy.add_balance(ctx.author.id, 5_000)
    cog = _make_cog()

    await cog.cmd_lottery.callback(cog, ctx)

    assert "Bots" in ctx.sent_embeds[-1].description
    assert not any(t.startswith("\U0001f3b0 Current Lottery") for t in _all_titles(ctx))
    assert await _economy.get_balance(ctx.author.id) == 5_000
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}


@pytest.mark.asyncio
async def test_bot_chess_win_grants_no_tickets(db, monkeypatch):
    """A bot account beating a 1500 chess bot earns no free tickets, and its
    monthly gate row is never created."""
    _pin_clock(monkeypatch)
    ctx = _bot_ctx(uid=9262)
    cog = _make_cog()

    assert await cog.award_chess_tickets(ctx.guild, ctx.author.id, 1500) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}
    assert _state.lottery_ticket_grants.get((GUILD_ID, ctx.author.id)) is None


@pytest.mark.asyncio
async def test_bot_lookup_falls_back_to_client_user_cache(db, monkeypatch):
    """When the guild member cache misses, the client's user cache decides;
    an id neither knows counts as human."""
    _pin_clock(monkeypatch)
    ctx = _lottery_ctx(uid=9263)  # deliberately NOT in guild.members
    cog = _make_cog()
    cog.bot = SimpleNamespace(
        user=None, guilds=[],
        get_user=lambda uid: SimpleNamespace(id=uid, bot=True) if uid == 9263 else None,
    )

    assert cog._is_bot_user(ctx.guild, 9263) is True
    assert cog._is_bot_user(ctx.guild, 9264) is False
    assert await cog.award_chess_tickets(ctx.guild, 9263, 1500) == 0
    assert (await _persistence.load_lottery(GUILD_ID))["players"] == {}


@pytest.mark.asyncio
async def test_draw_pool_excludes_bot_entrants(db):
    """Tickets a bot already holds stay in the pot but can never win: the
    draw's candidate pool drops bot accounts, and is empty when only bots
    entered."""
    guild = FakeGuild(gid=GUILD_ID)
    human = FakeMember(uid=9265)
    robot = FakeMember(uid=9266)
    robot.bot = True
    guild.members.extend([human, robot])
    cog = _make_cog()

    players = {"9265": 3, "9266": 40, "9267": 1}
    assert cog._eligible_players(guild, players) == {"9265": 3, "9267": 1}
    assert cog._eligible_players(guild, {"9266": 40}) == {}
