"""Notification policy for gambling/game/interaction messages.

Rule: a message triggered by a user's own action (a gamble, a move, a claim
click) must be sent silent — that user is already watching the channel.
The only loud sends are ones whose job is to ping someone who *isn't* the
actor: game invites, and record announcements from the scheduled lottery
draw (notify=True).

These tests pin the policy at its three most regression-prone points:
announce_record's holder ping, the raw-channel gambling result sends
(which bypass SilentContext), and the invite send (which must opt OUT of
SilentContext's silent default and carry its mentions in content, since
embed mentions never notify).
"""
import asyncio

import pytest

import src.economy as _economy
from src.helpers import announce_record
from src.gambling.flip import play_flip
from src.invites import _wait_for_confirmations

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember


class _RecordingChannel:
    """Bare channel fake that records every send's (args, kwargs)."""
    def __init__(self, guild=None):
        self.guild = guild
        self.sent: list[tuple[tuple, dict]] = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return None


@pytest.mark.asyncio
async def test_announce_record_holder_ping_is_silent_by_default(db):
    """The achiever normally just gambled in this channel — mention for the
    highlight, but no push notification."""
    chan = _RecordingChannel(guild=FakeGuild(gid=42))
    await announce_record(chan, "flip", "Joseph", 5_000, holder_id=77)

    assert len(chan.sent) == 1
    _, kwargs = chan.sent[0]
    assert kwargs.get("content") == "<@77>"
    assert kwargs.get("silent") is True


@pytest.mark.asyncio
async def test_announce_record_notify_pings_loud(db):
    """notify=True (lottery draw — holder isn't present) keeps a real ping."""
    chan = _RecordingChannel(guild=FakeGuild(gid=42))
    await announce_record(chan, "lottery", "Joseph", 5_000, holder_id=77, notify=True)

    _, kwargs = chan.sent[0]
    assert kwargs.get("content") == "<@77>"
    assert kwargs.get("silent") is False


@pytest.mark.asyncio
async def test_play_flip_result_is_silent(db):
    """Gambling results go through raw channel.send (no SilentContext) — the
    silent flag must be explicit."""
    author = FakeMember(uid=77, display_name="player")
    guild = FakeGuild(gid=42)
    chan = _RecordingChannel(guild=guild)
    await _economy.add_balance(77, 10_000)

    await play_flip(author, chan, guild, 100)

    assert chan.sent, "flip should announce its result"
    for _, kwargs in chan.sent:
        assert kwargs.get("silent") is True


@pytest.mark.asyncio
async def test_invite_send_is_loud_with_content_mentions(db):
    """Invites are the flip side of the policy: the invitee hasn't acted yet,
    so the invite must override SilentContext's silent default AND put the
    mentions in content (embed mentions never notify)."""
    ctx = FakeCtx(author=FakeMember(uid=1, display_name="host"), guild=FakeGuild(gid=42))

    class _Bot:
        async def wait_for(self, *a, **kw):
            raise asyncio.TimeoutError

    ctx.bot = _Bot()
    invitee = FakeMember(uid=2, display_name="guest")

    await _wait_for_confirmations(ctx, [invitee], timeout=0.01)

    args, kwargs = ctx.send_mock.call_args
    assert kwargs.get("silent") is False
    # content is passed positionally through FakeCtx.send
    content = args[0] if args else kwargs.get("content")
    assert content == invitee.mention
