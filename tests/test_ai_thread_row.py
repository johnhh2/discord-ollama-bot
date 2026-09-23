"""The Continue / Invite / Stop row under AI thread posts
(src/ai_thread_view.py) and the hook in respond() that hangs it."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.ai as _ai
import src.ai_thread_view as _row
import src.state as _state
from src.ai_thread_view import ThreadActionsView, attach_row
from src.cogs.ai_cog import AICog
from src.settings_views import FormModal

from tests.fakes.discord import FakeGuild, FakeMember, FakeMessage, FakeThread

pytestmark = pytest.mark.asyncio

TID = 700


def _thread_state(owner: int = 1, invited=(1, 2)):
    _state.ai_threads[TID] = {"kind": "story", "owner_id": owner, "invited_ids": set(invited), "history": [], "system_prompt": "sp"}


def _interaction(uid: int, thread):
    guild = FakeGuild(gid=42)
    guild.members.extend([FakeMember(1), FakeMember(2), FakeMember(3)])
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: False)
    return SimpleNamespace(id=5, user=FakeMember(uid), channel=thread, guild=guild, response=response, _state=None)


def _button(view, label):
    return next(b for b in view.children if b.label == label)


@pytest.fixture(autouse=True)
def _clean():
    _row._current.clear()
    yield
    _row._current.clear()


async def test_the_cog_registers_the_hook_once_and_respond_calls_it(monkeypatch):
    AICog(bot=None)
    cog = AICog(bot=None)  # the latest construction owns the hook
    assert list(_ai.THREAD_POST_HOOKS) == ["ai_cog"]
    _thread_state()
    thread = FakeThread(thread_id=TID)
    post = FakeMessage(message_id=9)
    thread.send = AsyncMock(return_value=post)

    async def _stream(*args, **kwargs):
        return None
    monkeypatch.setattr(_ai, "_execute_ollama_stream", _stream)
    monkeypatch.setattr(_ai, "save_ai_threads", AsyncMock())
    returned = await _ai.respond(thread, 1, "hi", FakeMessage(), guild_id=42)
    assert returned is post
    post.edit.assert_awaited_once()
    assert isinstance(post.edit.await_args.kwargs["view"], ThreadActionsView)
    assert _row._current[TID][0] is post and _row._current[TID][1].cog is cog


async def test_attach_row_moves_the_buttons_to_the_latest_post():
    _thread_state()
    cog = AICog(bot=None)
    first, second = FakeMessage(message_id=1), FakeMessage(message_id=2)
    await attach_row(cog, first, TID)
    old_view = _row._current[TID][1]
    await attach_row(cog, second, TID)
    assert old_view.is_finished()
    assert first.edit.await_args_list[-1].kwargs == {"view": None}
    assert _row._current[TID][0] is second
    # No registered thread → nothing attached.
    await attach_row(cog, FakeMessage(message_id=3), 999)
    assert 999 not in _row._current


async def test_only_the_threads_group_may_press():
    _thread_state(owner=1, invited=(1, 2))
    view = ThreadActionsView(AICog(bot=None), TID)
    thread = FakeThread(thread_id=TID)
    outsider = _interaction(3, thread)
    assert await view.interaction_check(outsider) is False
    outsider.response.send_message.assert_awaited_once()
    assert await view.interaction_check(_interaction(2, thread)) is True


async def test_continue_runs_the_command_as_the_clicker(monkeypatch):
    _thread_state()
    cog = AICog(bot=SimpleNamespace())
    seen = []

    async def _continue(self, ctx):
        seen.append((ctx.author.id, ctx.channel.id, ctx.invoked_with))
    cog.cmd_continue = SimpleNamespace(callback=_continue, cog=None, name="continue", qualified_name="continue")
    view = ThreadActionsView(cog, TID)
    thread = FakeThread(thread_id=TID)
    interaction = _interaction(2, thread)
    await _button(view, "Continue").callback(interaction)
    assert seen == [(2, TID, "continue")]
    interaction.response.defer.assert_awaited_once()


async def test_invite_opens_a_user_picker_and_runs_invite_with_the_mentions():
    _thread_state()
    cog = AICog(bot=SimpleNamespace())
    seen = []

    async def _invite(self, ctx):
        seen.append([m.id for m in ctx.message.mentions])
    cog.cmd_invite_activity = SimpleNamespace(callback=_invite, cog=None, name="invite", qualified_name="invite")
    view = ThreadActionsView(cog, TID)
    thread = FakeThread(thread_id=TID)
    interaction = _interaction(1, thread)
    await _button(view, "Invite").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, FormModal) and modal.fields[0].kind == "users"
    modal._inputs["users"]._values = [SimpleNamespace(id=3)]
    await modal.on_submit(_interaction(1, thread))
    assert seen == [[3]]
