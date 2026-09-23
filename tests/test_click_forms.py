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

async def test_graph_panel_lists_every_graph_and_the_admin_page_for_bot_admins():
    ctx = _ctx()
    keys = [i.key for i in graph_items("graphs", ctx)]
    assert keys[:3] == ["balance", "economy", "assets"] and len(keys) == 12
    by_key = {i.key: i for i in graph_items("graphs", ctx)}
    assert by_key["balance"].fields and by_key["balance"].fields[0].kind == "users"  # takes a user
    assert not by_key["memory"].fields
    assert [k for k, _ in GraphHub(SimpleNamespace(bot=None), ctx).pages()] == ["graphs"]
    assert [k for k, _ in GraphHub(SimpleNamespace(bot=None), _ctx(bot_admin=True)).pages()] == ["graphs", "admin"]


async def test_graph_pick_forwards_to_the_typed_subcommand_with_mentions():
    bot, commands = _fake_bot("graph balance", "graph memory", "graph admin wallet")
    cog = GraphCog.__new__(GraphCog)  # no snapshot loop in tests
    cog.bot = bot
    ctx = _ctx()
    hub = GraphHub(cog, ctx, "graphs")

    interaction = _interaction()
    await _pick(hub, _ItemSelect, "memory").callback(interaction)
    assert commands["graph memory"].cog.calls == [(1, (), {})]

    interaction = _interaction()
    await _pick(hub, _ItemSelect, "balance").callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal._inputs["user"]._values = [SimpleNamespace(id=7)]
    await modal.on_submit(_interaction())
    assert commands["graph balance"].cog.calls == [(1, ("<@7>",), {})]

    _state.bot_admins.add(1)
    hub.page = "admin"
    hub._build()
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
