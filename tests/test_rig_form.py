"""`!rig` without a game or a player opens a form; a submit runs the typed subcommand."""
import pytest

import src.gambling.slots as _slots
import src.state as _state
from src.gambling.slots import SlotsCog

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember
from tests.test_money_flows import _StubBot

pytestmark = pytest.mark.asyncio

GID = 42
ADMIN, PLAYER = 7_301, 7_302


def _ctx():
    guild = FakeGuild(gid=GID)
    guild.members.append(FakeMember(uid=PLAYER, display_name="player"))
    return FakeCtx(author=FakeMember(uid=ADMIN, display_name="admin"), guild=guild)


def _form(monkeypatch, answer):
    seen = {}

    async def _open_form(ctx, *, fields, **kwargs):
        seen["fields"] = {f.key: f for f in fields}
        return answer
    monkeypatch.setattr(_slots, "open_form", _open_form)
    return seen


async def test_bare_rig_asks_for_game_player_and_quantity_then_rigs(db, monkeypatch):
    seen = _form(monkeypatch, {"kind": ["flip"], "user": [PLAYER], "qty": "5"})
    cog = SlotsCog(bot=_StubBot())
    await cog.cmd_rig.callback(cog, _ctx())
    fields = seen["fields"]
    assert [v for _, v in fields["kind"].options] == ["slots", "flip", "scratch", "steal"]
    assert fields["user"].kind == "users" and not fields["qty"].required
    assert _state.rigged_flips[PLAYER] == 5


async def test_a_subcommand_without_a_player_preselects_its_game_and_quantity(db, monkeypatch):
    seen = _form(monkeypatch, {"kind": ["steal"], "user": [PLAYER], "qty": ""})
    cog = SlotsCog(bot=_StubBot())
    await cog.cmd_rig_steal.callback(cog, _ctx(), None, "3")
    assert seen["fields"]["kind"].defaults == ("steal",) and seen["fields"]["qty"].default == "3"
    assert _state.rigged_steal[PLAYER] == 1  # the submit left quantity empty: the default


async def test_a_dismissed_form_rigs_nothing(db, monkeypatch):
    _form(monkeypatch, None)
    cog = SlotsCog(bot=_StubBot())
    await cog.cmd_rig_slots.callback(cog, _ctx(), None)
    assert PLAYER not in _state.rigged_slots
