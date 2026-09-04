"""Dailies channel (src/cogs/dailies_cog.py).

Covers the react-to-claim flow end to end against the fake DB:
- refresh_dailies_channel posts / keeps / reposts the claim embed correctly
- the 🪙 reaction runs the daily reward + all scratchoffs, capped per day
- 🪙 / 🎰 / 🏇 then gamble the whole claim (flip, slots, a race against the bot)
- the 5am rollover tick reposts (clearing reactions)
- every non-claim message in the channel is scheduled for deletion
- the !settings dailies-channel subcommand wires the config
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.economy as _economy
from src.config import DAILY_REWARD
from src.cogs.dailies_cog import (
    DailiesCog, refresh_dailies_channel, DAILIES_TITLE,
    DAILIES_CLAIM_EMOJI, DAILIES_FLIP_EMOJI, DAILIES_SLOTS_EMOJI,
    DAILIES_RACE_EMOJI, DAILIES_TICKETS_EMOJI, DAILIES_ALL_EMOJIS,
    _delete_unless_kept,
)
from src.cogs.lottery_cog import LotteryCog, DAILY_TICKET_PRICE, TICKET_POOL_SHARE
from src.cogs.settings_cog import SettingsCog
import src.persistence as _persistence

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

TODAY = "2026-05-02"
YESTERDAY = "2026-05-01"


class _Resp:
    status = 404
    reason = "Not Found"


class FakeDailiesChannel:
    """Text-channel stand-in with purge / send / fetch_message bookkeeping."""

    def __init__(self, ch_id: int = 500):
        self.id = ch_id
        self.mention = f"<#{ch_id}>"
        self.messages: dict[int, SimpleNamespace] = {}
        self.sent: list = []
        self.purge_calls: list = []
        self._next_id = 9000

    async def purge(self, limit=None, check=None, bulk=True):
        self.purge_calls.append(check)
        for mid in [m for m in self.messages if check is None or check(self.messages[m])]:
            del self.messages[mid]

    async def send(self, content=None, *, embed=None, **kwargs):
        self._next_id += 1
        msg = SimpleNamespace(
            id=self._next_id,
            content=content,
            embed=embed,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
            edit=AsyncMock(),   # the race board is edited in place
        )
        self.messages[msg.id] = msg
        self.sent.append(msg)
        return msg

    async def fetch_message(self, message_id: int):
        if message_id in self.messages:
            return self.messages[message_id]
        raise discord.NotFound(_Resp(), "message not found")


class _StubBot:
    def __init__(self, channel=None, guild=None):
        # display_name: the bot's lane in a dailies 🏇 race (FakeGuild has no
        # `me`, so play_bot_race falls back to the client user).
        self.user = SimpleNamespace(id=999_999_999, bot=True, display_name="Bot")
        self.cogs: dict = {}
        self._channel = channel
        self._guild = guild

    def get_channel(self, ch_id):
        if self._channel is not None and self._channel.id == ch_id:
            return self._channel
        return None

    def get_cog(self, name):
        return self.cogs.get(name)

    def get_guild(self, gid):
        if self._guild is not None and self._guild.id == gid:
            return self._guild
        return None

    async def fetch_channel(self, ch_id):
        ch = self.get_channel(ch_id)
        if ch is None:
            raise discord.NotFound(_Resp(), "channel not found")
        return ch


def _make_cog(bot) -> DailiesCog:
    cog = DailiesCog(bot)
    # The minute loop's before_loop needs a real gateway bot; tests drive the
    # tick body directly via cog._reset_task.coro.
    cog._reset_task.cancel()
    return cog


def _pin_today(monkeypatch, today=TODAY):
    monkeypatch.setattr("src.cogs.dailies_cog._ct_today", lambda: today)
    monkeypatch.setattr("src.gambling.scratchoff._ct_today", lambda: today)
    monkeypatch.setattr("src.events._ct_today", lambda: today)


def _payload(uid=1, guild_id=42, message_id=None, channel_id=500,
             emoji=DAILIES_CLAIM_EMOJI, member=None):
    return SimpleNamespace(
        user_id=uid, guild_id=guild_id, message_id=message_id,
        channel_id=channel_id, emoji=emoji, member=member,
    )


# ── refresh_dailies_channel ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_posts_claim_embed_and_reaction(db, monkeypatch):
    _pin_today(monkeypatch)
    _state.guild_settings["42"] = {"dailies_channel": 500}
    channel = FakeDailiesChannel(500)
    bot = _StubBot(channel=channel)

    await refresh_dailies_channel(bot, 42)

    cfg = _state.guild_settings["42"]
    assert len(channel.sent) == 1
    claim = channel.sent[0]
    assert claim.embed.title == DAILIES_TITLE
    assert claim.add_reaction.await_count == 5
    for emoji in DAILIES_ALL_EMOJIS:
        claim.add_reaction.assert_any_await(emoji)
    # The 🎟️ ticket reaction must be added (and thus displayed) last.
    assert claim.add_reaction.await_args_list[-1].args == (DAILIES_TICKETS_EMOJI,)
    assert DAILIES_ALL_EMOJIS[-1] == DAILIES_TICKETS_EMOJI
    # 🏇 sits right before the ticket.
    assert DAILIES_ALL_EMOJIS[-2] == DAILIES_RACE_EMOJI
    assert "10,000 🪙 or more" in claim.embed.description  # big-result notice
    # The emoji legend sits at the bottom of the claim embed.
    assert f"{DAILIES_CLAIM_EMOJI} claim dailies" in claim.embed.description
    assert f"{DAILIES_FLIP_EMOJI} claim dailies, then coin-flip" in claim.embed.description
    assert f"{DAILIES_SLOTS_EMOJI} claim dailies, then bet" in claim.embed.description
    assert f"{DAILIES_RACE_EMOJI} claim dailies, then race the bot" in claim.embed.description
    assert f"{DAILIES_TICKETS_EMOJI} buy today's lottery ticket" in claim.embed.description
    assert cfg["dailies_message_id"] == claim.id
    assert cfg["dailies_reset_day"] == TODAY
    assert len(channel.purge_calls) == 1


@pytest.mark.asyncio
async def test_refresh_same_day_keeps_claim_message(db, monkeypatch):
    """A same-day refresh (boot sweep, reconnect) must NOT repost — reposting
    would wipe the reactions of players who already claimed today."""
    _pin_today(monkeypatch)
    channel = FakeDailiesChannel(500)
    bot = _StubBot(channel=channel)
    claim = await channel.send(embed=SimpleNamespace(title=DAILIES_TITLE))
    channel.sent.clear()
    _state.guild_settings["42"] = {
        "dailies_channel": 500, "dailies_message_id": claim.id,
        "dailies_reset_day": TODAY,
    }

    await refresh_dailies_channel(bot, 42)

    assert channel.sent == []  # no repost
    assert _state.guild_settings["42"]["dailies_message_id"] == claim.id
    # The claim message survived the purge; anything else would have gone.
    assert claim.id in channel.messages


@pytest.mark.asyncio
async def test_refresh_new_day_reposts_and_purges_old(db, monkeypatch):
    """Once the 5am CT gameplay-day rolls over, the claim embed is reposted so
    all claim reactions reset — and yesterday's kept big wins go with it."""
    _pin_today(monkeypatch)
    channel = FakeDailiesChannel(500)
    bot = _StubBot(channel=channel)
    old_claim = await channel.send(embed=SimpleNamespace(title=DAILIES_TITLE))
    stray = await channel.send(content="chatter from overnight")
    big_win = await channel.send(embed=SimpleNamespace(title="🎫 Scratchoff"))
    channel.sent.clear()
    _state.guild_settings["42"] = {
        "dailies_channel": 500, "dailies_message_id": old_claim.id,
        "dailies_reset_day": YESTERDAY, "dailies_keep_ids": [big_win.id],
    }

    await refresh_dailies_channel(bot, 42)

    cfg = _state.guild_settings["42"]
    assert len(channel.sent) == 1
    new_claim = channel.sent[0]
    assert cfg["dailies_message_id"] == new_claim.id != old_claim.id
    assert cfg["dailies_reset_day"] == TODAY
    # Old claim, stray chatter, AND yesterday's big win purged; only the
    # fresh embed remains and the keep list is reset.
    assert set(channel.messages) == {new_claim.id}
    assert stray.id not in channel.messages
    assert "dailies_keep_ids" not in cfg


