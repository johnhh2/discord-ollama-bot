"""The `!settings` panel (src/settings_hub.py): the catalog, the forwarding
of picks into typed commands, the captured replies, and the form modals."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.cogs.settings_cog as _settings_cog
import src.settings_hub as _hub
from src.cogs.settings_cog import SettingsCog
from src.features import set_feature
from src.guild_config import get_guild_cfg
from src.settings_hub import (
    CATEGORIES, CHANNEL_SETTINGS, GLOBAL_CHANNEL_SETTINGS, SettingsHub, _CategorySelect, _ItemSelect,
    channels_in, items_for,
)
from src.settings_views import Field, FormModal, MAX_OPTIONS, _ListEditorView

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio

GID = 42


@pytest.fixture(autouse=True)
def _no_io(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(_settings_cog, "save_guild_settings", _noop)
    monkeypatch.setattr(_settings_cog, "save_bot_settings", _noop)
    monkeypatch.setattr(_settings_cog, "save_channel_prompts", _noop)


def _ctx(command: str = "settings", *, admin: bool = True, bot_admin: bool = False) -> FakeCtx:
    ctx = FakeCtx(author=FakeMember(1, administrator=admin), guild=FakeGuild(gid=GID), command_name=command)
    ctx.message.channel_mentions = []
    ctx.message.mentions = []
    ctx.guild.channels.extend(_channel(c) for c in (10, 11, 12))
    if bot_admin:
        _state.bot_admins.add(1)
    return ctx


def _channel(cid: int):
    return SimpleNamespace(id=cid, mention=f"<#{cid}>")


def _interaction(uid: int = 1):
    response = SimpleNamespace(
        send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock(),
        is_done=lambda: False,
    )
    return SimpleNamespace(user=FakeMember(uid), response=response, edit_original_response=AsyncMock())


def _overview(ctx):
    return SettingsCog(bot=None)._overview_embed(ctx)


def _hub_for(ctx, category: str) -> SettingsHub:
    return SettingsHub(SettingsCog(bot=None), ctx, category, _overview)


def _pick(view, cls, value):
    select = next(i for i in view.children if isinstance(i, cls))
    select._values = [value]
    return select


# ── catalog ──────────────────────────────────────────────────────────────────

async def test_every_item_names_a_real_command_and_fits_a_select():
    cog = SettingsCog(bot=None)
    ctx = _ctx(bot_admin=True)
    get_guild_cfg(GID)["nsfw_banned_tags"] = ["a"]
    get_guild_cfg(GID)["tax_aliases"] = {"rent": "💰"}
    get_guild_cfg(GID)["soundboard_ratelimit"] = [7]
    seen = set()
    for key, _ in CATEGORIES:
        items = items_for(key, ctx, models=["m1"])
        assert len(items) <= MAX_OPTIONS, key
        for item in items:
            assert item.key not in seen, item.key
            seen.add(item.key)
            assert len(item.label) <= 100, item.label
            assert item.method.startswith("bot:") or getattr(cog, item.method, None) is not None, item.method
            if item.is_form:
                assert item.to_args is not None and item.title, item.key
            else:
                assert item.to_args is None
    assert {row[1] for row in CHANNEL_SETTINGS + GLOBAL_CHANNEL_SETTINGS} <= {
        i.key.split(":", 1)[1] for i in items_for("channels", ctx)
    }


async def test_non_bot_admins_see_no_global_channels_and_no_ai_settings():
    ctx = _ctx()
    keys = {i.key for i in items_for("channels", ctx)}
    assert "channel:admin_log_channel" not in keys and "channel:lottery_channel" in keys
    assert items_for("ai", ctx) == []
    assert [i.key for i in items_for("ai", _ctx(bot_admin=True))][:3] == ["model:ask_model", "model:roleplay_model", "model:coding_model"]


async def test_feature_items_show_the_stored_switch_and_flip_it():
    set_feature(GID, "economy", False)
    items = {i.key: i for i in items_for("features", _ctx())}
    assert items["feature:economy"].args == ("economy", "on")
    assert items["feature:gambling"].args == ("gambling", "off")
    assert "waiting on 💰 Economy" in items["feature:gambling"].value


# ── the view ─────────────────────────────────────────────────────────────────

async def test_a_toggle_pick_forwards_the_typed_form_and_shows_the_reply_in_the_panel():
    ctx = _ctx()
    hub = _hub_for(ctx, "shop")
    interaction = _interaction()
    await _pick(hub, _ItemSelect, "shop:role").callback(interaction)
    assert get_guild_cfg(GID)["shop_items"]["role"] is False
    assert ctx.sent_embeds == []  # captured, not posted
    interaction.response.defer.assert_awaited_once()
    edit = interaction.edit_original_response.await_args.kwargs
    assert edit["view"] is hub and "Last change" in [f.name for f in edit["embed"].fields]
    assert "role" in edit["embed"].fields[-1].value and "disabled" in edit["embed"].fields[-1].value
    # The rebuilt dropdown reads the new state: the next pick turns it back on.
    assert next(i for i in hub.items() if i.key == "shop:role").args == ("role", "on")


async def test_a_form_pick_opens_a_modal_whose_submit_forwards_the_channels():
    ctx = _ctx()
    hub = _hub_for(ctx, "channels")
    interaction = _interaction()
    await _pick(hub, _ItemSelect, "channel:levelup_channel").callback(interaction)
    interaction.response.defer.assert_not_awaited()  # a modal must be the first reply
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, FormModal) and modal.fields[0].kind == "channels"

    modal._inputs["channels"]._values = [_channel(11)]
    submit = _interaction()
    await modal.on_submit(submit)
    assert get_guild_cfg(GID)["levelup_channel"] == 11
    submit.edit_original_response.assert_awaited_once()

    # An emptied picker clears the setting.
    interaction2 = _interaction()
    await _pick(hub, _ItemSelect, "channel:levelup_channel").callback(interaction2)
    modal = interaction2.response.send_modal.await_args.args[0]
    modal._inputs["channels"]._values = []
    await modal.on_submit(_interaction())
    assert get_guild_cfg(GID)["levelup_channel"] is None


async def test_the_category_dropdown_switches_pages_and_fetches_models_once(monkeypatch):
    calls = []

    async def _models():
        calls.append(1)
        return ["llama3:8b"]
    monkeypatch.setattr(_hub, "list_ollama_models", _models)
    hub = _hub_for(_ctx(bot_admin=True), "overview")
    assert not any(isinstance(i, _ItemSelect) for i in hub.children)  # the overview has no picks
    for _ in range(2):
        await _pick(hub, _CategorySelect, "ai").callback(_interaction())
    assert hub.category == "ai" and calls == [1]
    model_item = next(i for i in hub.items() if i.key == "model:ask_model")
    assert model_item.fields[0].kind == "choices"
    assert model_item.to_args({"model": ["llama3:8b"]}) == ("llama3:8b",)


async def test_only_the_invoker_may_use_the_panel_and_close_stops_it():
    hub = _hub_for(_ctx(), "features")
    intruder = _interaction(uid=2)
    assert await hub.interaction_check(intruder) is False
    close = next(i for i in hub.children if getattr(i, "label", None) == "Close")
    await close.callback(_interaction())
    assert hub.is_finished()


async def test_open_settings_hub_deletes_the_panel_when_it_closes(monkeypatch):
    ctx = _ctx()
    sent = []

    async def _send(content=None, *, embed=None, view=None, **kwargs):
        msg = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
        sent.append((kwargs, view, msg))
        view.stop()
        return msg
    ctx.send = _send
    await _hub.open_settings_hub(ctx, SettingsCog(bot=None), overview=_overview, ephemeral=True)
    kwargs, view, msg = sent[0]
    assert kwargs.get("ephemeral") is True and view.category == "overview"
    msg.delete.assert_awaited_once()


# ── forwarding helpers ───────────────────────────────────────────────────────

async def test_channels_in_reads_mentions_then_ids_and_skips_unknowns():
    ctx = _ctx()
    ctx.message.channel_mentions = [_channel(10)]
    found = channels_in(ctx, ("<#11>", "<#10>", "999999999999999999", "<#12>", "nonsense"))
    assert [c.id for c in found] == [10, 11, 12]


async def test_run_captured_reaches_another_cogs_command_and_reports_a_missing_one():
    class _Cog:
        pass

    async def _ai_off(self, ctx):
        ctx.hit = True
        await ctx.send(embed=_settings_cog.emb("🤖 AI Disabled", "off", 0))
    command = SimpleNamespace(callback=_ai_off, cog=_Cog(), qualified_name="ai off")
    bot = SimpleNamespace(get_command=lambda name: command if name == "ai off" else None)
    cog = SettingsCog(bot=bot)
    ctx = _ctx()
    _state.command_perms["ai off"] = {"tier": "bot_admin", "hidden": False}
    _state.bot_admins.add(1)
    reply = await cog.run_captured(ctx, "bot:ai off")
    assert reply.title == "🤖 AI Disabled" and ctx.hit is True and ctx.sent_embeds == []
    assert (await cog.run_captured(ctx, "bot:nothing")).title == "❌"


# ── the prompts ──────────────────────────────────────────────────────────────

async def test_form_modal_collects_text_choices_and_ids():
    modal = FormModal("t", [
        Field("word", "Word"), Field("kind", "Kind", kind="choices", options=(("A", "a"), ("B", "b")), max_values=2),
        Field("who", "Who", kind="users", required=False, max_values=5),
    ])
    modal._inputs["word"]._value = "  rent "
    modal._inputs["kind"]._values = ["a", "b"]
    modal._inputs["who"]._values = [SimpleNamespace(id=7)]
    interaction = _interaction()
    await modal.on_submit(interaction)
    assert modal.result == {"word": "rent", "kind": ["a", "b"], "who": [7]}
    interaction.response.defer.assert_awaited_once()


async def test_list_editor_remove_needs_a_pick_and_add_finishes_from_the_modal():
    view = _ListEditorView(1, [("!a", "a"), ("!b", "b")], "Add", [Field("word", "Word")], remove_label="Remove", clear=True, timeout=5)
    labels = [getattr(i, "label", None) for i in view.children]
    assert labels[1:] == ["Add…", "Remove", "Clear all", "Done"]
    remove = view.children[2]
    nag = _interaction()
    await remove.callback(nag)
    nag.response.send_message.assert_awaited_once() and not view.is_finished()

    select = view.children[0]
    select._values = ["b"]
    await select.callback(_interaction())
    await remove.callback(_interaction())
    assert view.result == ("remove", ["b"]) and view.is_finished()

    view = _ListEditorView(1, [], "Add", [Field("word", "Word")], remove_label="Remove", clear=True, timeout=5)
    add = view.children[0]
    interaction = _interaction()
    await add.callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["word"]._value = "rent"
    await modal.on_submit(_interaction())
    assert view.result == ("add", {"word": "rent"}) and view.is_finished()
