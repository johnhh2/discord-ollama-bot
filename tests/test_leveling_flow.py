"""Tier F: leveling — voice-tick gating, scratch XP, _announce_levelup.

- _do_voice_tick: skips bots, 1-person channels, private channels;
  awards voice XP only when not muted/deafened; awards stream XP only
  when self_stream is True.
- grant_xp("scratch"): no rate limit (capped upstream by 3/day scratchoff
  limit); other sources are already covered in test_schedulers.py.
- _announce_levelup: awards levelup_coin_reward to the leveled-up user,
  posts to levelup_channel only when configured.
"""
import pytest

import src.state as _state
import src.economy as _economy
from src.leveling import (
    grant_xp, _ensure_lvl_record as _ensure_lvl_user,
    levelup_coin_reward, display_level, XP_SCRATCH,
)
from src.cogs.leveling_cog import LevelingCog

from tests.fakes.discord import FakeMember, FakeGuild, FakeChannel


pytestmark = pytest.mark.asyncio


# ── grant_xp("scratch") ───────────────────────────────────────────────────────

async def test_grant_xp_scratch_grants_unconditionally(db, monkeypatch):
    """`scratch` source has no rate-limit gate (cap is upstream, 3/day)."""
    monkeypatch.setattr("src.cogs.leveling_cog.time.time", lambda: 1_000_000.0)

    xp1, _ = await grant_xp(uid=1, source="scratch", guild_id=42)
    xp2, _ = await grant_xp(uid=1, source="scratch", guild_id=42)
    xp3, _ = await grant_xp(uid=1, source="scratch", guild_id=42)

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
    from src.leveling import XP_VOICE, XP_STREAM
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
    configured."""
    cog = LevelingCog.__new__(LevelingCog)
    cog.bot = _BotWithChannel({})

    # Seed the user at level 5 (display_level == 6 → tier 1 reward = 1000).
    rec = _ensure_lvl_user(42, 7)
    rec["level"] = 5

    await cog._announce_levelup(FakeMember(uid=7, display_name="winner"), guild_id=42)

    expected = levelup_coin_reward(display_level(5))
    assert await _economy.get_balance(7) == expected


async def test_announce_levelup_lists_newly_purchasable_artifacts(db):
    """Hitting a level with an artifact gate (5 → slots blank remover) lists
    the artifact in the Unlocked section of the announcement."""
    from src.config import ARTIFACT_SLOTS_BLANK_COST

    cog = LevelingCog.__new__(LevelingCog)
    channel = FakeChannel(ch_id=555)
    cog.bot = _BotWithChannel({555: channel})
    _state.guild_settings["42"] = {"levelup_channel": 555}

    rec = _ensure_lvl_user(42, 9)
    rec["level"] = 4  # display level 5

    await cog._announce_levelup(FakeMember(uid=9), guild_id=42)

    channel.send.assert_awaited_once()
    desc = channel.send.await_args.kwargs["embed"].description
    assert "🔓 Unlocked" in desc
    assert "🏺 New artifact for sale" in desc
    assert "⬛" in desc
    assert f"{ARTIFACT_SLOTS_BLANK_COST:,}" in desc


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


# ── Leveling runs silently without a level-up channel ────────────────────────
# The two entry points that level users up in the background (a message, a
# completed command) with no levelup_channel configured: XP lands, the level
# moves, the coin reward is paid, and nothing is posted.

class _StubBot:
    def __init__(self, lvl_cog):
        self.user = type("U", (), {"id": 999_999_999})()
        self.cogs = {"LevelingCog": lvl_cog}


def _silent_setup(gid: int, uid: int):
    from src.leveling import xp_for_level
    lvl_cog = LevelingCog.__new__(LevelingCog)
    lvl_cog.bot = _BotWithChannel({})
    rec = _ensure_lvl_user(gid, uid)
    rec["xp"] = xp_for_level(1) - 1  # one grant away from the first level-up
    return lvl_cog, rec


async def _drain_tasks():
    # The announce runs as a fire-and-forget task off the listener.
    import asyncio
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


async def test_message_xp_levels_up_silently_without_channel(db):
    from src.events import EventsCog
    from tests.fakes.discord import FakeMessage

    lvl_cog, rec = _silent_setup(42, 11)
    events = EventsCog(bot=_StubBot(lvl_cog))
    message = FakeMessage(content="hello", author=FakeMember(uid=11))
    message.guild = FakeGuild(gid=42)

    await events._handle_msg_xp(message)
    await _drain_tasks()

    assert rec["level"] == 1
    assert await _economy.get_balance(11) == levelup_coin_reward(display_level(1))
    assert _state.levelups_today.get((42, "11")) == 1
    message.channel.send.assert_not_awaited()


async def test_command_xp_levels_up_silently_without_channel(db):
    from src.events import EventsCog
    from tests.fakes.discord import FakeCtx

    lvl_cog, rec = _silent_setup(42, 12)
    events = EventsCog(bot=_StubBot(lvl_cog))
    ctx = FakeCtx(author=FakeMember(uid=12), guild=FakeGuild(gid=42), channel=FakeChannel(ch_id=100))
    ctx.command.cog = None  # stats bucketing reads ctx.command.cog

    await events.on_command_completion(ctx)
    await _drain_tasks()

    assert rec["level"] == 1
    assert await _economy.get_balance(12) == levelup_coin_reward(display_level(1))
    assert ctx.sent_embeds == []