@pytest.mark.asyncio
async def test_refresh_same_day_keeps_big_win_messages(db, monkeypatch):
    """A same-day sweep (boot, reconnect) must preserve kept big-win results
    alongside the claim embed."""
    _pin_today(monkeypatch)
    channel = FakeDailiesChannel(500)
    bot = _StubBot(channel=channel)
    claim = await channel.send(embed=SimpleNamespace(title=DAILIES_TITLE))
    big_win = await channel.send(embed=SimpleNamespace(title="🎫 Scratchoff"))
    chatter = await channel.send(content="gg")
    channel.sent.clear()
    _state.guild_settings["42"] = {
        "dailies_channel": 500, "dailies_message_id": claim.id,
        "dailies_reset_day": TODAY, "dailies_keep_ids": [big_win.id],
    }

    await refresh_dailies_channel(bot, 42)

    assert channel.sent == []  # no repost
    assert set(channel.messages) == {claim.id, big_win.id}
    assert chatter.id not in channel.messages
    assert _state.guild_settings["42"]["dailies_keep_ids"] == [big_win.id]


# ── reaction claim ────────────────────────────────────────────────────────────

async def _claim_setup(monkeypatch, uid=1):
    _pin_today(monkeypatch)
    guild = FakeGuild(gid=42)
    channel = FakeDailiesChannel(500)
    bot = _StubBot(channel=channel, guild=guild)
    member = FakeMember(uid=uid, display_name="player")
    guild.members.append(member)
    _state.guild_settings["42"] = {
        "dailies_channel": 500, "dailies_message_id": 777,
        "dailies_reset_day": TODAY,
    }
    await _economy._ensure_user(uid)
    user = _state.economy["users"][str(uid)]
    user["scratch_date"] = TODAY
    user["scratch_used"] = 0
    user["daily_date"] = None
    user["balance"] = 0
    return bot, guild, channel, member


