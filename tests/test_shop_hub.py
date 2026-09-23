"""The `!shop` panel (src/shop_hub.py): the catalog, the forms' typed
output, the gated forward, and the view."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.cogs.shop_cog as _shop_cog
from src.cogs.shop_cog import ShopCog
from src.features import set_feature
from src.guild_config import get_guild_cfg
from src.shop_hub import PAGES, ShopHub, _ItemSelect, _PageSelect, items_for, pages_for
from src.settings_views import FormModal, MAX_OPTIONS

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember, FakeRole

pytestmark = pytest.mark.asyncio

GID = 42


def _ctx(command: str = "shop", *, uid: int = 1) -> FakeCtx:
    ctx = FakeCtx(author=FakeMember(uid), guild=FakeGuild(gid=GID), command_name=command)
    ctx.message.channel_mentions = []
    ctx.message.mentions = []
    ctx.invoked_with = command
    return ctx


def _interaction(uid: int = 1):
    response = SimpleNamespace(
        send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(), is_done=lambda: False,
    )
    return SimpleNamespace(user=FakeMember(uid), response=response, edit_original_response=AsyncMock(),
                           followup=SimpleNamespace(send=AsyncMock()))


def _fake_bot(*names):
    """A bot whose get_command knows `names`, each recording its calls."""
    commands = {}
    for name in names:
        cog = SimpleNamespace(calls=[])

        async def _cb(self, ctx, *args, _cog=cog, **kwargs):
            _cog.calls.append((args, kwargs))
        commands[name] = SimpleNamespace(qualified_name=name, name=name.split(" ")[-1], callback=_cb, cog=cog)
    # add/remove_command satisfy ShopCog.__init__, which registers its top-level aliases.
    bot = SimpleNamespace(get_command=lambda name: commands.get(name), add_command=lambda c: None, remove_command=lambda n: None)
    return bot, commands


def _stock(ctx):
    """A server with something on every page."""
    ctx.guild.roles.append(FakeRole(5, "VIP"))
    _state.bot_roles.add(5)
    ctx.guild.channels.append(SimpleNamespace(id=10, name="lounge", mention="<#10>"))
    cfg = get_guild_cfg(GID)
    cfg["bot_channels"] = [10]
    cfg["bounty_channel"] = 99
    cfg["tax_aliases"] = {"rent": "🏠"}
    _state.property_owners["lemonade_stand"] = {"owner_id": ctx.author.id, "acquired_at": 0, "list_price": 500,
                                                "listed_at": 0, "upgraded": False, "custom_name": None}
    _state.insurance_subs[ctx.author.id] = "basic"


def _overview(ctx):
    return ShopCog(bot=None)._overview_embed(ctx)


def _pick(view, cls, value):
    select = next(i for i in view.children if isinstance(i, cls))
    select._values = [value]
    return select


# ── catalog ──────────────────────────────────────────────────────────────────

async def test_every_item_names_a_real_command_and_fits_a_select():
    ctx = _ctx()
    _stock(ctx)
    bot, _ = _fake_bot("assets buy", "assets sell", "assets upgrade", "assets unlist", "assets rename", "bounties")
    cog = ShopCog(bot=None)
    assert [k for k, _ in pages_for(ctx)] == [k for k, _ in PAGES]
    seen = set()
    for key, _ in PAGES:
        items = items_for(key, ctx)
        assert len(items) <= MAX_OPTIONS, key
        for item in items:
            assert item.key not in seen, item.key
            seen.add(item.key)
            assert len(item.label) <= 100, item.label
            if item.method.startswith("bot:"):
                assert bot.get_command(item.method[4:]) is not None, item.method
            else:
                assert getattr(cog, item.method, None) is not None, item.method
            if item.is_form:
                assert item.call is not None and item.title, item.key
            else:
                assert item.call is None and not item.fields, item.key


async def test_pages_hide_what_the_server_or_the_invoker_lacks():
    ctx = _ctx()
    keys = dict(pages_for(ctx))
    assert "bounties" not in keys and "portfolio" not in keys and "artifacts" in keys
    set_feature(GID, "assets", False)
    set_feature(GID, "artifacts", False)
    get_guild_cfg(GID)["shop_items"] = {k: False for k in ("createrole", "assignrole", "unassignrole", "deleterole",
                                                            "roleup", "roledown", "rolecolor", "renamerole", "lockrole")}
    keys = dict(pages_for(ctx))
    assert not {"assets_low", "assets_high", "artifacts", "roles"} & set(keys)
    assert "channels" in keys and "fun" in keys


async def test_assets_pages_show_price_status_and_the_owners_deeds():
    ctx = _ctx()
    _stock(ctx)
    low = {i.key: i for i in items_for("assets_low", ctx)}
    assert low["asset:lemonade_stand"].description.startswith("✅ yours")
    assert low["asset:hot_dog_cart"].kwargs == {"name": "hot_dog_cart"} and "12,000 🪙" in low["asset:hot_dog_cart"].label
    assert "🔒 global level" in low["asset:hot_dog_cart"].description  # a level-1 user
    high = items_for("assets_high", ctx)
    assert high and all(i.method == "bot:assets buy" for i in high)
    mine = {i.key: i for i in items_for("portfolio", ctx)}
    assert set(mine) == {"sell:lemonade_stand", "upgrade:lemonade_stand", "unlist:lemonade_stand", "rename:lemonade_stand"}
    assert mine["sell:lemonade_stand"].call({"price": "50k"}) == (("lemonade_stand", "50k"), {})
    assert mine["rename:lemonade_stand"].call({"name": "Sour Power"}) == (("lemonade_stand", "Sour", "Power"), {})


async def test_forms_produce_the_typed_argument_lists():
    ctx = _ctx()
    _stock(ctx)
    items = {}
    for page in ("nicknames", "roles", "channels", "fun", "insurance", "leveling", "bounties"):
        items.update({i.key: i for i in items_for(page, ctx)})
    assert items["nickname"].call({"user": [7], "name": "Boss"}) == (("<@7>", "Boss"), {})
    assert items["nickname"].call({"user": [], "name": "Boss"}) == (("Boss",), {})
    assert items["rolerename"].call({"role": ["5"], "name": "New Name"}) == (("<@&5>", "|", "New Name"), {})
    assert items["roleunassign"].call({"user": [], "role": ["5"]}) == (("<@&5>",), {})
    assert items["roledown"].invoked_with == "roledown" and items["roledown"].method == "shop_roleup"
    assert items["rolechannel"].call({"role": ["5"], "channel": ["10"]}) == (("<@&5>", "<#10>"), {})
    assert items["channelrename"].call({"channel": ["10"], "name": "new lounge"}) == (("<#10>", "new lounge"), {})
    assert items["ragebait"].call({"user": [7], "topic": ""}) == (("<@7>",), {})
    assert items["tax:rent"].invoked_with == "rent" and items["tax:rent"].method == "shop_tax"
    assert items["insurance"].call({"tier": ["standard"], "days": "3"}) == (("standard", "3"), {})
    assert items["insurance-sub"].call({"tier": ["premium"]}) == (("premium", "sub"), {})
    assert items["insurance-unsub"].args == ("unsub",)
    assert items["buyxp-to"].call({"level": "12"}) == (("lvl", "12"), {})
    assert items["bounty"].call({"coins": "5k", "duration": "", "condition": "do a thing"}) == (("5k", "do", "a", "thing"), {})
    # The bot's roles and channels come as a dropdown while they fit; the
    # command still receives a mention it can resolve.
    assert items["roledelete"].fields[0].kind == "choices" and items["roledelete"].fields[0].options == (("VIP", "5"),)


# ── forward ──────────────────────────────────────────────────────────────────

async def test_forward_runs_the_typed_command_with_invoked_with_set():
    cog = ShopCog(bot=None)
    ran = []

    async def _cb(self, ctx, *args):
        ran.append((ctx.invoked_with, args))
    # A tax alias: not level-gated, and the command reads invoked_with for the alias word.
    cog.shop_tax = SimpleNamespace(callback=_cb, cog=None, name="tax", qualified_name="shop tax")
    ctx = _ctx()
    assert await cog.forward(ctx, "shop_tax", "<@7>", invoked_with="rent") is None
    assert ran == [("rent", ("<@7>",))] and ctx.command is cog.shop_tax


async def test_forward_applies_the_feature_and_level_gates_a_direct_call_would_skip():
    bot, commands = _fake_bot("assets buy")
    cog = ShopCog(bot=bot)
    ctx = _ctx()
    set_feature(GID, "assets", False)
    refusal = await cog.forward(ctx, "bot:assets buy", name="lemonade_stand")
    assert "🏠 Assets" in refusal and commands["assets buy"].cog.calls == []
    set_feature(GID, "assets", True)
    assert await cog.forward(ctx, "bot:assets buy", name="lemonade_stand") is None
    assert commands["assets buy"].cog.calls == [((), {"name": "lemonade_stand"})]

    called = []

    async def _cb(self, ctx, *args):
        called.append(args)
    cog.shop_unoreverse = SimpleNamespace(callback=_cb, cog=None, name="unoreverse", qualified_name="shop unoreverse")
    refusal = await cog.forward(ctx, "shop_unoreverse", "<@7>")  # level 18, and the invoker is level 1
    assert "🔒" in refusal and called == []
    assert "isn't available" in await cog.forward(ctx, "bot:nothing")


# ── the view ─────────────────────────────────────────────────────────────────

async def test_a_buy_pick_forwards_and_refreshes_and_a_locked_pick_is_refused():
    ctx = _ctx()
    cog = ShopCog(bot=None)
    seen = []

    async def _forward(ctx_, method, *args, invoked_with=None, **kwargs):
        seen.append((method, args, kwargs))
        return None
    cog.forward = _forward
    hub = ShopHub(cog, ctx, "artifacts", _overview)
    interaction = _interaction()
    await _pick(hub, _ItemSelect, "artifact:1").callback(interaction)
    assert seen == [("shop_artifacts", ("buy", "1"), {})]
    interaction.response.defer.assert_awaited_once()
    assert interaction.edit_original_response.await_args.kwargs["view"] is hub

    hub.page = "fun"
    hub._build()
    locked = _interaction()
    await _pick(hub, _ItemSelect, "unoreverse").callback(locked)
    locked.response.send_message.assert_awaited_once()
    assert len(seen) == 1


async def test_a_form_pick_opens_a_modal_whose_submit_forwards_and_shows_a_refusal_privately():
    ctx = _ctx()
    cog = ShopCog(bot=None)

    async def _forward(ctx_, method, *args, invoked_with=None, **kwargs):
        return "🚫 nope"
    cog.forward = _forward
    hub = ShopHub(cog, ctx, "fun", _overview)
    interaction = _interaction()
    await _pick(hub, _ItemSelect, "mock").callback(interaction)
    interaction.response.defer.assert_not_awaited()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, FormModal) and modal.fields[0].kind == "users"
    modal._inputs["user"]._values = [SimpleNamespace(id=7)]
    submit = _interaction()
    await modal.on_submit(submit)
    submit.followup.send.assert_awaited_once_with("🚫 nope", ephemeral=True)
    submit.edit_original_response.assert_awaited_once()


async def test_the_page_dropdown_switches_pages_and_the_overview_has_no_picks():
    ctx = _ctx()
    hub = ShopHub(ShopCog(bot=None), ctx, "overview", _overview)
    assert not any(isinstance(i, _ItemSelect) for i in hub.children)
    assert hub.embed().title == "🛒 Shop"
    await _pick(hub, _PageSelect, "insurance").callback(_interaction())
    assert hub.page == "insurance" and "Insurance" in hub.embed().title
    assert any(isinstance(i, _ItemSelect) for i in hub.children)


async def test_bare_shop_and_shop_assets_open_the_panel(monkeypatch):
    opened = []

    async def _hub(ctx, cog, **kwargs):
        opened.append(kwargs)
    monkeypatch.setattr(_shop_cog, "open_shop_hub", _hub)
    cog = ShopCog(bot=None)
    ctx = _ctx()
    ctx.subcommand_passed = None
    await cog.cmd_shop.callback(cog, ctx)
    assert "page" not in opened[0] and opened[0]["overview"](ctx).title == "🛒 Shop"
    await cog.shop_assets.callback(cog, _ctx())
    assert opened[1]["page"] == "assets_low"
    await cog.shop_roles.callback(cog, _ctx())
    assert opened[2]["page"] == "roles"


async def test_shop_assets_forwards_typed_subcommands():
    bot, commands = _fake_bot("assets buy", "assets sell")
    cog = ShopCog(bot=bot)
    assets = SimpleNamespace(get_command=lambda name: commands.get(f"assets {name}"))
    bot.get_command = lambda name: assets if name == "assets" else commands.get(name)
    await cog.shop_assets.callback(cog, _ctx(), "buy", "hot", "dog", "cart")
    assert commands["assets buy"].cog.calls == [((), {"name": "hot dog cart"})]
    await cog.shop_assets.callback(cog, _ctx(), "sell", "hot_dog_cart", "50k")
    assert commands["assets sell"].cog.calls == [(("hot_dog_cart", "50k"), {})]
