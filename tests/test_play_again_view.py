"""PlayAgainView — the shared play-again buttons under !slots / !flip results.

Game-specific wiring (which buttons, which stakes, that a click re-charges)
lives in test_slots_flow.py / test_flip_multi.py. This file pins the view's
own contract against a stub replay: owner-only, one replay per set, the
blocklist gate on the click path, and the buttons vanishing on timeout.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
from src.gambling.play_again import PlayAgainView, PLAY_AGAIN_TIMEOUT
from tests.fakes.discord import FakeMember, FakeGuild, FakeMessage

pytestmark = pytest.mark.asyncio

OWNER = FakeMember(uid=1, display_name="owner")


class _FakeInteraction:
    def __init__(self, user):
        self.user = user
        self.response = SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock(),
        )


def _view():
    replay = AsyncMock()
    view = PlayAgainView(
        OWNER, FakeGuild(gid=42), replay=replay,
        options=[("Again · 100 🪙", 100), ("Double · 200 🪙", 200)],
        not_yours="Not yours.",
    )
    view.message = FakeMessage()
    return view, replay


async def test_buttons_carry_their_stakes_and_expire_after_15s():
    view, _ = _view()
    assert [b.stake for b in view.children] == [100, 200]
    assert [b.label for b in view.children] == ["Again · 100 🪙", "Double · 200 🪙"]
    assert view.timeout == PLAY_AGAIN_TIMEOUT == 15.0
    view.stop()


async def test_click_drops_buttons_and_replays_that_stake():
    view, replay = _view()
    interaction = _FakeInteraction(OWNER)

    await view.children[1].callback(interaction)

    interaction.response.edit_message.assert_awaited_once_with(view=None)
    replay.assert_awaited_once_with(200)
    assert view.is_finished()


async def test_second_click_on_the_same_set_is_ignored():
    view, replay = _view()
    await view.children[0].callback(_FakeInteraction(OWNER))
    second = _FakeInteraction(OWNER)

    await view.children[1].callback(second)

    second.response.defer.assert_awaited_once()
    second.response.edit_message.assert_not_awaited()
    replay.assert_awaited_once_with(100)


async def test_other_user_is_rejected_ephemerally():
    view, replay = _view()
    intruder = _FakeInteraction(FakeMember(uid=2, display_name="intruder"))

    await view.children[0].callback(intruder)

    intruder.response.send_message.assert_awaited_once_with("Not yours.", ephemeral=True)
    intruder.response.edit_message.assert_not_awaited()
    replay.assert_not_awaited()
    assert not view.is_finished()   # the owner can still click
    view.stop()


async def test_user_banned_after_the_result_cannot_replay():
    """Blocklist gate on the click path: a ban that lands between the result
    and the click must stop the replay — no message passes on_message."""
    view, replay = _view()
    _state.blocklist[(42, OWNER.id)] = {"reason": "t", "banned_by": 1, "banned_at": None}
    try:
        interaction = _FakeInteraction(OWNER)
        await view.children[0].callback(interaction)
    finally:
        _state.blocklist.pop((42, OWNER.id), None)

    interaction.response.defer.assert_awaited_once()
    interaction.response.edit_message.assert_not_awaited()
    replay.assert_not_awaited()
    view.stop()


async def test_timeout_removes_the_buttons_from_the_message():
    view, _ = _view()

    await view.on_timeout()

    view.message.edit.assert_awaited_once_with(view=None)


async def test_timeout_after_a_click_leaves_the_message_alone():
    """The click already stripped the buttons via the interaction edit."""
    view, _ = _view()
    await view.children[0].callback(_FakeInteraction(OWNER))

    await view.on_timeout()

    view.message.edit.assert_not_awaited()


async def test_timeout_survives_a_deleted_message():
    view, _ = _view()
    gone = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
    view.message.edit = AsyncMock(side_effect=gone)

    await view.on_timeout()   # must not raise
