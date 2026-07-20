"""Dailies channel (src/cogs/dailies_cog.py).

Covers the react-to-claim flow end to end against the fake DB:
- refresh_dailies_channel posts / keeps / reposts the claim embed correctly
- the 🪙 reaction runs the daily reward + all scratchoffs, capped per day
- the 5am rollover tick reposts (clearing reactions)
- every non-claim message in the channel is scheduled for deletion
- the !settings dailies-channel subcommand wires the config
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.economy as _economy
from src.config import DAILY_REWARD
from src.cogs.dailies_cog import (
    DailiesCog, refresh_dailies_channel, DAILIES_EMOJI, DAILIES_TITLE,
    DAILIES_MESSAGE_TTL,
)
from src.cogs.settings_cog import SettingsCog

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
        self.user = SimpleNamespace(id=999_999_999, bot=True)
        self.cogs: dict = {}
        self._channel = channel
        self._guild = guild

    def get_channel(self, ch_id):
        if self._channel is not None and self._channel.id == ch_id:
            return self._channel
        return None

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
             emoji=DAILIES_EMOJI, member=None):
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
    claim.add_reaction.assert_awaited_once_with(DAILIES_EMOJI)
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
    all claim reactions reset."""
    _pin_today(monkeypatch)
    channel = FakeDailiesChannel(500)
    bot = _StubBot(channel=channel)
    old_claim = await channel.send(embed=SimpleNamespace(title=DAILIES_TITLE))
    stray = await channel.send(content="chatter from overnight")
    channel.sent.clear()
    _state.guild_settings["42"] = {
        "dailies_channel": 500, "dailies_message_id": old_claim.id,
        "dailies_reset_day": YESTERDAY,
    }

    await refresh_dailies_channel(bot, 42)

    cfg = _state.guild_settings["42"]
    assert len(channel.sent) == 1
    new_claim = channel.sent[0]
    assert cfg["dailies_message_id"] == new_claim.id != old_claim.id
    assert cfg["dailies_reset_day"] == TODAY
    # Old claim + stray chatter purged; only the fresh embed remains.
    assert set(channel.messages) == {new_claim.id}
    assert stray.id not in channel.messages


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

@pytest.mark.asyncio
async def test_on_message_schedules_deletion_for_non_claim_messages(monkeypatch):
    _pin_today(monkeypatch)
    _state.guild_settings["42"] = {
        "dailies_channel": 500, "dailies_message_id": 777,
        "dailies_reset_day": TODAY,
    }
    deleted: list = []

    async def _record_delete(message, delay=5.0):
        deleted.append((message.id, delay))

    monkeypatch.setattr("src.cogs.dailies_cog._delete_after", _record_delete)
    cog = _make_cog(_StubBot())

    def _msg(mid, ch_id=500, guild_id=42):
        return SimpleNamespace(
            id=mid,
            guild=SimpleNamespace(id=guild_id) if guild_id else None,
            channel=SimpleNamespace(id=ch_id),
        )

    await cog.on_message(_msg(1001))                 # chatter → deleted
    await cog.on_message(_msg(777))                  # the claim embed → kept
    await cog.on_message(_msg(1002, ch_id=501))      # other channel → kept
    await cog.on_message(_msg(1003, guild_id=None))  # DM → kept
    await asyncio.sleep(0)  # let the created deletion tasks run

    assert deleted == [(1001, DAILIES_MESSAGE_TTL)]


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
