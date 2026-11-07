"""Round three of "a click instead of a typed command": the `!graph` panel,
scope buttons on records and the leaderboard, the wallet and savings cards,
the chess pickers, and the forms behind bare `!puzzle`, `!issue`,
`!featurerequest`, `!ask`, `!story` and `!roleplay`."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.cogs.ai_cog as _ai_cog
import src.cogs.economy_cog as _economy_cog
import src.cogs.utility_cog as _utility_cog
import src.games.chess as _chess
from src.cogs.economy_cog import EconomyCog
from src.cogs.graph_cog import GraphCog
from src.cogs.utility_cog import UtilityCog
from src.graph_hub import GraphHub, items_for as graph_items
from src.panel import _ItemSelect
from src.scope_view import ScopeView, send_scoped
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
    bot, commands = _fake_bot("graph balance", "graph memory", "graph admin wallet")
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
    assert commands["graph admin wallet"].cog.calls == [(1, ("5",), {})]


# ── scope buttons ────────────────────────────────────────────────────────────

async def test_scope_view_rerenders_on_press_and_disables_the_current_scope():
    seen = []

    async def _render(scope):
        seen.append(scope)
        return _economy_cog.emb(scope, "", 0)
    ctx = _ctx()
    await send_scoped(ctx, _render, [("Server", "server"), ("Global", "global")], "server")
    view = ctx.sent_views[0]
    assert isinstance(view, ScopeView) and _button(view, "Server").disabled and not _button(view, "Global").disabled
    interaction = _interaction()
    await _button(view, "Global").callback(interaction)
    assert seen == ["server", "global"]
    assert interaction.response.edit_message.await_args.kwargs["embed"].title == "global"
    assert _button(view, "Global").disabled and not _button(view, "Server").disabled


async def test_records_and_leaderboard_carry_scope_buttons(db):
    cog = EconomyCog(bot=SimpleNamespace(user=SimpleNamespace(id=999), fetch_user=AsyncMock()))
    ctx = _ctx("records")
    await cog.cmd_records.callback(cog, ctx, "server")
    view = ctx.sent_views[0]
    assert [getattr(b, "label", None) for b in view.children] == [None, "Server", "Global"]
    assert ctx.sent_embeds[0].title == "🏆 All-Time Records"
    assert "__**💰 Economy**__" in ctx.sent_embeds[0].description and "__**🎰 Gambling**__" in ctx.sent_embeds[0].description

    # The dropdown narrows the board to one section; a typed word does the same.
    select = view.children[0]
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

    ctx = _ctx("leaderboard")
    await cog.cmd_leaderboard.callback(cog, ctx, "idle")
    assert [b.label for b in ctx.sent_views[0].children] == ["Server", "Global", "Idle RPG"]
    assert ctx.sent_embeds[0].title == "⚔️ Idle RPG Leaderboard"


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

async def test_bare_puzzle_offers_the_kinds_and_runs_the_pick(monkeypatch):
    cog = UtilityCog(bot=SimpleNamespace())
    ctx = _ctx("puzzle")
    ran = []

    async def _pick_list(ctx_, **kwargs):
        return ["coding hard"]
    monkeypatch.setattr(_utility_cog, "pick_from_list", _pick_list)
    original = cog.cmd_puzzle.callback

    async def _spy(self, ctx_, *args):
        if args:
            ran.append(args)
            return
        await original(self, ctx_, *args)
    monkeypatch.setattr(cog.cmd_puzzle, "callback", _spy)

    async def _no_gate(ctx_):
        return False
    monkeypatch.setattr(_utility_cog, "check_puzzle_channel", _no_gate)
    await original(cog, ctx, "<@7>")  # the bare form: only an invitee, no kind
    assert ran == [("coding", "hard", "<@7>")]


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


async def test_crime_buttons_forward_the_member_and_amount_then_redraw():
    view, commands, guild, drawn = _crime_view("steal", "mug", "jailbreak", "bail")
    interaction = _press(guild)
    await _button(view, "Jailbreak").callback(interaction)
    assert commands["jailbreak"].cog.calls == [(1, (), {})]
    await _button(view, "Steal").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["user"]._values = [SimpleNamespace(id=7)]
    refused = _press(guild)
    await modal.on_submit(refused)  # the level lock applies to a press as to a typed !steal
    assert "🔒" in refused.response.send_message.await_args.args[0] and commands["steal"].cog.calls == []
    _state.leveling.setdefault(str(GID), {})["1"] = {"level": 20, "xp": 0}
    commands["jailbreak"].cog.calls.clear()
    drawn.clear()
    assert [b.label for b in view.children] == ["Steal", "Mug", "Bank heist", "Jailbreak", "Bail", "Insurance"]

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

    await _button(view, "Jailbreak").callback(_press(guild))
    assert commands["jailbreak"].cog.calls == [(1, (), {})]

    interaction = _press(guild)
    await _button(view, "Bail").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    await modal.on_submit(_press(guild))  # left empty: your own bail
    assert commands["bail"].cog.calls == [(1, (), {})]


async def test_crime_card_is_the_invokers_and_ignores_the_silenced():
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
    await _button(view, "Insurance").callback(interaction)
    assert shop.forward.await_args.args[1] == "shop_insurance"
    assert "Shop" in interaction.followup.send.await_args.args[0]  # the shop's refusal, privately
