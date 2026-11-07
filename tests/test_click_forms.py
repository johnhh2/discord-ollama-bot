"""Round three of "a click instead of a typed command": the `!graph` panel,
the rankings row on the leaderboards and records, the wallet and savings cards,
the chess pickers, and the forms behind bare `!puzzle`, `!issue`,
`!featurerequest`, `!ask`, `!story` and `!roleplay`."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.cogs.ai_cog as _ai_cog
import src.cogs.economy_cog as _economy_cog
import src.cogs.utility_cog as _utility_cog
import src.games.chess as _chess
from src.cogs.economy_cog import EconomyCog
from src.cogs.graph_cog import GraphCog
from src.cogs.leveling_cog import LevelingCog
from src.cogs.shop_cog import ShopCog
from src.cogs.utility_cog import UtilityCog
from src.graph_hub import GraphHub, items_for as graph_items
from src.panel import _ItemSelect
from src.settings_views import FormModal
from src.wallet_view import WalletView

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember, FakeMessage

pytestmark = pytest.mark.asyncio

GID = 42


def _ctx(command: str = "x", *, uid: int = 1, bot_admin: bool = False) -> FakeCtx:
    ctx = FakeCtx(author=FakeMember(uid, administrator=True), guild=FakeGuild(gid=GID), command_name=command)
    ctx.message.mentions = []
    ctx.message.channel_mentions = []
    ctx.guild.members.extend([FakeMember(1), FakeMember(7, display_name="target")])
    if bot_admin:
        _state.bot_admins.add(uid)
    return ctx


def _interaction(uid: int = 1, guild=None, channel=None):
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
                               edit_message=AsyncMock(), is_done=lambda: False)
    return SimpleNamespace(id=5, user=FakeMember(uid), guild=guild, channel=channel, response=response,
                           edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()), _state=None)


def _fake_bot(*names):
    commands = {}
    for name in names:
        cog = SimpleNamespace(calls=[])

        async def _cb(self, ctx, *args, _cog=cog, **kwargs):
            _cog.calls.append((getattr(ctx.author, "id", None), args, kwargs))
        commands[name] = SimpleNamespace(qualified_name=name, name=name.split(" ")[-1], callback=_cb, cog=cog)
    return SimpleNamespace(get_command=lambda n: commands.get(n), get_cog=lambda n: None), commands


def _pick(view, cls, value):
    select = next(i for i in view.children if isinstance(i, cls))
    select._values = [value]
    return select


def _button(view, label):
    return next(b for b in view.children if getattr(b, "label", None) == label)


# ── !graph ───────────────────────────────────────────────────────────────────

async def test_graph_panel_is_one_dropdown_of_every_graph_plus_breakouts_for_bot_admins():
    ctx = _ctx()
    keys = [i.key for i in graph_items(ctx)]
    assert keys[:3] == ["balance", "economy", "assets"] and len(keys) == 12
    hub = GraphHub(SimpleNamespace(bot=None), ctx)
    assert [type(c).__name__ for c in hub.children] == ["_ItemSelect", "_CloseButton"]  # no page dropdown
    admin_keys = [i.key for i in graph_items(_ctx(bot_admin=True))]
    assert admin_keys[12:] == ["admin:wallet", "admin:savings", "admin:assets", "admin:total"]
    admin_items = graph_items(_ctx(bot_admin=True))[12:]
    assert all(i.label.startswith("🔑 Admin · ") and i.description.startswith("Admin only") for i in admin_items)


async def test_graph_modals_offer_the_user_and_same_axis_graphs_to_combine():
    by_key = {i.key: i for i in graph_items(_ctx())}
    kinds = {f.key: f for f in by_key["balance"].fields}
    assert kinds["user"].kind == "users"
    assert [v for _, v in kinds["with"].options] == ["economy", "assets", "crime", "gambling"]
    assert [v for _, v in by_key["commands"].fields[0].options] == ["server", "ai"]
    assert [f.key for f in by_key["commands"].fields] == ["with"]  # nothing per-user to combine
    assert [f.key for f in by_key["economy"].fields] == ["with", "user"]  # for the per-user graphs it can add
    assert [f.key for f in by_key["levels"].fields] == ["user"]
    assert not by_key["memory"].fields and not by_key["ping"].fields


async def test_graph_levels_is_left_out_of_a_dm():
    ctx = _ctx()
    ctx.guild = None
    assert "levels" not in [i.key for i in graph_items(ctx)]


async def test_graph_pick_forwards_to_the_typed_subcommand_with_mentions():
    bot, commands = _fake_bot("graph balance", "graph memory", "graph wallet", "graph assets")
    cog = GraphCog.__new__(GraphCog)  # no snapshot loop in tests
    cog.bot = bot
    _state.bot_admins.add(1)
    ctx = _ctx()
    hub = GraphHub(cog, ctx)

    interaction = _interaction()
    await _pick(hub, _ItemSelect, "memory").callback(interaction)
    assert commands["graph memory"].cog.calls == [(1, (), {})]

    interaction = _interaction()
    await _pick(hub, _ItemSelect, "balance").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["with"]._values = ["crime", "gambling"]
    modal._inputs["user"]._values = [SimpleNamespace(id=7)]
    await modal.on_submit(_interaction())
    assert commands["graph balance"].cog.calls == [(1, ("crime", "gambling", "<@7>"), {})]

    interaction = _interaction()
    await _pick(hub, _ItemSelect, "admin:wallet").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["n"]._value = "5"
    await modal.on_submit(_interaction())
    assert commands["graph wallet"].cog.calls == [(1, ("5",), {})]

    interaction = _interaction()
    await _pick(hub, _ItemSelect, "admin:assets").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    await modal.on_submit(_interaction())
    assert commands["graph assets"].cog.calls == [(1, ("all",), {})]  # the breakout is `!graph assets all`


# ── rankings row ─────────────────────────────────────────────────────────────

def _rankings_bot():
    """A bot whose get_cog finds the boards' owners."""
    cogs = {}
    bot = SimpleNamespace(user=SimpleNamespace(id=999), fetch_user=AsyncMock(), get_cog=lambda n: cogs.get(n),
                          add_command=lambda c: None, remove_command=lambda n: None)
    leveling = LevelingCog.__new__(LevelingCog)  # no voice loop in tests
    leveling.bot = bot
    cogs.update(EconomyCog=EconomyCog(bot=bot), LevelingCog=leveling, ShopCog=ShopCog(bot=bot))
    return bot, cogs


