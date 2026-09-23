"""The `!admin` panel (src/admin_hub.py): tier-filtered pages, member
resolution for converter-typed commands, captured vs public replies."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.cogs.admin_cog as _admin_cog
from src.admin_hub import AdminHub, PAGES, items_for, pages_for
from src.cogs.admin_cog import AdminCog
from src.panel import _ItemSelect, _PageSelect
from src.settings_views import FormModal

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio

GID = 42


@pytest.fixture(autouse=True)
def _real_tiers():
    """The panel filters on the shipped tiers; conftest empties them."""
    import json
    from pathlib import Path
    _state.command_perms.update(json.loads(Path("src/command_perms.json").read_text(encoding="utf-8")))


def _ctx(*, admin: bool = True, bot_admin: bool = False, uid: int = 1) -> FakeCtx:
    ctx = FakeCtx(author=FakeMember(uid, administrator=admin), guild=FakeGuild(gid=GID), command_name="admin")
    ctx.guild.members.append(FakeMember(7, display_name="target"))
    if bot_admin:
        _state.bot_admins.add(uid)
    return ctx


def _interaction(uid: int = 1):
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: False)
    return SimpleNamespace(user=FakeMember(uid), response=response, edit_original_response=AsyncMock(),
                           followup=SimpleNamespace(send=AsyncMock()))


def _fake_bot(*names):
    commands = {}
    for name in names:
        cog = SimpleNamespace(calls=[])

        async def _cb(self, ctx, *args, _cog=cog, **kwargs):
            _cog.calls.append((args, kwargs))
            await ctx.send(embed=_admin_cog.emb(f"did {ctx.command.qualified_name}", "ok", 0))
        commands[name] = SimpleNamespace(qualified_name=name, name=name.split(" ")[-1], callback=_cb, cog=cog)
    return SimpleNamespace(get_command=lambda n: commands.get(n)), commands


def _pick(view, cls, value):
    select = next(i for i in view.children if isinstance(i, cls))
    select._values = [value]
    return select


async def test_pages_and_items_follow_the_invokers_tier():
    server_admin = _ctx()
    keys = {i.key for i in items_for("moderation", server_admin)}
    assert {"ban", "unban", "clear", "say"} <= keys and "globalban" not in keys
    assert items_for("permissions", server_admin) and not any(i.key == "setperm" for i in items_for("permissions", server_admin))
    assert "economy" not in dict(pages_for(server_admin))  # every economy action is bot-admin

    bot_admin = _ctx(bot_admin=True)
    assert [k for k, _ in pages_for(bot_admin)] == [k for k, _ in PAGES]
    assert {"globalban", "setperm", "admingive", "godmode"} <= {i.key for p in ("moderation", "permissions", "economy") for i in items_for(p, bot_admin)}


async def test_forms_resolve_members_and_build_the_typed_call():
    ctx = _ctx(bot_admin=True)
    items = {i.key: i for p in ("moderation", "effects", "permissions", "economy", "counters") for i in items_for(p, ctx)}
    target = ctx.guild.get_member(7)
    args, kwargs = items["ban"].call(ctx, {"user": [7], "reason": "spam"})
    assert args == (target,) and kwargs == {"reason": "spam"}
    assert items["effects-add"].call(ctx, {"user": [7], "effect": ["tax"], "duration": "2h"}) == (("<@7>", "add", "tax", "2h"), {})
    assert items["effects-remove"].call(ctx, {"user": [7], "effect": ["tax"]}) == (("<@7>", "remove", "tax"), {})
    assert items["setperm"].call(ctx, {"user": [7], "tier": ["bot_admin"]}) == ((target, "bot_admin"), {})
    assert items["counter-addperm"].call(ctx, {"user": [7]}) == ((), {"member": target})
    assert items["counter-add"].call(ctx, {"name": "afk", "description": "Away"}) == (("afk",), {"description": "Away"})
    assert items["event"].call(ctx, {"amount": "5k", "hours": ""}) == (("5k",), {}) and items["event"].public
    assert items["godmode"].call(ctx, {"user": []}) == ((), {})
    with pytest.raises(ValueError):
        items["unban"].call(ctx, {"user": [999]})  # not in the server


async def test_a_pick_forwards_with_the_gates_and_captures_the_reply():
    bot, commands = _fake_bot("clear", "say", "globalban")
    cog = AdminCog(bot=bot)
    ctx = _ctx()
    hub = AdminHub(cog, ctx, "moderation")

    interaction = _interaction()
    await _pick(hub, _ItemSelect, "clear").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, FormModal)
    modal._inputs["n"]._value = "5"
    submit = _interaction()
    await modal.on_submit(submit)
    assert commands["clear"].cog.calls == [(("5",), {})]
    assert ctx.sent_embeds == [] and hub.last.title == "did clear"  # captured into the panel
    submit.edit_original_response.assert_awaited_once()

    # `say` posts publicly: nothing captured, the reply lands in the channel.
    interaction = _interaction()
    await _pick(hub, _ItemSelect, "say").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["text"]._value = "hello"
    await modal.on_submit(_interaction())
    assert commands["say"].cog.calls == [((), {"text": "hello"})] and len(ctx.sent_embeds) == 1

    # A bot-admin command reached by a server admin is refused privately.
    hub.page = "moderation"
    from src.admin_hub import AdminItem
    sneaky = AdminItem(key="x", label="x", command="globalban")
    submit = _interaction()
    await hub.on_pick(submit, sneaky, None)
    submit.followup.send.assert_awaited_once()
    assert "can't use" in submit.followup.send.await_args.args[0] and commands["globalban"].cog.calls == []


async def test_page_dropdown_and_bare_admin_command_open_the_panel(monkeypatch):
    ctx = _ctx()
    hub = AdminHub(AdminCog(bot=None), ctx, "overview")
    assert not any(isinstance(i, _ItemSelect) for i in hub.children)
    await _pick(hub, _PageSelect, "moderation").callback(_interaction())
    assert hub.page == "moderation" and any(isinstance(i, _ItemSelect) for i in hub.children)

    opened = []

    async def _open(ctx_, panel, **kwargs):
        opened.append(panel)
    monkeypatch.setattr(_admin_cog, "open_panel", _open)
    _state.command_perms["admin"] = {"tier": "server_admin", "hidden": False}
    cog = AdminCog(bot=None)
    await cog.cmd_admin.callback(cog, _ctx())
    assert isinstance(opened[0], AdminHub)