@pytest.mark.asyncio
async def test_reaction_claim_runs_daily_and_all_scratchoffs(db, monkeypatch):
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))

    user = _state.economy["users"]["1"]
    assert user["scratch_used"] == 3          # all dailies used at once
    assert user["daily_date"] == TODAY        # daily reward claimed
    assert user["balance"] >= DAILY_REWARD    # daily + any scratch payouts
    # 1 daily-reward embed + 3 card embeds, all in the dailies channel.
    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert titles.count("🪙 Daily Reward") == 1
    assert titles.count("🎫 Scratchoff") == 3


def _pin_no_natural_matches(monkeypatch):
    """Make unrigged cards deterministic zero-match: the first random.choices
    call in play_scratchoffs draws the daily goal (all symbol #0), every later
    call draws a card (all symbol #1). Rigged cards bypass random.choices, so
    a state.rigged_scratch entry still controls a card's matches. The rig-fire
    roll (random.random) is pinned high so the rig defers to the user's last
    card of the day — the third in these 3-card claims."""
    calls = {"n": 0}

    def _fake_choices(pop, k):
        calls["n"] += 1
        return [pop[0] if calls["n"] == 1 else pop[1]] * k

    monkeypatch.setattr("src.gambling.scratchoff.random.choices", _fake_choices)
    monkeypatch.setattr("src.gambling.scratchoff.random.random", lambda: 0.99)


@pytest.mark.asyncio
async def test_reaction_claim_big_win_registered_for_keeping(db, monkeypatch):
    """A 10k+ scratchoff result in the dailies channel is registered in
    dailies_keep_ids and exempted from the 5-minute sweep — it stays until the
    reset repost purges the channel."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)
    # Rig the third card to 3 matches → 10,000 🪙, exactly at the keep threshold.
    _state.rigged_scratch[1] = 3
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))

    cfg = _state.guild_settings["42"]
    scratch_msgs = [m for m in channel.sent if m.embed is not None and m.embed.title == "🎫 Scratchoff"]
    big_win = scratch_msgs[2]  # the rigged third card
    assert cfg["dailies_keep_ids"] == [big_win.id]

    # The sweeper skips the kept win but still deletes ordinary results.
    keep_probe = _sweeper_msg(big_win.id)
    normal_probe = _sweeper_msg(scratch_msgs[0].id)
    await _delete_unless_kept(keep_probe, 0)
    await _delete_unless_kept(normal_probe, 0)
    keep_probe.delete.assert_not_awaited()
    normal_probe.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_reaction_claim_ordinary_wins_not_kept(db, monkeypatch):
    """Sub-10k results (0–2 matches) must not accumulate in dailies_keep_ids."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)
    # Rig the third card to 2 matches → 1,000 🪙, below the keep threshold.
    _state.rigged_scratch[1] = 2
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))

    assert "dailies_keep_ids" not in _state.guild_settings["42"]