def _row(view):
    return [getattr(c, "label", None) for c in view.children]


def _current(view):
    return [c.label for c in view.children
            if getattr(c, "disabled", False) and c.style is discord.ButtonStyle.primary]


async def test_every_ranking_carries_the_same_board_row_in_the_same_order(db):
    bot, cogs = _rankings_bot()
    boards = ["🪙 Coins", "🏆 Records", "📊 Levels", "👑 Roles"]  # no idle channel: no Idle RPG
    lb = _ctx("leaderboard")
    await cogs["EconomyCog"].cmd_leaderboard.callback(cogs["EconomyCog"], lb, "server")
    assert _row(lb.sent_views[0]) == boards + ["Server", "Global"]
    assert _current(lb.sent_views[0]) == ["🪙 Coins", "Server"]

    levels = _ctx("levels")
    await cogs["LevelingCog"].cmd_levels.callback(cogs["LevelingCog"], levels)
    assert _row(levels.sent_views[0]) == boards and _current(levels.sent_views[0]) == ["📊 Levels"]

    records = _ctx("records")
    await cogs["EconomyCog"].cmd_records.callback(cogs["EconomyCog"], records)
    assert _row(records.sent_views[0]) == [None] + boards + ["Server", "Global"]  # the section dropdown first
    assert _current(records.sent_views[0]) == ["🏆 Records", "Server"]

    roles = _ctx("roles")
    await cogs["ShopCog"].cmd_roles.callback(cogs["ShopCog"], roles)
    assert _row(roles.sent_views[0]) == boards and _current(roles.sent_views[0]) == ["👑 Roles"]


