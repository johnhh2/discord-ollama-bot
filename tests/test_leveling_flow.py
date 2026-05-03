"""Tier F: leveling — voice-tick gating, scratch XP, _announce_levelup.

`!prestige` was listed in the gap analysis but doesn't exist in production
(no cmd_prestige) — skipped. The remaining bits:

- _do_voice_tick: skips bots, 1-person channels, private channels;
  awards voice XP only when not muted/deafened; awards stream XP only
  when self_stream is True.
- grant_xp("scratch"): no rate limit (capped upstream by 3/day scratchoff
  limit); other sources are already covered in test_schedulers.py.
- _announce_levelup: awards levelup_coin_reward to the leveled-up user,
  posts to levelup_channel only when configured.
"""
import pytest
from unittest.mock import AsyncMock

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
from src.cogs.leveling_cog import (
    LevelingCog, grant_xp, _ensure_user as _ensure_lvl_user,
    levelup_coin_reward, display_level, XP_SCRATCH,
)

from tests.fakes.discord import FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio


# ── grant_xp("scratch") ───────────────────────────────────────────────────────

async def test_grant_xp_scratch_grants_unconditionally(db, monkeypatch):
    """`scratch` source has no rate-limit gate (cap is upstream, 3/day)."""
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    xp1, _ = await grant_xp(uid=1, source="scratch", guild_id=42)
    xp2, _ = await grant_xp(uid=1, source="scratch", guild_id=42)
    xp3, _ = await grant_xp(uid=1, source="scratch", guild_id=42)

    # Three calls in immediate succession — each grants XP_SCRATCH.
    assert xp1 == XP_SCRATCH
    assert xp2 == XP_SCRATCH
    assert xp3 == XP_SCRATCH
    assert _state.leveling["42"]["1"]["xp"] == 3 * XP_SCRATCH


# ── _do_voice_tick ────────────────────────────────────────────────────────────

class _VoiceState:
    def __init__(self, self_mute=False, self_deaf=False, mute=False, deaf=False, self_stream=False):
        self.self_mute = self_mute
        self.self_deaf = self_deaf
        self.mute = mute
        self.deaf = deaf
        self.self_stream = self_stream


class _VoiceMember(FakeMember):
    def __init__(self, uid, voice_state, *, bot=False):
        super().__init__(uid)
        self.voice = voice_state
        self.bot = bot


class _Overwrite:
    def __init__(self, view_channel=None):
        self.view_channel = view_channel


class _VoiceChannel:
    def __init__(self, members, view_channel=None):
        self.members = members
        self._overwrite = _Overwrite(view_channel=view_channel)

    def overwrites_for(self, _role):
        return self._overwrite


class _GuildWithVC(FakeGuild):
    def __init__(self, gid, voice_channels):
        super().__init__(gid=gid)
        self.voice_channels = voice_channels
        # default_role is referenced by _do_voice_tick.
        self.default_role = object()


class _BotWithGuilds:
    def __init__(self, guilds):
        self.guilds = guilds


async def test_voice_tick_grants_to_unmuted_in_busy_channel(db, monkeypatch):
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    a = _VoiceMember(uid=1, voice_state=_VoiceState())
    b = _VoiceMember(uid=2, voice_state=_VoiceState())
    vc = _VoiceChannel(members=[a, b])
    guild = _GuildWithVC(gid=42, voice_channels=[vc])
    bot = _BotWithGuilds([guild])

    cog = LevelingCog.__new__(LevelingCog)  # bypass __init__ which starts the task loop
    cog.bot = bot

    await cog._do_voice_tick()

    # Both got voice XP.
    assert _state.leveling["42"]["1"]["xp"] > 0
    assert _state.leveling["42"]["2"]["xp"] > 0


async def test_voice_tick_skips_solo_channel(db, monkeypatch):
    """Single human in a channel: no XP."""
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    solo = _VoiceMember(uid=1, voice_state=_VoiceState())
    vc = _VoiceChannel(members=[solo])
    guild = _GuildWithVC(gid=42, voice_channels=[vc])
    bot = _BotWithGuilds([guild])

    cog = LevelingCog.__new__(LevelingCog)
    cog.bot = bot
    await cog._do_voice_tick()

    assert "1" not in _state.leveling.get("42", {})