@pytest.mark.asyncio
async def test_reaction_flip_gambles_whole_claim_and_keeps_big_results(db, monkeypatch):
    """🪙 claims dailies, then coin-flips the whole claim — daily reward plus
    the total scratchoff winnings. A ±10k flip result joins the keep list
    alongside the 10k scratch card."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)
    _state.rigged_scratch[1] = 3   # third card → 10,000 🪙 total winnings
    _state.rigged_flips[1] = 1     # the follow-up flip wins
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_FLIP_EMOJI))

    user = _state.economy["users"]["1"]
    assert user["scratch_used"] == 3
    # daily + 10k scratch − (daily + 10k) flip stake + 2x flip payout
    assert user["balance"] == (DAILY_REWARD + 10_000) * 2
    flip_msgs = [m for m in channel.sent if m.embed is not None and m.embed.title == "🪙 Heads!"]
    assert len(flip_msgs) == 1
    scratch_msgs = [m for m in channel.sent if m.embed is not None and m.embed.title == "🎫 Scratchoff"]
    assert _state.guild_settings["42"]["dailies_keep_ids"] == [
        scratch_msgs[2].id, flip_msgs[0].id,
    ]


@pytest.mark.asyncio
async def test_reaction_slots_gambles_whole_claim_and_keeps_big_results(db, monkeypatch):
    """🎰 claims dailies, then bets the whole claim — daily reward plus the
    total scratchoff winnings — on slots. The slots win joins the keep list
    alongside the 10k scratch card."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)
    _state.rigged_scratch[1] = 3   # third card → 10,000 🪙 total winnings
    _state.rigged_slots[1] = "🍒"  # three cherries → 3x payout
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_SLOTS_EMOJI))

    user = _state.economy["users"]["1"]
    assert user["scratch_used"] == 3
    # daily + 10k scratch − (daily + 10k) slots stake + 3x slots payout
    assert user["balance"] == (DAILY_REWARD + 10_000) * 3
    slots_msgs = [m for m in channel.sent if m.embed is not None and m.embed.title == "🎰 Winner!"]
    assert len(slots_msgs) == 1
    scratch_msgs = [m for m in channel.sent if m.embed is not None and m.embed.title == "🎫 Scratchoff"]
    assert _state.guild_settings["42"]["dailies_keep_ids"] == [
        scratch_msgs[2].id, slots_msgs[0].id,
    ]


async def _give_property_with_pending_revenue(uid: int, pid: str = "car_wash"):
    """Install property ownership with a full day of unbanked revenue."""
    now = time.time()
    row = {
        "owner_id": uid, "acquired_at": now - 10 * 86400.0,
        "list_price": None, "listed_at": None,
        "upgraded": False, "custom_name": None,
    }
    _state.property_owners[pid] = row
    await _persistence.save_property_owner(pid, row)
    _state.economy["users"][str(uid)]["property_paid_at"] = now - 86400.0


def _script_race(monkeypatch, human: int = 3, bot: int = 1):
    """Make the dailies race instant and scripted (default: the player wins)."""
    monkeypatch.setattr("src.games.race.RACE_TICK_SECONDS", 0)
    monkeypatch.setattr(
        "src.games.race._tick_rolls",
        lambda players: {players[0]: human, players[1]: bot},
    )