async def test_a_board_press_redraws_in_place_and_keeps_each_boards_scope(db):
    bot, cogs = _rankings_bot()
    ctx = _ctx("leaderboard")
    await cogs["EconomyCog"].cmd_leaderboard.callback(cogs["EconomyCog"], ctx, "server")
    view = ctx.sent_views[0]

    interaction = _interaction()
    await _button(view, "📊 Levels").callback(interaction)
    assert interaction.response.edit_message.await_args.kwargs["embed"].title == "📊 XP Leaderboard"
    assert "Server" not in _row(view)  # levels are per server only

    await _button(view, "🪙 Coins").callback(_interaction())
    assert _current(view) == ["🪙 Coins", "Server"]
    interaction = _interaction()
    await _button(view, "Global").callback(interaction)
    assert interaction.response.edit_message.await_args.kwargs["embed"].title == "🪙 Leaderboard"
    await _button(view, "🏆 Records").callback(_interaction())
    assert _current(view) == ["🏆 Records", "Server"]  # records keep their own scope


async def test_idle_rpg_is_offered_only_where_its_channel_is_set(db):
    from src.guild_config import get_guild_cfg
    bot, cogs = _rankings_bot()
    ctx = _ctx("leaderboard")
    await cogs["EconomyCog"].cmd_leaderboard.callback(cogs["EconomyCog"], ctx, "idle")
    assert "⚔️ Idle RPG" not in _row(ctx.sent_views[0]) and _current(ctx.sent_views[0])[0] == "🪙 Coins"

    get_guild_cfg(GID)["idle_channel"] = 555
    ctx = _ctx("leaderboard")
    await cogs["EconomyCog"].cmd_leaderboard.callback(cogs["EconomyCog"], ctx, "idle")
    assert _row(ctx.sent_views[0]) == ["🪙 Coins", "🏆 Records", "📊 Levels", "⚔️ Idle RPG", "👑 Roles"]
    assert ctx.sent_embeds[0].title == "⚔️ Idle RPG Leaderboard"


async def test_boards_whose_feature_is_off_are_left_out(db):
    from src.features import set_feature
    bot, cogs = _rankings_bot()
    set_feature(GID, "economy", False)
    set_feature(GID, "shop", False)
    ctx = _ctx("levels")
    await cogs["LevelingCog"].cmd_levels.callback(cogs["LevelingCog"], ctx)
    assert _row(ctx.sent_views[0]) == ["📊 Levels"]


async def test_records_section_dropdown_and_typed_words(db):
    bot, cogs = _rankings_bot()
    cog = cogs["EconomyCog"]
    ctx = _ctx("records")
    await cog.cmd_records.callback(cog, ctx, "server")
    assert ctx.sent_embeds[0].title == "🏆 All-Time Records"
    assert "__**💰 Economy**__" in ctx.sent_embeds[0].description and "__**🎰 Gambling**__" in ctx.sent_embeds[0].description

    select = ctx.sent_views[0].children[0]
    select._values = ["gambling"]
    interaction = _interaction()
    await select.callback(interaction)
    narrowed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert narrowed.title == "🏆 All-Time Records — Gambling"
    assert "__**🎰 Gambling**__" in narrowed.description and "Economy" not in narrowed.description
    typed = _ctx("records")
    await cog.cmd_records.callback(cog, typed, "global", "games")
    assert typed.sent_embeds[0].title == "🏆 Global All-Time Records — Games"
    bad = _ctx("records")
    await cog.cmd_records.callback(cog, bad, "nonsense")
    assert "Usage" in bad.sent_embeds[0].description


# ── wallet & savings cards ───────────────────────────────────────────────────

async def test_balance_card_has_actions_only_for_your_own_wallet():
    cog = EconomyCog(bot=SimpleNamespace(user=SimpleNamespace(id=999)))
    ctx = _ctx("balance")
    await cog.cmd_balance.callback(cog, ctx)
    assert [b.label for b in ctx.sent_views[0].children] == ["Deposit", "Withdraw", "Pay", "Shop"]

    other = _ctx("balance")
    await cog.cmd_balance.callback(cog, other, FakeMember(7))
    assert other.sent_views == []


