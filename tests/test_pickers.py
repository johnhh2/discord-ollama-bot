"""src/pickers.py: the dropdowns a name lookup opens when it misses.

conftest replaces the four prompt entry points with a decline for every test,
so the real ones are bound here at import time, before that patch applies.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import pickers
from src.pickers import (
    _OneOfSelect, NONE_VALUE, NONE_LABEL,
    choose_member as _real_choose_member,
    choose_voice_channel as _real_choose_voice_channel,
)
from src.settings_views import _OwnedView, MAX_OPTIONS
from tests.fakes.discord import FakeCtx, FakeMember

_aio = pytest.mark.asyncio


def _interaction(uid=1):
    return SimpleNamespace(user=SimpleNamespace(id=uid), response=SimpleNamespace(defer=AsyncMock()))


def _select(options):
    view = _OwnedView(owner_id=1, timeout=10)
    sel = _OneOfSelect(options, "Pick…")
    view.add_item(sel)
    return view, sel


def test_none_of_the_above_is_always_the_last_option():
    _, sel = _select([(f"user{i}", str(i), "") for i in range(40)])
    assert len(sel.options) == MAX_OPTIONS  # Discord's cap, last slot reserved
    assert sel.options[-1].label == NONE_LABEL
    assert sel.options[-1].value == NONE_VALUE

    _, sel = _select([("alice", "1", "@alice1"), ("bob", "2", "")])
    assert [o.label for o in sel.options] == ["alice", "bob", NONE_LABEL]
    assert sel.options[0].description == "@alice1"
    assert sel.options[1].description is None


@_aio
async def test_a_pick_resolves_and_none_of_the_above_declines():
    view, sel = _select([("alice", "1", ""), ("alicia", "2", "")])
    sel._values = ["2"]
    await sel.callback(_interaction())
    assert view.result == "2"
    assert view.is_finished()

    view, sel = _select([("alice", "1", ""), ("alicia", "2", "")])
    sel._values = [NONE_VALUE]
    await sel.callback(_interaction())
    assert view.result is None
    assert view.closed_as == "🚫 None of the above"


@_aio
async def test_only_the_invoker_can_answer():
    view, _ = _select([("alice", "1", "")])
    other = SimpleNamespace(user=SimpleNamespace(id=2),
                            response=SimpleNamespace(send_message=AsyncMock()))
    assert await view.interaction_check(other) is False
    other.response.send_message.assert_awaited_once()
    assert await view.interaction_check(_interaction(uid=1)) is True


@_aio
async def test_choose_member_maps_the_pick_back_to_the_member(monkeypatch):
    alice = FakeMember(uid=1, display_name="alice")
    alicia = FakeMember(uid=2, display_name="alicia")
    seen = {}

    async def _pick_one(ctx, *, title, description, options, **kw):
        seen["options"] = options
        seen["description"] = description
        return "2"
    monkeypatch.setattr(pickers, "pick_one", _pick_one)
    ctx = FakeCtx()
    assert await _real_choose_member(ctx, "ali", [alice, alicia]) is alicia
    assert [(label, value) for label, value, _ in seen["options"]] == [("alice", "1"), ("alicia", "2")]
    assert "**ali**" in seen["description"]

    async def _declined(ctx, **kw):
        return None
    monkeypatch.setattr(pickers, "pick_one", _declined)
    assert await _real_choose_member(ctx, "ali", [alice, alicia]) is None


@_aio
async def test_choose_voice_channel_maps_the_pick_back_to_the_channel(monkeypatch):
    general = SimpleNamespace(id=10, name="🔊 General")
    general2 = SimpleNamespace(id=11, name="General 2")

    async def _pick_one(ctx, *, options, **kw):
        assert [label for label, _, _ in options] == ["🔊 General", "General 2"]
        return "11"
    monkeypatch.setattr(pickers, "pick_one", _pick_one)
    assert await _real_choose_voice_channel(FakeCtx(), "gen", [general, general2]) is general2