@pytest.mark.asyncio
async def test_reaction_race_gambles_whole_claim_and_keeps_big_results(db, monkeypatch):
    """🏇 claims dailies, then races the bot for the whole claim — daily
    reward plus the total scratchoff winnings. The bot is the house, so a
    win doubles the stake; a 10k+ result joins the keep list alongside the
    10k scratch card. The daily stake is a one-shot: no Race Again buttons."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)
    _state.rigged_scratch[1] = 3   # third card → 10,000 🪙 total winnings
    _script_race(monkeypatch, human=3, bot=1)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_RACE_EMOJI))

    user = _state.economy["users"]["1"]
    assert user["scratch_used"] == 3
    stake = DAILY_REWARD + 10_000
    # daily + 10k scratch − stake + 2x race payout
    assert user["balance"] == stake * 2
    race_msgs = [m for m in channel.sent if m.embed is not None and m.embed.title == "🏇 Race Starting!"]
    assert len(race_msgs) == 1
    final = race_msgs[0].edit.await_args.kwargs
    assert final["embed"].title == "🏁 Race Finished!"
    assert f"🏆 **player** wins **{stake * 2:,} 🪙**!" in final["embed"].description
    assert final["embed"].description.splitlines()[-1].startswith("🏆 **player**")
    assert "view" not in final
    scratch_msgs = [m for m in channel.sent if m.embed is not None and m.embed.title == "🎫 Scratchoff"]
    assert _state.guild_settings["42"]["dailies_keep_ids"] == [
        scratch_msgs[2].id, race_msgs[0].id,
    ]
    assert _state.active_race_games == {}


@pytest.mark.asyncio
async def test_reaction_race_loss_forfeits_the_claim(db, monkeypatch):
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)   # all three cards miss
    _script_race(monkeypatch, human=1, bot=3)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_RACE_EMOJI))

    assert _state.economy["users"]["1"]["balance"] == 0
    race_msgs = [m for m in channel.sent if m.embed is not None and m.embed.title == "🏇 Race Starting!"]
    desc = race_msgs[0].edit.await_args.kwargs["embed"].description
    assert f"🤖 **Bot** wins — **player** lost **{DAILY_REWARD:,} 🪙**." in desc


def _spy_race(monkeypatch):
    """Wrap the dailies cog's play_bot_race binding, recording stake/exclude."""
    import src.cogs.dailies_cog as _dailies_mod
    real_race = _dailies_mod.play_bot_race
    seen = {}

    async def _wrapped(member, channel, guild, stake, bot_lane, record_exclude=0):
        seen["stake"], seen["exclude"] = stake, record_exclude
        return await real_race(member, channel, guild, stake, bot_lane, record_exclude=record_exclude)

    monkeypatch.setattr("src.cogs.dailies_cog.play_bot_race", _wrapped)
    return seen


def _spy_flip(monkeypatch):
    """Wrap the dailies cog's play_flip binding, recording stake/exclude."""
    import src.cogs.dailies_cog as _dailies_mod
    real_flip = _dailies_mod.play_flip
    seen = {}

    async def _wrapped(member, channel, guild, stake, record_exclude=0):
        seen["stake"], seen["exclude"] = stake, record_exclude
        return await real_flip(member, channel, guild, stake, record_exclude=record_exclude)

    monkeypatch.setattr("src.cogs.dailies_cog.play_flip", _wrapped)
    return seen


@pytest.mark.asyncio
async def test_reaction_flip_leaves_property_revenue_out_of_stake_by_default(db, monkeypatch):
    """Property revenue banks with the claim but is NOT gambled unless the
    owner opted in with !daily property — the flip stakes only the daily
    reward (+ scratchoff winnings, none here)."""
    from src.properties import daily_revenue
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)   # all three cards miss
    _state.rigged_flips[1] = 1             # the follow-up flip wins
    await _give_property_with_pending_revenue(1)
    rev = daily_revenue(20_000)
    seen = _spy_flip(monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_FLIP_EMOJI))

    assert seen == {"stake": DAILY_REWARD, "exclude": 0}
    # Revenue banked untouched; only the daily reward was flipped (won 2x).
    assert _state.economy["users"]["1"]["balance"] == rev + DAILY_REWARD * 2


@pytest.mark.asyncio
async def test_reaction_flip_includes_property_revenue_when_opted_in(db, monkeypatch):
    """With daily_gamble_property on, the whole claim is staked and the
    property portion rides as record_exclude."""
    from src.properties import daily_revenue
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)
    _state.rigged_flips[1] = 1
    await _give_property_with_pending_revenue(1)
    _state.economy["users"]["1"]["daily_gamble_property"] = True
    rev = daily_revenue(20_000)
    seen = _spy_flip(monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_FLIP_EMOJI))

    assert seen == {"stake": DAILY_REWARD + rev, "exclude": rev}
    assert _state.economy["users"]["1"]["balance"] == (DAILY_REWARD + rev) * 2


@pytest.mark.asyncio
async def test_reaction_race_property_revenue_follows_the_same_opt_in(db, monkeypatch):
    """🏇 stakes exactly what 🪙 would: property revenue stays out unless the
    owner opted in, and then rides as record_exclude so auto-staked income
    can't set the race record."""
    from src.properties import daily_revenue
    from src.persistence import load_records
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)
    _script_race(monkeypatch, human=3, bot=1)
    await _give_property_with_pending_revenue(1)
    _state.economy["users"]["1"]["daily_gamble_property"] = True
    rev = daily_revenue(20_000)
    seen = _spy_race(monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_RACE_EMOJI))

    assert seen == {"stake": DAILY_REWARD + rev, "exclude": rev}
    assert _state.economy["users"]["1"]["balance"] == (DAILY_REWARD + rev) * 2
    assert (await load_records(42))["race"]["value"] == DAILY_REWARD * 2