async def test_savings_card_has_deposit_and_withdraw(db):
    cog = EconomyCog(bot=SimpleNamespace(user=SimpleNamespace(id=999)))
    ctx = _ctx("savings")
    await cog.cmd_savings.callback(cog, ctx)
    assert [b.label for b in ctx.sent_views[0].children] == ["Deposit", "Withdraw"]


async def test_wallet_buttons_run_the_command_for_the_clicker_and_redraw():
    bot, commands = _fake_bot("deposit", "pay")
    cog = SimpleNamespace(bot=bot, cmd_deposit=commands["deposit"], cmd_pay=commands["pay"])
    drawn = []

    async def _render():
        drawn.append(1)
        return _economy_cog.emb("card", "", 0)
    view = WalletView(cog, render=_render, pay=True)
    view.message = FakeMessage()
    guild = FakeGuild(gid=GID)
    guild.members.append(FakeMember(7))
    _state.command_perms["deposit"] = {"tier": "everyone", "hidden": False}

    # The savings level gate applies to a press as to a typed !deposit.
    locked = _interaction(uid=3, guild=guild, channel=SimpleNamespace(id=1, _state=None))
    await _button(view, "Deposit").callback(locked)
    modal = locked.response.send_modal.await_args.args[0]
    modal._inputs["amount"]._value = "5k"
    refused = _interaction(uid=3, guild=guild, channel=SimpleNamespace(id=1, _state=None))
    await modal.on_submit(refused)
    assert "🔒" in refused.response.send_message.await_args.args[0] and commands["deposit"].cog.calls == []
    _state.leveling.setdefault(str(GID), {})[str(3)] = {"level": 10, "xp": 0}

    interaction = _interaction(uid=3, guild=guild, channel=SimpleNamespace(id=1, _state=None))
    await _button(view, "Deposit").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, FormModal)
    modal._inputs["amount"]._value = "5k"
    await modal.on_submit(_interaction(uid=3, guild=guild, channel=SimpleNamespace(id=1, _state=None)))
    assert commands["deposit"].cog.calls == [(3, ("5k",), {})]  # the clicker, not the card's owner
    assert drawn == [1] and view.message.edit.await_count == 1

    interaction = _interaction(uid=3, guild=guild, channel=SimpleNamespace(id=1, _state=None))
    await _button(view, "Pay").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["user"]._values = [SimpleNamespace(id=7)]
    modal._inputs["amount"]._value = "500"
    await modal.on_submit(_interaction(uid=3, guild=guild, channel=SimpleNamespace(id=1, _state=None)))
    (who, args, _), = commands["pay"].cog.calls
    assert who == 3 and args[0].id == 7 and args[1] == "500"


# ── chess pickers ────────────────────────────────────────────────────────────

async def test_chess_view_bare_offers_recent_games_then_replays_the_pick(db, monkeypatch):
    from src.persistence import save_chess_report
    cog = _chess.ChessCog(bot=SimpleNamespace(user=SimpleNamespace(id=999)))
    ctx = _ctx("chess")
    rid = await save_chess_report(guild_id=GID, channel_id=1, white_id=1, black_id=7, winner_id=1, result="1-0",
                                  pgn="1. e4 e5", final_fen="x", elo=None)
    offered = []

    async def _pick_list(ctx_, **kwargs):
        offered.append(kwargs["options"])
        return [str(rid)]
    monkeypatch.setattr(_chess, "pick_from_list", _pick_list)
    viewed = []

    async def _load(report_id):
        viewed.append(report_id)
        return None  # "not found" short-circuits the replay rendering
    monkeypatch.setattr(_chess, "load_chess_report", _load)
    await cog._cmd_view(ctx, ())
    assert offered and offered[0][0][1] == str(rid) and "target" in offered[0][0][0]
    assert viewed == [rid]


# ── forms behind bare commands ───────────────────────────────────────────────

async def _submit(view, label, guild=None, **values):
    """Press `label` on a command menu; fill its modal (if any) with `values` and submit."""
    press = _interaction(guild=guild)
    await _button(view, label).callback(press)
    if not press.response.send_modal.await_count:
        return press
    modal = press.response.send_modal.await_args.args[0]
    for key, value in values.items():
        component = modal._inputs[key]
        if isinstance(value, list):
            component._values = value
        else:
            component._value = value
    submit = _interaction(guild=guild)
    await modal.on_submit(submit)
    return submit


