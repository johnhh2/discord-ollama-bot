"""Invite prompts (src/invites.py): Accept / Decline buttons for games and
AI threads."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.invites import InviteView, _send_invite, _wait_for_confirmations
from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember, FakeMessage

pytestmark = pytest.mark.asyncio


def _interaction(user):
    return SimpleNamespace(user=user, response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()))


def _button(view, label):
    return next(b for b in view.children if b.label == label)


def _invite_ctx(invite_msg: FakeMessage, clicks):
    """A ctx whose invite message, once sent, receives `clicks` — (label,
    user) pairs — as button presses on the posted view."""
    ctx = FakeCtx(author=FakeMember(uid=1, display_name="host"), guild=FakeGuild(gid=42))
    posted = {}

    async def _send(content=None, *, embed=None, view=None, **kwargs):
        posted["view"], posted["kwargs"], posted["content"] = view, kwargs, content

        async def _press():
            for label, user in clicks:
                await _button(view, label).callback(_interaction(user))
        asyncio.get_running_loop().create_task(_press())
        return invite_msg
    ctx.send = _send
    return ctx, posted


async def test_accept_confirms_and_the_invite_is_loud_with_content_mentions():
    invite_msg = FakeMessage(message_id=60)
    invitee = FakeMember(uid=2, display_name="guest")
    ctx, posted = _invite_ctx(invite_msg, [("Accept", invitee)])

    confirmed = await _wait_for_confirmations(ctx, [invitee], timeout=5.0)

    assert confirmed == {invitee.id}
    invite_msg.delete.assert_awaited_once()
    # The ping is the delivery: not silent, mentions in content.
    assert posted["kwargs"].get("silent") is False and posted["content"] == invitee.mention
    assert isinstance(posted["view"], InviteView)


async def test_decline_ends_the_wait_early_with_nobody_confirmed():
    invitee = FakeMember(uid=2)
    ctx, _ = _invite_ctx(FakeMessage(), [("Decline", invitee)])
    assert await _wait_for_confirmations(ctx, [invitee], timeout=5.0) == set()


async def test_only_invitees_may_press_and_a_stranger_is_told_privately():
    view = InviteView({2}, timeout=5)
    stranger = _interaction(FakeMember(uid=3))
    assert await view.interaction_check(stranger) is False
    stranger.response.send_message.assert_awaited_once()
    assert await view.interaction_check(_interaction(FakeMember(uid=2))) is True


async def test_the_view_waits_for_every_invitee_and_an_acceptance_beats_a_later_decline():
    a, b = FakeMember(uid=2), FakeMember(uid=3)
    view = InviteView({a.id, b.id}, timeout=5)
    await _button(view, "Accept").callback(_interaction(a))
    assert not view.is_finished()
    await _button(view, "Decline").callback(_interaction(a))  # changed their mind — too late
    await _button(view, "Decline").callback(_interaction(b))
    assert view.is_finished() and view.accepted == {a.id} and view.declined == {b.id}


async def test_timeout_returns_whoever_accepted_so_far():
    invitee, slow = FakeMember(uid=2), FakeMember(uid=3)
    ctx, _ = _invite_ctx(FakeMessage(), [("Accept", invitee)])
    assert await _wait_for_confirmations(ctx, [invitee, slow], timeout=0.05) == {invitee.id}


async def test_send_invite_calls_on_join_once_per_acceptance():
    invitee = FakeMember(uid=2)
    ctx, posted = _invite_ctx(FakeMessage(message_id=61), [])
    joined = []

    async def _on_join(user):
        joined.append(user)
    await _send_invite(ctx, [invitee], on_join=_on_join)
    view = posted["view"]
    await _button(view, "Accept").callback(_interaction(invitee))
    await _button(view, "Accept").callback(_interaction(invitee))
    assert joined == [invitee] and view.is_finished()