def _add_lottery_cog(bot, monkeypatch, today=TODAY):
    """Register a LotteryCog on the stub bot for the 🎟️ reaction path."""
    monkeypatch.setattr("src.cogs.lottery_cog._ct_today", lambda: today)
    # Pin past the one-time TICKET_SALES_START_CT launch gate and clear of
    # the 1st-of-month lock/draw windows.
    import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    now_ct = _dt.datetime(2026, 10, 2, 12, 0, tzinfo=_ZI("America/Chicago"))
    monkeypatch.setattr("src.cogs.lottery_cog._ct_now", lambda: now_ct)
    cog = LotteryCog(bot)
    cog.lottery_scheduler.cancel()
    bot.cogs["LotteryCog"] = cog
    return cog


@pytest.mark.asyncio
async def test_reaction_tickets_buys_daily_ticket_without_claiming(db, monkeypatch):
    """🎟️ buys the once-a-day 1,000 🪙 ticket and nothing else — the daily
    reward stays unclaimed and no scratchoffs are burned."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _state.guild_settings["42"]["lottery_channel"] = 600
    _state.economy["users"]["1"]["balance"] = 2_000
    _add_lottery_cog(bot, monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_TICKETS_EMOJI))

    user = _state.economy["users"]["1"]
    assert user["daily_date"] is None                # daily reward untouched
    assert user["scratch_used"] == 0                 # no scratchoffs burned
    assert _state.lottery_ticket_grants[(42, 1)]["daily_day"] == TODAY
    # Only the ticket purchase moved money.
    assert user["balance"] == 2_000 - DAILY_TICKET_PRICE
    lot = await _persistence.load_lottery(42)
    assert lot["players"]["1"] == 1
    # Pool share of the ticket price, +1,000 new-player bonus.
    assert lot["prize_pool"] == TICKET_POOL_SHARE + 1_000
    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert "🎰 Daily Ticket Purchased" in titles
    assert "🪙 Daily Reward" not in titles
    assert "🎫 Scratchoff" not in titles


@pytest.mark.asyncio
async def test_reaction_tickets_second_click_reports_already_bought(db, monkeypatch):
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _state.guild_settings["42"]["lottery_channel"] = 600
    _state.economy["users"]["1"]["balance"] = 5_000
    _add_lottery_cog(bot, monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_TICKETS_EMOJI))
    channel.sent.clear()
    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_TICKETS_EMOJI))

    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert "🎟️ Daily Ticket Already Bought" in titles
    lot = await _persistence.load_lottery(42)
    assert lot["players"]["1"] == 1  # unchanged
    assert _state.economy["users"]["1"]["balance"] == 5_000 - DAILY_TICKET_PRICE


@pytest.mark.asyncio
async def test_reaction_tickets_without_lottery_channel_reports_disabled(db, monkeypatch):
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _state.economy["users"]["1"]["balance"] = 2_000
    _add_lottery_cog(bot, monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_TICKETS_EMOJI))

    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert "🎰 Lottery Disabled" in titles
    # The daily gate must not be burned by a refused purchase.
    assert _state.lottery_ticket_grants.get((42, 1), {}).get("daily_day") is None
    assert _state.economy["users"]["1"]["balance"] == 2_000


@pytest.mark.asyncio
async def test_reaction_flip_stakes_daily_reward_when_no_scratch_winnings(db, monkeypatch):
    """🪙 with zero scratchoff winnings still flips the daily reward — the
    claim always pays out at least DAILY_REWARD, so there is always a stake."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)   # all three cards miss
    _state.rigged_flips[1] = 1             # the follow-up flip wins
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_FLIP_EMOJI))

    user = _state.economy["users"]["1"]
    assert user["scratch_used"] == 3
    # daily − daily flip stake + 2x flip payout
    assert user["balance"] == DAILY_REWARD * 2
    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert "🪙 Heads!" in titles
    # Well under the 10k keep threshold — nothing pinned.
    assert "dailies_keep_ids" not in _state.guild_settings["42"]