async def test_bare_puzzle_offers_a_button_per_kind_with_invitees_preselected(monkeypatch):
    bot, commands = _fake_bot("puzzle")
    cog = UtilityCog(bot=bot)
    ctx = _ctx("puzzle")

    async def _no_gate(ctx_):
        return False
    monkeypatch.setattr(_utility_cog, "check_puzzle_channel", _no_gate)
    await cog.cmd_puzzle.callback(cog, ctx, "<@7>")  # the bare form: only an invitee, no kind
    view = ctx.sent_views[-1]
    assert [b.label for b in view.children] == ["Coding", "Riddle", "AI riddle"]

    press = _interaction()
    await _button(view, "Coding").callback(press)
    modal = press.response.send_modal.await_args.args[0]
    assert modal.fields[1].defaults == (7,)
    modal._inputs["difficulty"]._values = ["hard"]
    modal._inputs["invite"]._values = [SimpleNamespace(id=7)]
    await modal.on_submit(_interaction())
    assert commands["puzzle"].cog.calls == [(1, ("coding", "hard", "<@7>"), {})]


async def test_bare_puzzle_offers_only_the_curated_riddle_with_ai_off(monkeypatch):
    from src.features import set_feature
    bot, _ = _fake_bot("puzzle")
    cog = UtilityCog(bot=bot)
    ctx = _ctx("puzzle")

    async def _no_gate(ctx_):
        return False
    monkeypatch.setattr(_utility_cog, "check_puzzle_channel", _no_gate)
    set_feature(GID, "ai", False)
    await cog.cmd_puzzle.callback(cog, ctx)
    assert [b.label for b in ctx.sent_views[-1].children] == ["Riddle"]


async def test_bare_issue_and_featurerequest_take_a_form(monkeypatch):
    cog = UtilityCog(bot=SimpleNamespace())
    submitted = []

    async def _submit(ctx_, *, kind, report):
        submitted.append((kind, report))
    monkeypatch.setattr(cog, "_submit_issue", _submit)
    forms = []

    async def _form(ctx_, **kwargs):
        forms.append(kwargs)
        return {"kind": ["task"], "report": "do the thing", "description": "a wish"}
    monkeypatch.setattr(_utility_cog, "open_form", _form)
    _state.bot_admins.add(1)
    await cog.cmd_issue.callback(cog, _ctx("issue"))
    assert submitted == [("task", "do the thing")]
    assert [f.key for f in forms[0]["fields"]] == ["kind", "report"]

    # A wrong kind word still gets the usage line, not a form.
    ctx = _ctx("issue")
    await cog.cmd_issue.callback(cog, ctx, "nonsense", rest="x")
    assert "Usage" in ctx.sent_embeds[-1].description and len(forms) == 1


async def test_bare_story_and_ask_take_a_form(monkeypatch):
    cog = _ai_cog.AICog(bot=SimpleNamespace())
    started = []

    async def _form(ctx_, **kwargs):
        return {"prompt": "a heist", "users": [7], "question": "why?"}
    monkeypatch.setattr(_ai_cog, "open_form", _form)

    async def _no_gate(ctx_):
        return False
    monkeypatch.setattr(_ai_cog, "check_ai_channel", _no_gate)

    async def _budget(ctx_, text):
        started.append(("budget", text))
        return False  # stop before any AI call
    monkeypatch.setattr(_ai_cog, "check_token_budget_or_notify", _budget)
    ctx = _ctx("story")
    await cog.cmd_story.callback(cog, ctx)
    assert started == [("budget", "a heist")]

    _state.bot_settings["ai_enabled"] = True
    ctx = _ctx("ask")
    await cog.cmd_ask.callback(cog, ctx)
    assert started[-1] == ("budget", "why?")


# ── !crime buttons ───────────────────────────────────────────────────────────

