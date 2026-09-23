"""The settings prompts (src/settings_views.py) and the bare settings commands
that open them instead of printing a usage line."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.cogs.settings_cog as _settings_cog
from src.cogs.settings_cog import SettingsCog
from src.guild_config import get_guild_cfg
from src.settings_views import (
    _ChannelView, _OwnedView, _ToggleView, pick_channels, pick_from_list,
)

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio


def _interaction(uid: int = 1):
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock())
    return SimpleNamespace(user=FakeMember(uid), response=response)


def _channel(cid: int):
    return SimpleNamespace(id=cid, mention=f"<#{cid}>")


def _item(view, cls_name: str):
    return next(i for i in view.children if type(i).__name__ == cls_name)


# ── views ────────────────────────────────────────────────────────────────────

async def test_only_the_invoker_can_use_a_prompt():
    view = _OwnedView(owner_id=1, timeout=5)
    intruder = _interaction(uid=2)
    assert await view.interaction_check(intruder) is False
    intruder.response.send_message.assert_awaited_once()
    assert await view.interaction_check(_interaction(uid=1)) is True


async def test_single_channel_select_saves_on_pick():
    view = _ChannelView(1, [], multi=False, timeout=5)
    select = _item(view, "_ChannelSelect")
    select._values = [_channel(10)]
    await select.callback(_interaction())
    assert [c.id for c in view.result] == [10] and view.is_finished()


async def test_multi_channel_select_waits_for_save_and_clear_returns_empty():
    view = _ChannelView(1, [5], multi=True, timeout=5)
    save = _item(view, "_SaveButton")

    nothing_picked = _interaction()
    await save.callback(nothing_picked)
    nothing_picked.response.send_message.assert_awaited_once()
    assert not view.is_finished()

    select = _item(view, "_ChannelSelect")
    select._values = [_channel(10), _channel(11)]
    await select.callback(_interaction())
    assert not view.is_finished()
    await save.callback(_interaction())
    assert [c.id for c in view.result] == [10, 11]

    cleared = _ChannelView(1, [5], multi=True, timeout=5)
    await _item(cleared, "_ClearButton").callback(_interaction())
    assert cleared.result == []


async def test_toggle_button_saves_before_it_repaints():
    calls = []

    async def _on_toggle(key, enabled):
        calls.append((key, enabled))
    view = _ToggleView(1, {"role": ("role", True)}, _on_toggle, timeout=5)
    button = _item(view, "_ToggleButton")
    interaction = _interaction()
    await button.callback(interaction)
    assert calls == [("role", False)] and button.enabled is False
    interaction.response.edit_message.assert_awaited_once()


async def test_pick_channels_resolves_to_guild_channels_and_cancel_is_none():
    guild = FakeGuild(gid=1)
    real = SimpleNamespace(id=10, mention="<#10>", send=AsyncMock())
    guild.channels.append(real)
    ctx = FakeCtx(author=FakeMember(1), guild=guild)

    async def _click(cls_name, values=None):
        await asyncio.sleep(0.01)
        item = _item(ctx.sent_views[-1], cls_name)
        if values is not None:
            item._values = values
        await item.callback(_interaction())

    task = asyncio.create_task(_click("_ChannelSelect", [_channel(10)]))
    picked = await pick_channels(ctx, title="t", current_ids=[None], multi=False, typed_usage="u")
    await task
    assert picked == [real]

    task = asyncio.create_task(_click("_CancelButton"))
    assert await pick_channels(ctx, title="t", current_ids=[], multi=False, typed_usage="u") is None
    await task


async def test_pick_from_list_notes_when_options_are_truncated():
    ctx = FakeCtx(author=FakeMember(1))

    async def _cancel():
        await asyncio.sleep(0.01)
        await _item(ctx.sent_views[-1], "_CancelButton").callback(_interaction())
    task = asyncio.create_task(_cancel())
    await pick_from_list(ctx, title="t", description="d", options=[(str(i), str(i)) for i in range(30)])
    await task
    assert "first 25 of 30" in ctx.sent_embeds[0].description


# ── bare commands open a prompt ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(_settings_cog, "save_guild_settings", _noop)
    monkeypatch.setattr(_settings_cog, "save_bot_settings", _noop)


def _ctx(command: str, *, admin: bool = True) -> FakeCtx:
    ctx = FakeCtx(author=FakeMember(1, administrator=admin), guild=FakeGuild(gid=42), command_name=command)
    ctx.message.channel_mentions = []
    ctx.message.mentions = []
    return ctx


def _returns(monkeypatch, name: str, value):
    calls = []

    async def _stub(ctx, **kwargs):
        calls.append(kwargs)
        return value
    monkeypatch.setattr(_settings_cog, name, _stub)
    return calls


async def test_bare_channel_list_command_saves_the_pick_and_clear(monkeypatch):
    cog = SettingsCog(bot=None)
    get_guild_cfg(42)["game_channels"] = [5]
    calls = _returns(monkeypatch, "pick_channels", [_channel(10), _channel(11)])
    await cog.settings_channel_game.callback(cog, _ctx("settings-channel game"))
    assert get_guild_cfg(42)["game_channels"] == [10, 11]
    assert calls[0]["current_ids"] == [5] and calls[0]["multi"] is True

    _returns(monkeypatch, "pick_channels", [])
    await cog.settings_channel_game.callback(cog, _ctx("settings-channel game"))
    assert get_guild_cfg(42)["game_channels"] == []


async def test_bare_global_channel_command_stores_a_str_id(monkeypatch):
    cog = SettingsCog(bot=None)
    _state.bot_admins.add(1)
    _returns(monkeypatch, "pick_channels", [_channel(77)])
    await cog.settings_channel_admin_log.callback(cog, _ctx("settings-channel admin-log"))
    assert _state.bot_settings["admin_log_channel"] == "77"


async def test_bare_shop_opens_toggles_that_write_through(monkeypatch):
    cog = SettingsCog(bot=None)
    get_guild_cfg(42)["shop_items"] = {"role": False}

    async def _panel(ctx, *, items, on_toggle, **kwargs):
        assert items["role"] == ("role", False) and items["nickname"] == ("nickname", True)
        await on_toggle("nickname", False)
    monkeypatch.setattr(_settings_cog, "toggle_panel", _panel)

    await cog.settings_shop.callback(cog, _ctx("settings shop"))
    assert get_guild_cfg(42)["shop_items"] == {"role": False, "nickname": False}


async def test_bare_on_off_and_scope_commands_use_the_button_pick(monkeypatch):
    cog = SettingsCog(bot=None)
    _returns(monkeypatch, "confirm_choice", "on")
    await cog.settings_quote.callback(cog, _ctx("settings quote"))
    assert get_guild_cfg(42)["quote_bypass_restrictions"] is True

    _returns(monkeypatch, "confirm_choice", "server")
    await cog.settings_leaderboard.callback(cog, _ctx("settings leaderboard"))
    assert get_guild_cfg(42)["leaderboard_default_scope"] == "server"


async def test_nsfw_menu_reaches_the_unban_dropdown(monkeypatch):
    cog = SettingsCog(bot=None)
    get_guild_cfg(42)["nsfw_banned_tags"] = ["a", "b", "c"]
    _returns(monkeypatch, "confirm_choice", "unban")
    _returns(monkeypatch, "pick_from_list", ["a", "c"])
    await cog.settings_nsfw.callback(cog, _ctx("settings nsfw"))
    assert get_guild_cfg(42)["nsfw_banned_tags"] == ["b"]


async def test_alias_remove_dropdown_and_confirmed_clear(monkeypatch):
    cog = SettingsCog(bot=None)
    get_guild_cfg(42)["tax_aliases"] = {"rent": "💰", "toll": "💰", "fee": "💰"}
    _returns(monkeypatch, "pick_from_list", ["rent"])
    await cog.settings_tax_aliases.callback(cog, _ctx("settings tax-aliases"), "remove")
    assert set(get_guild_cfg(42)["tax_aliases"]) == {"toll", "fee"}

    _returns(monkeypatch, "confirm_prompt", False)
    await cog.settings_tax_aliases.callback(cog, _ctx("settings tax-aliases"), "clear")
    assert set(get_guild_cfg(42)["tax_aliases"]) == {"toll", "fee"}

    _returns(monkeypatch, "confirm_prompt", True)
    await cog.settings_tax_aliases.callback(cog, _ctx("settings tax-aliases"), "clear")
    assert get_guild_cfg(42)["tax_aliases"] == {}


async def test_soundboard_add_and_remove_pickers(monkeypatch):
    cog = SettingsCog(bot=None)
    _returns(monkeypatch, "pick_users", [FakeMember(7), FakeMember(8)])
    await cog.settings_soundboard_ratelimit.callback(cog, _ctx("settings soundboard-ratelimit"), "add")
    assert get_guild_cfg(42)["soundboard_ratelimit"] == [7, 8]

    _returns(monkeypatch, "pick_from_list", ["7"])
    await cog.settings_soundboard_ratelimit.callback(cog, _ctx("settings soundboard-ratelimit"), "remove")
    assert get_guild_cfg(42)["soundboard_ratelimit"] == [8]


async def test_menu_opens_the_panel_but_not_past_the_permission_check(monkeypatch):
    cog = SettingsCog(bot=None)
    _state.command_perms["settings"] = {"tier": "server_admin", "hidden": False}
    game_index = str([m for _, m, _ in _settings_cog._CHANNEL_PANELS].index("settings_channel_game"))
    _returns(monkeypatch, "pick_from_list", [game_index])
    _returns(monkeypatch, "pick_channels", [_channel(10)])

    ctx = _ctx("settings-channel")
    await cog._open_panel(ctx, _settings_cog._CHANNEL_PANELS)
    assert get_guild_cfg(42)["game_channels"] == [10]
    assert ctx.command is cog.settings_channel_game

    get_guild_cfg(42)["game_channels"] = []
    await cog._open_panel(_ctx("settings-channel", admin=False), _settings_cog._CHANNEL_PANELS)
    assert get_guild_cfg(42)["game_channels"] == []