@pytest.mark.asyncio
async def test_reaction_slots_stakes_daily_reward_when_no_scratch_winnings(db, monkeypatch):
    """🎰 with zero scratchoff winnings bets the daily reward on slots
    (DAILY_REWARD clears SLOT_MIN_BET)."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)   # all three cards miss
    _state.rigged_slots[1] = "🍒"          # three cherries → 3x payout
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_SLOTS_EMOJI))

    user = _state.economy["users"]["1"]
    assert user["scratch_used"] == 3
    # daily − daily slots stake + 3x slots payout
    assert user["balance"] == DAILY_REWARD * 3
    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert "🎰 Winner!" in titles


@pytest.mark.asyncio
async def test_reaction_gamble_skipped_on_second_click(db, monkeypatch):
    """A second 🪙 click claims nothing — daily already taken, scratchoffs
    spent — so there is no stake and no flip."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _pin_no_natural_matches(monkeypatch)   # all three cards miss
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_FLIP_EMOJI))
    channel.sent.clear()
    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_FLIP_EMOJI))

    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert not any(t.startswith("🪙 Heads") or t.startswith("🪙 Tails") for t in titles)


@pytest.mark.asyncio
async def test_reaction_claim_second_click_hits_daily_limit(db, monkeypatch):
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))
    channel.sent.clear()
    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))

    user = _state.economy["users"]["1"]
    assert user["scratch_used"] == 3  # unchanged — cap enforced
    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert "🎰 Daily Limit" in titles
    assert "🎫 Scratchoff" not in titles


@pytest.mark.asyncio
async def test_reaction_ignored_for_wrong_message_emoji_and_bot(db, monkeypatch):
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    cog = _make_cog(bot)

    # Wrong message id.
    await cog.on_raw_reaction_add(_payload(message_id=778, member=member))
    # Wrong emoji.
    await cog.on_raw_reaction_add(_payload(message_id=777, emoji="👍", member=member))
    # The bot's own seed reaction.
    await cog.on_raw_reaction_add(_payload(uid=bot.user.id, message_id=777))

    assert _state.economy["users"]["1"]["scratch_used"] == 0
    assert channel.sent == []


@pytest.mark.asyncio
async def test_reaction_ignored_for_blocklisted_user(db, monkeypatch):
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _state.blocklist[(42, 1)] = {"reason": None, "banned_by": 2, "banned_at": None}
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))

    assert _state.economy["users"]["1"]["scratch_used"] == 0
    assert channel.sent == []


# ── 5am rollover tick ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reset_tick_reposts_when_day_rolls_over(db, monkeypatch):
    _pin_today(monkeypatch)
    channel = FakeDailiesChannel(500)
    bot = _StubBot(channel=channel)
    old_claim = await channel.send(embed=SimpleNamespace(title=DAILIES_TITLE))
    channel.sent.clear()
    _state.guild_settings["42"] = {
        "dailies_channel": 500, "dailies_message_id": old_claim.id,
        "dailies_reset_day": YESTERDAY,
    }
    cog = _make_cog(bot)

    await cog._reset_task.coro(cog)

    cfg = _state.guild_settings["42"]
    assert len(channel.sent) == 1
    assert cfg["dailies_message_id"] == channel.sent[0].id != old_claim.id
    assert cfg["dailies_reset_day"] == TODAY

    # Same-day tick is a no-op (no repost churn every minute).
    channel.sent.clear()
    await cog._reset_task.coro(cog)
    assert channel.sent == []


# ── 5-minute message sweeper ──────────────────────────────────────────────────