def _crime_view(*names, shop=None):
    from src.crime_view import CrimeView
    bot, commands = _fake_bot(*names)
    bot.get_cog = lambda n: shop if n == "ShopCog" else None
    drawn = []

    async def _render():
        drawn.append(1)
        return _economy_cog.emb("card", "", 0)
    view = CrimeView(SimpleNamespace(bot=bot), _ctx(), render=_render)
    view.message = FakeMessage()
    guild = FakeGuild(gid=GID)
    guild.members.append(FakeMember(7))
    return view, commands, guild, drawn


def _press(guild, uid=1):
    return _interaction(uid=uid, guild=guild, channel=SimpleNamespace(id=1, _state=None))


def _jail(uid=1, secs=3600):
    import time as _time
    _state.economy["users"].setdefault(str(uid), {"balance": 0})["jail_until"] = _time.time() + secs


def _labels(view):
    return [b.label for b in view.children]


async def test_crime_buttons_forward_the_member_and_amount_then_redraw():
    view, commands, guild, drawn = _crime_view("steal", "mug")
    interaction = _press(guild)
    await _button(view, "Steal").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["user"]._values = [SimpleNamespace(id=7)]
    refused = _press(guild)
    await modal.on_submit(refused)  # the level lock applies to a press as to a typed !steal
    assert "🔒" in refused.response.send_message.await_args.args[0] and commands["steal"].cog.calls == []
    _state.leveling.setdefault(str(GID), {})["1"] = {"level": 20, "xp": 0}

    interaction = _press(guild)
    await _button(view, "Steal").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["user"]._values = [SimpleNamespace(id=7)]
    await modal.on_submit(_press(guild))
    (uid, (target,), _), = commands["steal"].cog.calls
    assert uid == 1 and target.id == 7 and drawn == [1]

    interaction = _press(guild)
    await _button(view, "Mug").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["user"]._values = [SimpleNamespace(id=7)]
    modal._inputs["amount"]._value = "5k"
    await modal.on_submit(_press(guild))
    assert [(a[0].id, a[1]) for _, a, _ in commands["mug"].cog.calls] == [(7, "5k")]


async def test_jailbreak_and_bail_show_only_while_jailed():
    view, commands, guild, _ = _crime_view("jailbreak", "bail")
    assert _labels(view) == ["Steal", "Mug", "Bank heist", "Insurance 0/2"]

    _jail()
    view, commands, guild, _ = _crime_view("jailbreak", "bail")
    assert _labels(view) == ["Steal", "Mug", "Bank heist", "Jailbreak", "Bail", "Insurance 0/2"]
    await _button(view, "Bail").callback(_press(guild))  # your own bail, no form
    assert commands["bail"].cog.calls == [(1, (), {})]

    _state.economy["users"]["1"]["jail_until"] = 0  # released: the redraw drops both
    await _button(view, "Jailbreak").callback(_press(guild))
    assert commands["jailbreak"].cog.calls == [(1, (), {})]
    assert "Jailbreak" not in _labels(view) and "Bail" not in _labels(view)


async def test_insurance_button_counts_personal_and_property_and_hides_when_both_held():
    _state.insurance_subs[1] = "basic"
    view, _, _, _ = _crime_view()
    assert _labels(view)[-1] == "Insurance 1/2"
    _state.property_insurance_subs[1] = "basic"
    view, _, _, _ = _crime_view()
    assert not any(label.startswith("Insurance") for label in _labels(view))


async def test_crime_card_is_the_invokers_and_ignores_the_silenced():
    _jail()
    view, commands, guild, _ = _crime_view("jailbreak")
    stranger = _press(guild, uid=9)
    assert await view.interaction_check(stranger) is False
    _state.global_blocklist[1] = {"reason": None, "banned_by": 2, "banned_at": None}
    try:
        await _button(view, "Jailbreak").callback(_press(guild))
    finally:
        _state.global_blocklist.pop(1)
    assert commands["jailbreak"].cog.calls == []


async def test_crime_insurance_button_opens_the_typed_shop_insurance():
    shop = SimpleNamespace(forward=AsyncMock(return_value="🚫 That needs **Shop**, which is off in this server."))
    view, _, guild, _ = _crime_view("shop insurance", shop=shop)
    interaction = _press(guild)
    await _button(view, "Insurance 0/2").callback(interaction)
    assert shop.forward.await_args.args[1] == "shop_insurance"
    assert "Shop" in interaction.followup.send.await_args.args[0]  # the shop's refusal, privately


