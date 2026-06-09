"""VoiceCog: subscription pings and the per-subscriber ignore list.

The behavior under test is the empty→active DM and `!subscribe ignore`. The key
invariant for the ignore list: an ignored member triggering a channel does NOT
ping the subscriber AND does NOT consume the subscriber's per-channel cooldown,
so a later non-ignored trigger still pings.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import src.state as _state
from src.cogs.voice_cog import VoiceCog


_aio = pytest.mark.asyncio

GUILD_ID = 42
CHANNEL_ID = 100
SUBSCRIBER_ID = 7
TRIGGER_ID = 9


def _make_cog(user_dms):
    """Cog whose bot.get_user returns a stub user recording .send() calls."""
    sent_to = {}

    def _get_user(uid):
        if uid not in user_dms:
            return None
        u = SimpleNamespace()
        u.send = AsyncMock()
        sent_to[uid] = u.send
        return u

    fake_bot = SimpleNamespace(get_user=_get_user, fetch_user=AsyncMock())
    cog = VoiceCog(bot=fake_bot)
    return cog, sent_to


def _voice_channel(members):
    # spec= makes isinstance(ch, discord.VoiceChannel) true in the listener while
    # letting us set .members (a read-only property on the real class).
    ch = MagicMock(spec=discord.VoiceChannel)
    ch.id = CHANNEL_ID
    ch.name = "General"
    ch.members = members
    ch.guild = SimpleNamespace(id=GUILD_ID, name="Test Guild")
    return ch


def _member(uid, *, bot=False):
    return SimpleNamespace(id=uid, bot=bot, display_name=f"user{uid}")


async def _fire_join(cog, *, trigger_id, already_present=()):
    """Fire the listener for trigger_id joining.

    `already_present` is an iterable of member specs already in the channel
    *before* this join — each is an id, or a (id, {"bot": True}) tuple. The
    channel's member roster passed to the listener includes the just-joined
    trigger plus everyone in `already_present`.
    """
    trigger = _member(trigger_id)
    members = [trigger]
    for spec in already_present:
        if isinstance(spec, tuple):
            uid, opts = spec
            members.append(_member(uid, bot=opts.get("bot", False)))
        else:
            members.append(_member(spec))
    channel = _voice_channel(members)
    before = SimpleNamespace(channel=None)
    after = SimpleNamespace(channel=channel)
    await cog.on_voice_state_update(trigger, before, after)


@pytest.fixture(autouse=True)
def _seed_subscription(monkeypatch):
    """One subscriber to CHANNEL_ID, no ignores, with DB writes stubbed."""
    _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)] = {
        "guild_id": GUILD_ID,
        "last_pinged_at": None,
    }
    import src.cogs.voice_cog as _vc
    monkeypatch.setattr(_vc, "update_voice_ping_last_pinged", AsyncMock())
    yield


@_aio
async def test_trigger_pings_subscriber_and_sets_cooldown():
    cog, sent_to = _make_cog({SUBSCRIBER_ID})
    await _fire_join(cog, trigger_id=TRIGGER_ID)

    sent_to[SUBSCRIBER_ID].assert_awaited_once()
    assert _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)]["last_pinged_at"] is not None


@_aio
async def test_ignored_trigger_does_not_ping_and_does_not_burn_cooldown():
    _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] = {TRIGGER_ID}
    cog, sent_to = _make_cog({SUBSCRIBER_ID})

    await _fire_join(cog, trigger_id=TRIGGER_ID)

    assert SUBSCRIBER_ID not in sent_to  # get_user never resolved into a send
    # Cooldown untouched: a later non-ignored trigger must still be able to ping.
    assert _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)]["last_pinged_at"] is None


@_aio
async def test_ignore_then_non_ignored_trigger_still_pings():
    _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] = {TRIGGER_ID}
    cog, sent_to = _make_cog({SUBSCRIBER_ID})

    # Ignored member fills the channel — no ping, no cooldown.
    await _fire_join(cog, trigger_id=TRIGGER_ID)
    assert SUBSCRIBER_ID not in sent_to

    # A different (non-ignored) member fills it — ping fires because the cooldown
    # was never consumed by the ignored trigger.
    await _fire_join(cog, trigger_id=TRIGGER_ID + 1)
    sent_to[SUBSCRIBER_ID].assert_awaited_once()
    assert _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)]["last_pinged_at"] is not None


@_aio
async def test_ignore_is_per_subscriber_not_global():
    """Subscriber B ignoring the trigger must not suppress subscriber A's ping."""
    other_sub = SUBSCRIBER_ID + 50
    _state.voice_pings[(CHANNEL_ID, other_sub)] = {
        "guild_id": GUILD_ID, "last_pinged_at": None,
    }
    # other_sub ignores the trigger; SUBSCRIBER_ID does not.
    _state.voice_ping_ignores[(GUILD_ID, other_sub)] = {TRIGGER_ID}

    cog, sent_to = _make_cog({SUBSCRIBER_ID, other_sub})
    await _fire_join(cog, trigger_id=TRIGGER_ID)

    sent_to[SUBSCRIBER_ID].assert_awaited_once()   # A still pinged
    assert other_sub not in sent_to                # B suppressed


