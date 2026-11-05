"""The !assets buttons and dropdowns (src/assets_view.py)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.assets_view as _view
import src.state as _state
from src.assets_view import AssetsView, _PropertySelect
from src.cogs.assets_cog import AssetsCog
from src.persistence import save_property_owner
from tests.fakes.discord import FakeMember, FakeMessage
from tests.test_properties import GID, _ctx, _give_property, _set_level

pytestmark = pytest.mark.asyncio


def _interaction(uid: int):
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
                               edit_message=AsyncMock(), is_done=lambda: False)
    return SimpleNamespace(id=5, user=FakeMember(uid=uid), guild=SimpleNamespace(id=GID), channel=None,
                           message=None, response=response, _state=None)


def _button(view, label):
    return next(b for b in view.children if getattr(b, "label", None) == label)


def _labels(view):
    return [getattr(b, "label", None) for b in view.children]


async def _pick(view, pid, uid):
    select = next(i for i in view.children if isinstance(i, _PropertySelect) and pid in {o.value for o in i.options})
    select._values = [pid]
    await select.callback(_interaction(uid))


@pytest.fixture
def forwarded(monkeypatch):
    """Record what a press forwards instead of running the command."""
    calls = []

    async def _forward(ctx, command, *args, cog=None, **kwargs):
        calls.append((command.name, args, kwargs))
    monkeypatch.setattr(_view, "forward", _forward)
    monkeypatch.setattr(_view, "button_context", lambda bot, interaction, command, **kw: SimpleNamespace())
    monkeypatch.setattr(_view, "refusal_for", lambda ctx, command, **kw: None)
    return calls


async def test_own_portfolio_has_browse_and_manage_but_not_the_subcommand_list(db):
    await _give_property(1, "lemonade_stand")
    ctx = _ctx(1)
    await AssetsCog.cmd_assets.callback(AssetsCog(bot=None), ctx)
    view = ctx.sent_views[-1]
    assert _labels(view) == ["Browse offerings", "Manage properties"]
    assert "Subcommands" not in ctx.sent_embeds[-1].description


async def test_someone_elses_portfolio_has_no_buttons(db):
    ctx = _ctx(1)
    await AssetsCog.cmd_assets.callback(AssetsCog(bot=None), ctx, FakeMember(uid=2))
    assert ctx.sent_views == []


async def test_browse_lines_each_say_one_available(db):
    _set_level(1, 50)
    ctx = _ctx(1)
    await AssetsCog.assets_browse.callback(AssetsCog(bot=None), ctx, 1)
    text = ctx.sent_embeds[-1].description
    lemonade = next(line for line in text.splitlines() if "Lemonade Stand" in line)
    assert lemonade.endswith("• 1 available")
    assert "available" not in next(line for line in text.splitlines() if line.startswith("**Tier 1**"))


async def test_browse_dropdown_then_buy_forwards_the_pick(db, forwarded):
    _set_level(1, 50)
    await _give_property(2, "car_wash")
    ctx = _ctx(1)
    await AssetsCog.assets_browse.callback(AssetsCog(bot=None), ctx)
    view = ctx.sent_views[-1]
    view.message = FakeMessage()
    offered = {o.value for s in view.children if isinstance(s, _PropertySelect) for o in s.options}
    assert "lemonade_stand" in offered and "car_wash" not in offered
    assert _button(view, "Buy").disabled
    await _pick(view, "lemonade_stand", 1)
    await _button(view, "Buy").callback(_interaction(1))
    assert forwarded == [("buy", (), {"name": "lemonade_stand"})]


async def test_buy_with_no_name_or_an_unknown_one_offers_the_dropdown(db):
    for name in (None, "Moon Base"):
        ctx = _ctx(1)
        await AssetsCog.assets_buy.callback(AssetsCog(bot=None), ctx, name=name)
        view = ctx.sent_views[-1]
        assert view.mode == "browse" and any(isinstance(i, _PropertySelect) for i in view.children)
    assert "Unknown Property" in ctx.sent_embeds[-1].title


async def test_manage_shows_the_actions_for_the_picked_deed(db, forwarded):
    await _give_property(1, "lemonade_stand")
    row = _state.property_owners["lemonade_stand"]
    row.update(list_price=50_000)
    await save_property_owner("lemonade_stand", row)
    view = AssetsView(AssetsCog(bot=None), FakeMember(uid=1), mode="home", home=True)
    view.message = FakeMessage()
    await _button(view, "Manage properties").callback(_interaction(1))
    assert "Sell" not in _labels(view)
    await _pick(view, "lemonade_stand", 1)
    assert _labels(view)[1:] == ["Sell", "Unlist", "Upgrade", "Rename", "Back"]

    await _button(view, "Unlist").callback(_interaction(1))
    assert forwarded[-1] == ("unlist", (), {"name": "lemonade_stand"})
    press = _interaction(1)
    await _button(view, "Sell").callback(press)
    modal = press.response.send_modal.await_args.args[0]
    modal._inputs["price"]._value = "40k"
    await modal.on_submit(_interaction(1))
    assert forwarded[-1] == ("sell", ("lemonade_stand", "40k"), {})


async def test_manage_hides_unlist_and_upgrade_when_they_dont_apply(db):
    await _give_property(1, "lemonade_stand")
    _state.property_owners["lemonade_stand"]["upgraded"] = True
    view = AssetsView(AssetsCog(bot=None), FakeMember(uid=1), mode="manage")
    view.message = FakeMessage()
    await _pick(view, "lemonade_stand", 1)
    assert _labels(view)[1:] == ["Sell", "Rename"]


async def test_only_the_invoker_can_press(db):
    view = AssetsView(AssetsCog(bot=None), FakeMember(uid=1), mode="browse")
    stranger = _interaction(2)
    assert await view.interaction_check(stranger) is False
    stranger.response.send_message.assert_awaited()
