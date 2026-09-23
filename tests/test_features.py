"""Per-guild feature toggles (src/features.py): the catalog, the global
gate, the setup wizard, the settings commands and the menus and passive
handlers that honour a switch."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.cogs.settings_cog as _settings_cog
import src.cogs.fun_cog as _fun_cog
from src.cogs.settings_cog import SettingsCog
from src.cogs.utility_cog import UtilityCog
from src.features import (
    COMMAND_FEATURES, FEATURES, command_features, disabled_feature_for,
    feature_enabled, features_overview, set_feature,
)
from src.guild_config import get_guild_cfg
from src.setup_wizard import _AdminAnswer, run_setup_wizard

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember, FakeMessage

pytestmark = pytest.mark.asyncio

GID = 42


@pytest.fixture(autouse=True)
def _no_io(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(_settings_cog, "save_guild_settings", _noop)
    import src.helpers as _helpers
    monkeypatch.setattr(_helpers, "add_ephemeral_msg", _noop, raising=False)


def _admin_ctx(command: str = "settings", *, admin: bool = True) -> FakeCtx:
    ctx = FakeCtx(author=FakeMember(1, administrator=admin), guild=FakeGuild(gid=GID), command_name=command)
    ctx.message.channel_mentions = []
    ctx.message.mentions = []
    return ctx


def _channel(cid: int):
    return SimpleNamespace(id=cid, mention=f"<#{cid}>")


# ── catalog ──────────────────────────────────────────────────────────────────

async def test_everything_is_on_until_a_switch_is_written():
    assert all(feature_enabled(GID, key) for key in FEATURES)
    assert feature_enabled(None, "economy")  # DMs have nothing to configure
    assert "features" not in get_guild_cfg(GID)


async def test_a_child_inherits_its_parent_off_switch_but_keeps_its_own_value():
    set_feature(GID, "economy", False)
    assert feature_enabled(GID, "gambling") is False
    assert feature_enabled(GID, "artifacts") is False
    assert get_guild_cfg(GID)["features"] == {"economy": False}
    set_feature(GID, "economy", True)
    assert feature_enabled(GID, "gambling") is True
    set_feature(GID, "shop", False)
    assert feature_enabled(GID, "artifacts") is False and feature_enabled(GID, "gambling") is True


async def test_command_lookup_walks_prefixes_like_the_permission_gate():
    assert command_features("shop artifacts") == ("artifacts",)
    assert command_features("shop nickname") == ("shop",)
    assert command_features("shop bounty") == ("economy",)
    assert command_features("graph savings") == ("savings",)
    assert command_features("daily property") == ("assets",)
    assert command_features("chess") == () and command_features("idle shop") == ()


async def test_disabled_feature_names_the_outermost_switch():
    set_feature(GID, "economy", False)
    assert disabled_feature_for("slots", GID).key == "economy"
    set_feature(GID, "economy", True)
    set_feature(GID, "gambling", False)
    assert disabled_feature_for("slots", GID).key == "gambling"
    assert disabled_feature_for("chess", GID) is None
    assert disabled_feature_for("slots", None) is None
    set_feature(GID, "ai", False)
    assert disabled_feature_for("adminragebait", GID).key == "ai"


async def test_every_mapped_command_names_a_real_feature():
    for needs in COMMAND_FEATURES.values():
        assert all(key in FEATURES for key in needs)
    assert "💰 Economy ✅" in features_overview(GID)
    set_feature(GID, "ai", False)
    assert "🤖 AI ❌" in features_overview(GID)


# ── the gate ─────────────────────────────────────────────────────────────────

async def test_global_gate_refuses_a_switched_off_command_and_lets_others_through():
    from src.core import create_bot
    from src.features import FeatureDisabled
    gate = next(check for check in create_bot()._checks if check.__name__ == "_feature_gate")
    set_feature(GID, "gambling", False)

    assert await gate(_admin_ctx("chess")) is True
    assert await gate(FakeCtx(author=FakeMember(1), guild=None, command_name="slots")) is True

    ctx = _admin_ctx("slots")
    with pytest.raises(FeatureDisabled):
        await gate(ctx)
    assert "🎲 Gambling" in ctx.sent_embeds[0].description and "`!slots`" in ctx.sent_embeds[0].description


# ── the wizard ───────────────────────────────────────────────────────────────

class _WizardChannel:
    def __init__(self):
        self.embeds, self.views = [], []

    async def send(self, content=None, *, embed=None, view=None, **kwargs):
        self.embeds.append(embed)
        self.views.append(view)
        return FakeMessage()


def _interaction(uid: int = 1, admin: bool = True):
    response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock())
    return SimpleNamespace(user=FakeMember(uid, administrator=admin), response=response)


async def _click_through(channel: _WizardChannel, answers: list[bool]):
    """Press Enable/Disable on each question as it appears, in order."""
    done = set()
    for _ in range(500):
        await asyncio.sleep(0)
        if not answers:
            return
        view = channel.views[-1] if channel.views else None
        if view is None or id(view) in done or view.is_finished():
            continue
        done.add(id(view))
        want = answers.pop(0)
        button = next(b for b in view.children if b.label == ("Enable" if want else "Disable"))
        await button.callback(_interaction())
    raise AssertionError(f"unanswered: {answers}")


def _titles(channel: _WizardChannel) -> list[str]:
    return [e.title for e in channel.embeds if e is not None]


async def test_wizard_asks_the_extensions_only_when_the_economy_is_on():
    channel = _WizardChannel()
    guild = SimpleNamespace(id=GID)
    clicker = asyncio.create_task(_click_through(channel, [True, False, True, False, True, False, True]))
    assert await run_setup_wizard(guild, channel) is True
    await clicker

    asked = [t for t in _titles(channel) if t.startswith("Setup ")]
    assert [t.split("· ")[1] for t in asked] == [
        "💰 Economy", "🎲 Gambling", "🐷 Savings", "🏠 Assets", "🛒 Shop", "🏺 Artifacts", "🤖 AI",
    ]
    assert get_guild_cfg(GID)["features"] == {
        "economy": True, "gambling": False, "savings": True, "assets": False,
        "shop": True, "artifacts": False, "ai": True,
    }
    shop_note = next(e for e in channel.embeds if e is not None and e.title == "🛒 Shop items")
    assert "`!settings shop`" in shop_note.description
    assert _titles(channel)[-1] == "✅ Setup complete"
    assert "🎲 Gambling ❌" in channel.embeds[-1].description


async def test_wizard_skips_the_economy_extensions_when_the_economy_is_off():
    channel = _WizardChannel()
    clicker = asyncio.create_task(_click_through(channel, [False, True]))
    assert await run_setup_wizard(SimpleNamespace(id=GID), channel) is True
    await clicker

    asked = [t for t in _titles(channel) if t.startswith("Setup ")]
    assert [t.split("· ")[1] for t in asked] == ["💰 Economy", "🤖 AI"]
    assert get_guild_cfg(GID)["features"] == {"economy": False, "ai": True}
    # Shop and artifacts are off with the economy, and the items hint still shows.
    shop_note = next(e for e in channel.embeds if e is not None and e.title == "🛒 Shop items")
    assert "off with the economy" in shop_note.description and "`!settings shop`" in shop_note.description
    assert feature_enabled(GID, "shop") is False


async def test_wizard_timeout_keeps_what_was_answered_and_switches_nothing_off():
    channel = _WizardChannel()
    clicker = asyncio.create_task(_click_through(channel, [True]))
    assert await run_setup_wizard(SimpleNamespace(id=GID), channel, timeout=0.05) is False
    await clicker
    assert get_guild_cfg(GID)["features"] == {"economy": True}
    assert _titles(channel)[-1] == "⌛ Setup paused"
    assert all(feature_enabled(GID, key) for key in FEATURES)


async def test_only_an_admin_may_answer_a_wizard_question():
    view = _AdminAnswer(GID, current=True, timeout=5)
    nobody = _interaction(uid=7, admin=False)
    assert await view.interaction_check(nobody) is False
    nobody.response.send_message.assert_awaited_once()
    assert await view.interaction_check(_interaction(uid=8, admin=True)) is True
    _state.bot_admins.add(9)
    assert await view.interaction_check(_interaction(uid=9, admin=False)) is True
    _state.user_perm_overrides[(GID, 10)] = "server_admin"
    assert await view.interaction_check(_interaction(uid=10, admin=False)) is True


async def test_guild_join_greets_then_starts_the_wizard(monkeypatch):
    from src.events import EventsCog
    import src.setup_wizard as wizard
    started = []

    async def _start(guild, channel):
        started.append((guild.id, channel))
    monkeypatch.setattr(wizard, "start_setup_after_join", _start)
    channel = _WizardChannel()
    channel.permissions_for = lambda member: SimpleNamespace(send_messages=True)
    guild = SimpleNamespace(id=GID, name="g", system_channel=channel, me=object(), text_channels=[channel])

    await EventsCog.on_guild_join(SimpleNamespace(bot=None), guild)
    assert channel.embeds[0].title == "👋 Hello!" and "`!settings setup`" in channel.embeds[0].description
    assert started == [(GID, channel)]


# ── settings commands ────────────────────────────────────────────────────────

async def test_settings_features_typed_and_bare_forms():
    cog = SettingsCog(bot=None)
    await cog.settings_features.callback(cog, _admin_ctx("settings features"), "gambling", "off")
    assert get_guild_cfg(GID)["features"] == {"gambling": False}

    ctx = _admin_ctx("settings features")
    await cog.settings_features.callback(cog, ctx, "artifacts", "on")
    assert "🏺 Artifacts" in ctx.sent_embeds[-1].description
    set_feature(GID, "shop", False)
    ctx = _admin_ctx("settings features")
    await cog.settings_features.callback(cog, ctx, "artifacts", "on")
    assert "🛒 Shop** is on too" in ctx.sent_embeds[-1].description

    ctx = _admin_ctx("settings features")
    await cog.settings_features.callback(cog, ctx, "nonsense", "on")
    assert "Usage" in ctx.sent_embeds[-1].description


async def test_bare_settings_features_opens_toggles_showing_the_stored_switch(monkeypatch):
    cog = SettingsCog(bot=None)
    set_feature(GID, "economy", False)
    seen = {}

    async def _panel(ctx, *, items, on_toggle, **kwargs):
        seen.update(items)
        await on_toggle("ai", False)
    monkeypatch.setattr(_settings_cog, "toggle_panel", _panel)
    await cog.settings_features.callback(cog, _admin_ctx("settings features"))
    # The child button shows its own (on) value, not the inherited off, and says what it needs.
    assert seen["economy"] == ("💰 Economy", False)
    assert seen["gambling"] == ("🎲 Gambling · needs 💰 Economy", True)
    assert get_guild_cfg(GID)["features"] == {"economy": False, "ai": False}


async def test_settings_opens_the_panel_on_an_overview_of_features_and_channels(monkeypatch):
    cog = SettingsCog(bot=None)
    get_guild_cfg(GID)["lottery_channel"] = 55
    set_feature(GID, "savings", False)
    opened = []

    async def _hub(ctx, cog_, **kwargs):
        opened.append(kwargs)
    monkeypatch.setattr(_settings_cog, "open_settings_hub", _hub)

    ctx = _admin_ctx("settings")
    await cog.cmd_settings.callback(cog, ctx)
    assert opened and "category" not in opened[0]  # the panel starts on the overview
    fields = {f.name: f.value for f in opened[0]["overview"](ctx).fields}
    assert "🐷 Savings ❌" in fields["🧩 Features"]
    assert "🎰 Lottery: <#55>" in fields["📁 Channels"] and "🪙 Dailies: ❌ off" in fields["📁 Channels"]

    await cog.cmd_settings_channel.callback(cog, _admin_ctx("settings channel"))
    assert opened[1]["category"] == "channels"


async def test_channel_settings_live_under_settings_channel_and_the_old_spellings_forward():
    cog = SettingsCog(bot=None)
    assert cog.cmd_settings_channel.get_command("ai") is cog.settings_channel_ai
    assert cog.cmd_settings_channel.get_command("bounty") is cog.settings_bounty_channel

    ctx = _admin_ctx("settings-channel")
    ctx.message.channel_mentions = [_channel(10)]
    await cog.cmd_settings_channel_legacy.callback(cog, ctx, "ai")
    assert get_guild_cfg(GID)["ai_channels"] == [10] and ctx.command is cog.settings_channel_ai

    ctx = _admin_ctx("settings bounty-channel")
    ctx.message.channel_mentions = [_channel(11)]
    await cog.settings_bounty_channel_legacy.callback(cog, ctx)
    assert get_guild_cfg(GID)["bounty_channel"] == 11

    ctx = _admin_ctx("settings-channel")
    await cog.cmd_settings_channel_legacy.callback(cog, ctx, "nonsense")
    assert "No channel setting" in ctx.sent_embeds[-1].description


async def test_forwarded_hidden_bot_admin_channel_setting_stays_gated():
    _state.command_perms["settings channel admin-log"] = {"tier": "bot_admin", "hidden": True}
    _state.bot_settings.pop("admin_log_channel", None)  # not reset between tests
    cog = SettingsCog(bot=None)
    ctx = _admin_ctx("settings-channel")
    ctx.message.channel_mentions = [_channel(12)]
    await cog.cmd_settings_channel_legacy.callback(cog, ctx, "admin-log")
    assert "admin_log_channel" not in _state.bot_settings and ctx.sent_embeds == []


# ── menus ────────────────────────────────────────────────────────────────────

def _field_names(ctx) -> list[str]:
    return [f.name for f in ctx.sent_embeds[-1].fields]


async def test_help_drops_the_sections_of_switched_off_features():
    cog = UtilityCog(bot=None)
    ctx = _admin_ctx("help")
    await cog.cmd_help.callback(cog, ctx)
    assert "💰 Economy" in _field_names(ctx) and "🤖 AI" in _field_names(ctx) and "🛒 Shop" in _field_names(ctx)

    set_feature(GID, "economy", False)
    set_feature(GID, "ai", False)
    ctx = _admin_ctx("help")
    await cog.cmd_help.callback(cog, ctx)
    names = _field_names(ctx)
    assert "💰 Economy" not in names and "🤖 AI" not in names and "🛒 Shop" not in names
    assert "🎮 Games" in names
    fields = {f.name: f.value for f in ctx.sent_embeds[-1].fields}
    assert "!leaderboard" not in fields["🏆 Leaderboards"] and "!levels" in fields["🏆 Leaderboards"]
    assert "!effects" not in fields["🎉 Fun"] and "!searchquote" not in fields["🎉 Fun"]


async def test_games_menu_hides_gambling_wagers_and_ai_puzzles():
    cog = UtilityCog(bot=None)
    set_feature(GID, "gambling", False)
    ctx = _admin_ctx("games")
    await cog.cmd_game.callback(cog, ctx)
    fields = {f.name: f.value for f in ctx.sent_embeds[-1].fields}
    assert "💰 Gambling" not in fields and ctx.sent_embeds[-1].title == "🎮 Games"
    assert "[amount]" in fields["🎯 Competitive"]  # the economy is still on

    set_feature(GID, "economy", False)
    set_feature(GID, "ai", False)
    ctx = _admin_ctx("games")
    await cog.cmd_game.callback(cog, ctx)
    fields = {f.name: f.value for f in ctx.sent_embeds[-1].fields}
    assert "[amount]" not in fields["🎯 Competitive"]
    assert "riddleai" not in fields["🧩 Puzzles"] and "coding" not in fields["🧩 Puzzles"]
    assert "!puzzle riddle" in fields["🧩 Puzzles"] and "🪙" not in fields["🧩 Puzzles"]


async def test_ai_puzzle_kinds_refuse_when_ai_is_off_but_riddles_work():
    cog = UtilityCog(bot=None)
    set_feature(GID, "ai", False)
    ctx = _admin_ctx("puzzle")
    await cog.cmd_puzzle.callback(cog, ctx, "coding")
    assert ctx.sent_embeds[-1].title == "🚫 Turned Off"


async def test_economy_and_crime_menus_list_only_enabled_extensions(monkeypatch):
    import src.cogs.economy_cog as economy_cog
    from src.cogs.economy_cog import EconomyCog

    async def _no_lottery(gid):
        return {"prize_pool": 0}
    monkeypatch.setattr(economy_cog, "load_lottery", _no_lottery)
    cog = EconomyCog(bot=None)
    set_feature(GID, "savings", False)
    set_feature(GID, "gambling", False)
    ctx = _admin_ctx("economy")
    await cog.cmd_economy.callback(cog, ctx)
    body = ctx.sent_embeds[-1].description
    assert "!savings" not in body and "!lottery" not in body
    assert "!assets" in body and "!shop" in body and "!crime" in body

    ctx = _admin_ctx("crime")
    await cog.cmd_crime.callback(cog, ctx)
    assert "!bankheist" not in ctx.sent_embeds[-1].description and "!steal" in ctx.sent_embeds[-1].description


async def test_level_unlocks_stop_advertising_switched_off_features():
    from src.level_unlocks import next_unlocks, unlocks_at_level
    assert any(cmd == "savings" for _, cmd, _ in next_unlocks(1, GID, count=10))
    set_feature(GID, "savings", False)
    set_feature(GID, "shop", False)
    assert not any(cmd == "savings" for _, cmd, _ in next_unlocks(1, GID, count=10))
    assert unlocks_at_level(5, GID) == []
    assert [cmd for cmd, _ in unlocks_at_level(10, GID)] == ["steal"]


async def test_dailies_embed_offers_only_the_claim_button_without_gambling():
    from src.cogs.dailies_cog import DAILIES_ALL_EMOJIS, DAILIES_CLAIM_EMOJI, _dailies_body, _dailies_emojis
    assert _dailies_emojis(GID) == DAILIES_ALL_EMOJIS
    set_feature(GID, "gambling", False)
    assert _dailies_emojis(GID) == (DAILIES_CLAIM_EMOJI,)
    body = _dailies_body(GID)
    assert "scratchoff" not in body and "🎰" not in body and DAILIES_CLAIM_EMOJI in body


async def test_tips_skip_commands_the_server_switched_off(monkeypatch):
    tips = ["Try !slots for jackpots.", "Use !hangman with friends.", "Type !b for your balance."]
    monkeypatch.setattr(_fun_cog, "_load_tips", lambda: tips)
    monkeypatch.setattr(_fun_cog, "_tip_queue", [])
    monkeypatch.setattr(_fun_cog, "_tip_pool_signature", frozenset())
    allowed = lambda tip: "!hangman" in tip  # noqa: E731
    assert _fun_cog._next_tip(allowed) == tips[1]
    assert _fun_cog._next_tip(allowed) == tips[1]  # a fresh cycle finds it again
    assert _fun_cog._next_tip(lambda tip: False) is None
    assert _fun_cog._next_tip() in tips


# ── passive paths ────────────────────────────────────────────────────────────

async def test_message_chain_skips_shop_effects_and_auto_daily_when_off():
    from src.events import EventsCog
    message = FakeMessage(content="hello", author=FakeMember(5))
    message.guild = SimpleNamespace(id=GID)
    cog = SimpleNamespace(
        bot=SimpleNamespace(user=FakeMember(999)),
        _handle_msg_xp=AsyncMock(), _handle_ragebait=AsyncMock(), _handle_mock=AsyncMock(),
        _handle_tax=AsyncMock(), _handle_curse=AsyncMock(), _handle_auto_daily=AsyncMock(),
        _handle_blackjack_input=AsyncMock(return_value=False), _handle_puzzle_answer=AsyncMock(return_value=False),
        _handle_hangman_guess=AsyncMock(return_value=False), _handle_ai_routing=AsyncMock(),
    )
    await EventsCog.on_message(cog, message)
    cog._handle_mock.assert_awaited_once()
    cog._handle_auto_daily.assert_awaited_once()

    set_feature(GID, "shop", False)
    set_feature(GID, "economy", False)
    for handler in (cog._handle_mock, cog._handle_auto_daily, cog._handle_msg_xp):
        handler.reset_mock()
    await EventsCog.on_message(cog, message)
    cog._handle_msg_xp.assert_awaited_once()
    cog._handle_mock.assert_not_awaited()
    cog._handle_auto_daily.assert_not_awaited()
    cog._handle_ai_routing.assert_awaited()


async def test_mention_replies_stop_where_ai_is_off():
    from src.events import EventsCog
    bot_user = FakeMember(999)
    message = FakeMessage(content=f"<@{bot_user.id}> hi", author=FakeMember(5))
    message.guild = SimpleNamespace(id=GID)
    message.mentions = [bot_user]
    message.reply = AsyncMock()
    cog = SimpleNamespace(bot=SimpleNamespace(user=bot_user, process_commands=AsyncMock()))
    set_feature(GID, "ai", False)
    await EventsCog._handle_ai_routing(cog, message)
    cog.bot.process_commands.assert_awaited_once_with(message)
    message.reply.assert_not_awaited()


async def test_ai_commands_are_free_where_the_economy_is_off():
    from src.ai import enforce_cost, refund_cost
    from src.economy import get_balance
    ctx = _admin_ctx("ask")
    assert await enforce_cost(ctx, "ask") is False  # nothing in the wallet
    set_feature(GID, "economy", False)
    assert await enforce_cost(ctx, "ask") is True
    await refund_cost(1, "ask", GID)
    assert await get_balance(1) == 0  # no refund for a charge that never happened


async def test_lottery_loop_and_dailies_refresh_skip_switched_off_guilds(monkeypatch):
    from src.cogs import dailies_cog, lottery_cog
    from src.cogs.dailies_cog import refresh_dailies_channel
    set_feature(GID, "economy", False)
    get_guild_cfg(GID)["dailies_channel"] = 77
    get_guild_cfg(GID)["lottery_channel"] = 78
    fetched = AsyncMock()
    bot = SimpleNamespace(get_channel=lambda cid: None, fetch_channel=fetched)
    await refresh_dailies_channel(bot, GID)
    fetched.assert_not_awaited()

    cog = SimpleNamespace(bot=bot)
    await lottery_cog.LotteryCog._run_guild_schedule(cog, SimpleNamespace(id=GID), None)
    fetched.assert_not_awaited()
    assert dailies_cog.feature_enabled(GID, "gambling") is False