# ── !chess menu ──────────────────────────────────────────────────────────────

async def test_bare_chess_has_bot_challenge_and_shop_buttons(db):
    from src.games.chess import ChessCog
    bot, commands = _fake_bot("chess", "chessbot")
    cog = ChessCog.__new__(ChessCog)
    cog.bot = bot
    ctx = _ctx("chess")
    await cog.cmd_chess.callback(cog, ctx)
    view = ctx.sent_views[-1]
    assert [b.label for b in view.children] == ["Play the bot", "Challenge a player", "Chess shop",
                                                "Inventory", "Replay a game", "Game PGN"]
    guild = FakeGuild(gid=GID)
    guild.members.append(FakeMember(7))

    await _submit(view, "Play the bot", guild, elo="1500")
    await _submit(view, "Play the bot", guild, elo="")  # empty: the ladder
    assert [a for _, a, _ in commands["chessbot"].cog.calls] == [("1500",), ()]

    await _submit(view, "Challenge a player", guild, user=[SimpleNamespace(id=7)], wager="2.5k")
    await _submit(view, "Chess shop", guild)
    assert [a for _, a, _ in commands["chess"].cog.calls] == [("<@7>", "2.5k"), ("shop",)]


async def test_chess_challenge_hands_the_opponent_over_as_a_mention():
    from src.games.chess import _challenge
    guild = FakeGuild(gid=GID)
    opponent = FakeMember(7)
    guild.members.append(opponent)
    call = _challenge({"user": [7], "wager": ""}, guild)
    assert call.args == ("<@7>",) and call.mentions == (opponent,)
    assert _challenge({"user": [], "wager": ""}, guild) == "Pick someone in this server."


# ── bare !flip ───────────────────────────────────────────────────────────────

async def test_bare_flip_tosses_a_coin_and_offers_a_bet():
    from src.gambling.flip import FlipCog
    bot, commands = _fake_bot("flip")
    cog = FlipCog(bot)
    ctx = _ctx("flip")
    await cog.cmd_flip.callback(cog, ctx)
    embed = ctx.sent_embeds[-1]
    assert ("Heads" in embed.description or "Tails" in embed.description) and "!flip <coins>" in embed.description
    view = ctx.sent_views[-1]
    await _submit(view, "Bet coins", FakeGuild(gid=GID), amount="500", side=["tails"])
    assert commands["flip"].cog.calls == [(1, ("500", 1, "tails"), {})]


async def test_bare_flip_with_gambling_off_has_no_bet_and_no_betting_hint():
    from src.features import set_feature
    from src.gambling.flip import FlipCog
    cog = FlipCog(_fake_bot("flip")[0])
    set_feature(GID, "gambling", False)
    ctx = _ctx("flip")
    await cog.cmd_flip.callback(cog, ctx)
    assert "!flip <coins>" not in ctx.sent_embeds[-1].description
    assert not any(v for v in ctx.sent_views)


async def test_the_feature_gate_lets_only_a_bare_flip_through_with_gambling_off():
    from discord.ext.commands.view import StringView
    from src.features import FREE_WHEN_BARE, disabled_feature_for, set_feature
    set_feature(GID, "gambling", False)
    assert "flip" in FREE_WHEN_BARE and disabled_feature_for("flip", GID) is not None

    def _remainder(text):
        view = StringView(text)
        view.skip_string("!")
        view.get_word()
        return view.buffer[view.index:].strip()
    assert _remainder("!flip") == "" and _remainder("!flip 500") == "500"


# ── counters ─────────────────────────────────────────────────────────────────