# ── "first non-ignored arrival" — ping when a non-ignored user joins after an
#    ignored one already filled the channel ───────────────────────────────────

@_aio
async def test_non_ignored_second_joiner_pings_after_ignored_first():
    """Ignored user joins first (0→1), then a non-ignored user joins second
    (1→2). The subscriber should be pinged for the second joiner."""
    _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] = {TRIGGER_ID}
    cog, sent_to = _make_cog({SUBSCRIBER_ID})

    # Ignored user is first — no ping, no cooldown.
    await _fire_join(cog, trigger_id=TRIGGER_ID)
    assert SUBSCRIBER_ID not in sent_to

    # Non-ignored user joins as the 2nd member (the ignored user is still there).
    other = TRIGGER_ID + 1
    await _fire_join(cog, trigger_id=other, already_present=[TRIGGER_ID])
    sent_to[SUBSCRIBER_ID].assert_awaited_once()
    assert _state.voice_pings[(CHANNEL_ID, SUBSCRIBER_ID)]["last_pinged_at"] is not None


@_aio
async def test_no_double_ping_for_third_relevant_joiner():
    """Once a non-ignored user is present and pinged, a further non-ignored
    joiner shouldn't re-ping (they aren't the first relevant arrival)."""
    cog, sent_to = _make_cog({SUBSCRIBER_ID})

    # First non-ignored user → ping.
    await _fire_join(cog, trigger_id=TRIGGER_ID)
    sent_to[SUBSCRIBER_ID].assert_awaited_once()

    # A second non-ignored user joins while the first is still present. Not the
    # first relevant arrival → no new ping (cooldown would also block it).
    await _fire_join(cog, trigger_id=TRIGGER_ID + 1, already_present=[TRIGGER_ID])
    sent_to[SUBSCRIBER_ID].assert_awaited_once()  # still exactly one


@_aio
async def test_bots_present_dont_block_first_relevant_arrival():
    """A bot already in the channel doesn't count as a relevant member, so a
    human joining second is still the first relevant arrival."""
    cog, sent_to = _make_cog({SUBSCRIBER_ID})
    bot_id = 555
    await _fire_join(
        cog, trigger_id=TRIGGER_ID,
        already_present=[(bot_id, {"bot": True})],
    )
    sent_to[SUBSCRIBER_ID].assert_awaited_once()


# ── !subscribe ignore command (round-trips through the real DB) ──────────────

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember  # noqa: E402
from src.persistence import load_voice_ping_ignores  # noqa: E402


def _ctx():
    author = FakeMember(uid=SUBSCRIBER_ID)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=GUILD_ID),
                  command_name="subscribe ignore")
    return ctx


def _target_member(uid=TRIGGER_ID, *, bot=False):
    return SimpleNamespace(id=uid, bot=bot, display_name=f"user{uid}")


@_aio
async def test_ignore_command_adds_then_removes(db):
    cog, _ = _make_cog(set())
    ctx = _ctx()
    target = _target_member()

    # Add.
    await cog.cmd_subscribe_ignore.callback(cog, ctx, member=target)
    assert TRIGGER_ID in _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)]
    assert (await load_voice_ping_ignores())[(GUILD_ID, SUBSCRIBER_ID)] == {TRIGGER_ID}
    assert any("Ignoring" in (e.title or "") for e in ctx.sent_embeds)

    # Toggle off — same command, same target.
    ctx2 = _ctx()
    await cog.cmd_subscribe_ignore.callback(cog, ctx2, member=target)
    assert (GUILD_ID, SUBSCRIBER_ID) not in _state.voice_ping_ignores
    assert (await load_voice_ping_ignores()) == {}
    assert any("No Longer Ignoring" in (e.title or "") for e in ctx2.sent_embeds)


@_aio
async def test_ignore_command_lists_when_no_member(db):
    cog, _ = _make_cog(set())
    _state.voice_ping_ignores[(GUILD_ID, SUBSCRIBER_ID)] = {TRIGGER_ID}
    ctx = _ctx()
    ctx.guild.members = [FakeMember(uid=TRIGGER_ID)]

    await cog.cmd_subscribe_ignore.callback(cog, ctx, member=None)
    assert any("Ignored Triggers" in (e.title or "") for e in ctx.sent_embeds)


@_aio
async def test_ignore_command_rejects_self_and_bots(db):
    cog, _ = _make_cog(set())

    ctx = _ctx()
    await cog.cmd_subscribe_ignore.callback(
        cog, ctx, member=_target_member(uid=SUBSCRIBER_ID))
    assert any("Yourself" in (e.title or "") for e in ctx.sent_embeds)

    ctx2 = _ctx()
    await cog.cmd_subscribe_ignore.callback(
        cog, ctx2, member=_target_member(bot=True))
    assert any("Bots" in (e.title or "") for e in ctx2.sent_embeds)
    assert (GUILD_ID, SUBSCRIBER_ID) not in _state.voice_ping_ignores