async def test_voice_tick_does_not_count_bots_toward_human_count(db, monkeypatch):
    """One human + one bot = 1 human → counted as solo, no XP."""
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    human = _VoiceMember(uid=1, voice_state=_VoiceState())
    botmember = _VoiceMember(uid=2, voice_state=_VoiceState(), bot=True)
    vc = _VoiceChannel(members=[human, botmember])
    guild = _GuildWithVC(gid=42, voice_channels=[vc])
    bot = _BotWithGuilds([guild])

    cog = LevelingCog.__new__(LevelingCog)
    cog.bot = bot
    await cog._do_voice_tick()

    assert "1" not in _state.leveling.get("42", {})
    assert "2" not in _state.leveling.get("42", {})


async def test_voice_tick_skips_private_channel(db, monkeypatch):
    """A channel with view_channel overrides set to False is private — skip."""
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    a = _VoiceMember(uid=1, voice_state=_VoiceState())
    b = _VoiceMember(uid=2, voice_state=_VoiceState())
    vc = _VoiceChannel(members=[a, b], view_channel=False)
    guild = _GuildWithVC(gid=42, voice_channels=[vc])
    bot = _BotWithGuilds([guild])

    cog = LevelingCog.__new__(LevelingCog)
    cog.bot = bot
    await cog._do_voice_tick()

    assert _state.leveling.get("42", {}) == {}


async def test_voice_tick_skips_muted_member(db, monkeypatch):
    """self_mute=True: no voice XP; not streaming either: no stream XP."""
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    muted = _VoiceMember(uid=1, voice_state=_VoiceState(self_mute=True))
    unmuted = _VoiceMember(uid=2, voice_state=_VoiceState())
    vc = _VoiceChannel(members=[muted, unmuted])
    guild = _GuildWithVC(gid=42, voice_channels=[vc])
    bot = _BotWithGuilds([guild])

    cog = LevelingCog.__new__(LevelingCog)
    cog.bot = bot
    await cog._do_voice_tick()

    # Muted user has no leveling row; unmuted does.
    assert "1" not in _state.leveling.get("42", {})
    assert _state.leveling["42"]["2"]["xp"] > 0


async def test_voice_tick_grants_stream_xp_when_streaming(db, monkeypatch):
    """self_stream=True grants stream XP on top of voice XP (separate cooldown)."""
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    streamer = _VoiceMember(uid=1, voice_state=_VoiceState(self_stream=True))
    other = _VoiceMember(uid=2, voice_state=_VoiceState())
    vc = _VoiceChannel(members=[streamer, other])
    guild = _GuildWithVC(gid=42, voice_channels=[vc])
    bot = _BotWithGuilds([guild])

    cog = LevelingCog.__new__(LevelingCog)
    cog.bot = bot
    await cog._do_voice_tick()

    rec = _state.leveling["42"]["1"]
    # Got voice XP AND stream XP (counters for both bumped).
    assert rec["voice_today"] == 1
    assert rec["stream_today"] == 1
    # XP is the sum of XP_VOICE + XP_STREAM (currently 10 + 15 = 25).
    from src.cogs.leveling_cog import XP_VOICE, XP_STREAM
    assert rec["xp"] == XP_VOICE + XP_STREAM


# ── _announce_levelup ─────────────────────────────────────────────────────────

class _BotWithChannel:
    """Bot stub that exposes get_channel for _announce_levelup."""
    def __init__(self, channels):
        self._channels = channels

    def get_channel(self, ch_id):
        return self._channels.get(ch_id)


async def test_announce_levelup_awards_coin_reward_even_without_channel(db):
    """The coin reward fires regardless of whether a levelup_channel is
    configured (line 269 in leveling_cog.py)."""
    cog = LevelingCog.__new__(LevelingCog)
    cog.bot = _BotWithChannel({})

    # Seed the user at level 5 (display_level == 6 → tier 1 reward = 1000).
    rec = _ensure_lvl_user(42, 7)
    rec["level"] = 5

    await cog._announce_levelup(FakeMember(uid=7, display_name="winner"), guild_id=42)

    expected = levelup_coin_reward(display_level(5))
    assert await _economy.get_balance(7) == expected


async def test_announce_levelup_silent_when_no_channel_configured(db):
    """No levelup_channel in guild_settings → coin still added, no message."""
    cog = LevelingCog.__new__(LevelingCog)
    cog.bot = _BotWithChannel({})

    rec = _ensure_lvl_user(42, 8)
    rec["level"] = 0

    # _state.guild_settings is fresh per autouse fixture.
    await cog._announce_levelup(FakeMember(uid=8), guild_id=42)

    expected = levelup_coin_reward(display_level(0))
    assert await _economy.get_balance(8) == expected