async def test_counter_list_offers_view_add_and_the_admin_actions():
    from src.cogs.counter_cog import CounterCog
    bot, commands = _fake_bot("count", "counter add", "counter addperm")
    cog = CounterCog(bot)
    _state.counters[GID] = {"afk": {"description": "times afk", "user_required": False, "kind": "number",
                                    "created_by": 1, "created_at": 0, "values": {}}}
    ctx = _ctx("count")  # an administrator, so a writer and a counter admin
    await cog.cmd_count.callback(cog, ctx)
    view = ctx.sent_views[-1]
    assert [b.label for b in view.children] == ["View", "Add", "New counter", "Remove counter",
                                                "Give access", "Take access", "Access list"]
    guild = FakeGuild(gid=GID)
    guild.members.append(FakeMember(7))

    await _submit(view, "Add", guild, counter=["afk"], user=[SimpleNamespace(id=7)], amount="3")
    await _submit(view, "View", guild, counter=["afk"], user=[])
    assert commands["count"].cog.calls == [(1, ("afk",), {"rest": "7 3"}), (1, ("afk",), {"rest": ""})]

    await _submit(view, "New counter", guild, name="wins", description="games won")
    assert commands["counter add"].cog.calls == [(1, ("wins",), {"description": "games won"})]
    await _submit(view, "Give access", guild, user=[SimpleNamespace(id=7)])
    (_, _, kwargs), = commands["counter addperm"].cog.calls
    assert kwargs["member"].id == 7


async def test_counter_list_for_a_plain_member_is_view_only():
    from src.cogs.counter_cog import CounterCog
    cog = CounterCog(_fake_bot("count")[0])
    _state.command_perms["counter"] = {"tier": "server_admin", "hidden": False}
    _state.counters[GID] = {"afk": {"description": "times afk", "user_required": False, "kind": "number",
                                    "created_by": 1, "created_at": 0, "values": {}}}
    ctx = FakeCtx(author=FakeMember(5), guild=FakeGuild(gid=GID), command_name="count")
    await cog.cmd_count.callback(cog, ctx)
    assert [b.label for b in ctx.sent_views[-1].children] == ["View"]


async def test_bare_counter_subcommands_prompt_instead_of_printing_usage(monkeypatch):
    import src.cogs.counter_cog as _counter_cog
    from src.cogs.counter_cog import CounterCog
    cog = CounterCog(_fake_bot()[0])
    seen = []

    async def _form(ctx_, **kwargs):
        seen.append(kwargs["title"])
        return None
    monkeypatch.setattr(_counter_cog, "open_form", _form)
    ctx = _ctx("counter add")
    await cog.cmd_counter_add.callback(cog, ctx)
    assert seen == ["🔢 New counter"] and not any("Usage" in (e.title or "") for e in ctx.sent_embeds)

    async def _users(ctx_, **kwargs):
        seen.append(kwargs["title"])
        return None
    monkeypatch.setattr(_counter_cog, "pick_users", _users)
    await cog.cmd_counter_addperm.callback(cog, _ctx("counter addperm"))
    assert seen[-1] == "🔑 Give counter access"


async def test_chess_inventory_offers_owned_unequipped_items_and_equips_by_name(db):
    from src.chess_shop import BOARD_ITEMS, PIECE_SET_ITEMS
    from src.games.chess import ChessCog
    bot, commands = _fake_bot("chess")
    cog = ChessCog.__new__(ChessCog)
    cog.bot = bot
    paid_board = next(it for it in BOARD_ITEMS if it["cost"] > 0)
    _state.chess_unlocks[1] = {paid_board["id"]}
    ctx = _ctx("chess")
    await cog.cmd_chess.callback(cog, ctx, "inventory")
    view = ctx.sent_views[-1]
    labels = [b.label for b in view.children]
    assert "Change board" in labels
    # Nothing to switch to means no button.
    free_sets = [it for it in PIECE_SET_ITEMS if it["cost"] == 0]
    assert ("Change pieces" in labels) == (len(free_sets) > 1)

    await _submit(view, "Change board", FakeGuild(gid=GID), item=[paid_board["name"]])
    assert commands["chess"].cog.calls == [(1, (paid_board["name"],), {})]


async def test_shop_artifact_singular_is_the_artifacts_subcommand():
    import discord as _discord
    from discord.ext import commands as _commands
    bot = _commands.Bot(command_prefix="!", intents=_discord.Intents.none())
    await bot.add_cog(ShopCog(bot))
    shop = bot.get_command("shop")
    assert shop.get_command("artifact") is shop.get_command("artifacts")