def _sweeper_msg(mid, ch_id=500, guild_id=42):
    return SimpleNamespace(
        id=mid,
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
        channel=SimpleNamespace(id=ch_id),
        delete=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_on_message_deletes_non_claim_messages_only(monkeypatch):
    _pin_today(monkeypatch)
    _state.guild_settings["42"] = {
        "dailies_channel": 500, "dailies_message_id": 777,
        "dailies_reset_day": TODAY,
    }
    monkeypatch.setattr("src.cogs.dailies_cog.DAILIES_MESSAGE_TTL", 0.0)
    cog = _make_cog(_StubBot())

    chatter = _sweeper_msg(1001)
    claim = _sweeper_msg(777)
    other_channel = _sweeper_msg(1002, ch_id=501)
    dm = _sweeper_msg(1003, guild_id=None)
    for m in (chatter, claim, other_channel, dm):
        await cog.on_message(m)
    await asyncio.sleep(0.01)  # let the created deletion tasks run

    chatter.delete.assert_awaited_once()
    claim.delete.assert_not_awaited()
    other_channel.delete.assert_not_awaited()
    dm.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_embed_survives_race_with_id_recording(monkeypatch):
    """Race regression: the gateway delivers MESSAGE_CREATE for a freshly
    reposted claim embed while refresh_dailies_channel is still awaiting
    send/add_reaction — before it records the new id. The exemption must be
    evaluated at deletion time (config settled by then), not at schedule
    time, or every reposted claim embed self-deletes after the TTL.
    """
    _pin_today(monkeypatch)
    cfg = {
        "dailies_channel": 500, "dailies_message_id": 777,  # OLD claim id
        "dailies_reset_day": TODAY,
    }
    _state.guild_settings["42"] = cfg
    monkeypatch.setattr("src.cogs.dailies_cog.DAILIES_MESSAGE_TTL", 0.0)
    cog = _make_cog(_StubBot())

    fresh_claim = _sweeper_msg(9001)
    # MESSAGE_CREATE dispatches while cfg still points at the old embed …
    await cog.on_message(fresh_claim)
    # … and the refresh records the new id a beat later.
    cfg["dailies_message_id"] = fresh_claim.id
    await asyncio.sleep(0.01)

    fresh_claim.delete.assert_not_awaited()


# ── !settings dailies-channel ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_dailies_channel_set_and_clear(db, monkeypatch):
    _pin_today(monkeypatch)
    channel = FakeDailiesChannel(500)
    bot = _StubBot(channel=channel)
    cog = SettingsCog(bot=bot)
    author = FakeMember(uid=1, administrator=True)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    ctx.bot = bot
    ctx.command.qualified_name = "settings dailies-channel"
    ctx.message.channel_mentions = [channel]

    await cog.settings_dailies_channel.callback(cog, ctx)

    cfg = _state.guild_settings["42"]
    assert cfg["dailies_channel"] == 500
    # The claim embed was posted immediately as part of setup.
    assert cfg["dailies_message_id"] == channel.sent[0].id
    assert channel.sent[0].embed.title == DAILIES_TITLE
    assert ctx.sent_embeds[-1].title == "🪙 Dailies Channel"

    claim = channel.sent[0]
    ctx.message.channel_mentions = []
    await cog.settings_dailies_channel.callback(cog, ctx, "clear")

    assert cfg["dailies_channel"] is None
    assert "dailies_message_id" not in cfg
    assert "dailies_reset_day" not in cfg
    claim.delete.assert_awaited_once()  # claim embed cleaned up best-effort


@pytest.mark.asyncio
async def test_settings_dailies_channel_usage_message(db):
    cog = SettingsCog(bot=_StubBot())
    author = FakeMember(uid=1, administrator=True)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    ctx.command.qualified_name = "settings dailies-channel"
    ctx.message.channel_mentions = []

    await cog.settings_dailies_channel.callback(cog, ctx)

    assert "dailies_channel" not in _state.guild_settings.get("42", {})
    assert "Usage" in ctx.sent_embeds[-1].description


# ── daily streak ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reaction_claim_counts_toward_daily_streak(db, monkeypatch):
    """A dailies click is a day of activity for the streak, same as a command:
    the first click of the day extends it, later clicks are no-ops."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _state.command_streak["1"] = {"date": "2026-05-01", "count": 4}
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))
    assert _state.command_streak["1"] == {"date": TODAY, "count": 5}

    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))
    assert _state.command_streak["1"] == {"date": TODAY, "count": 5}


@pytest.mark.asyncio
async def test_reaction_ticket_click_counts_toward_daily_streak(db, monkeypatch):
    """🎟️ skips the claim but is still a dailies click, so it keeps the streak."""
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _state.guild_settings["42"]["lottery_channel"] = 600
    _state.economy["users"]["1"]["balance"] = 2_000
    _add_lottery_cog(bot, monkeypatch)
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(
        _payload(message_id=777, member=member, emoji=DAILIES_TICKETS_EMOJI))

    assert _state.command_streak["1"] == {"date": TODAY, "count": 1}
    assert _state.economy["users"]["1"]["daily_date"] is None   # still no claim


@pytest.mark.asyncio
async def test_reaction_first_click_of_the_day_sets_the_streak_record(db, monkeypatch):
    bot, guild, channel, member = await _claim_setup(monkeypatch)
    _state.command_streak["1"] = {"date": "2026-05-01", "count": 7}
    cog = _make_cog(bot)

    await cog.on_raw_reaction_add(_payload(message_id=777, member=member))

    rec = (await _persistence.load_records(42))["command_streak"]
    assert rec["value"] == 8
    assert rec["holder_id"] == 1
    titles = [m.embed.title for m in channel.sent if m.embed is not None]
    assert "🏆 New Record!" in titles
