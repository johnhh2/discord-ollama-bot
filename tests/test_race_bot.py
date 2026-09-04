"""`!race @Bot [amount]` and play_bot_race (src/games/race.py).

The house race is a coin flip with a track: the bot stakes nothing and holds
no balance, a win pays 2× the stake, a loss forfeits it, a photo-finish tie
is a push. Pins:
- payout / forfeit / push arithmetic and the gambling-event record
- a free race (no amount) runs for a player with no coins at all
- an unaffordable stake is refused before the board is posted
- the bot only races one-on-one; extra invitees are rejected
- no channel slot is taken (two races can run at once) and !stop is inert
- a Discord failure mid-race refunds the stake
- godmode: no charge, no payout, no event
- the "biggest race payout" record honours record_exclude
- !race @Bot attaches Race Again / 2x buttons; play_bot_race alone does not
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.economy as _economy
import src.games.race as _race
from src.games.race import RaceCog, play_bot_race
from src.gambling.play_again import PlayAgainView
from src.persistence import load_records

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage

BOT_ID = 999_999_999
GID = 42


class _Resp:
    status = 404
    reason = "Not Found"


class _StubBot:
    def __init__(self):
        self.user = SimpleNamespace(id=BOT_ID, bot=True, display_name="Bot")


def _bot_member():
    return FakeMember(uid=BOT_ID, display_name="Bot")


@pytest.fixture(autouse=True)
def _instant_ticks(monkeypatch):
    monkeypatch.setattr(_race, "RACE_TICK_SECONDS", 0)


def _script(monkeypatch, human: int, bot: int):
    """Every tick moves the human lane `human` squares and the bot's `bot`."""
    monkeypatch.setattr(_race, "_tick_rolls", lambda players: {players[0]: human, players[1]: bot})


def _ctx(uid: int = 1, *, mentions, edit_raises=None):
    """A ctx whose channel keeps every message it posts (the race board is
    edited in place, so tests read the final result off `msg.edit`)."""
    author = FakeMember(uid=uid, display_name="player")
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=GID))
    ctx.bot = _StubBot()
    ctx.message.mentions = list(mentions)
    ctx.channel.posted = []

    async def _send(*args, **kwargs):
        msg = FakeMessage()
        if edit_raises is not None:
            msg.edit = AsyncMock(side_effect=edit_raises)
        ctx.channel.posted.append((kwargs, msg))
        return msg

    ctx.channel.send = AsyncMock(side_effect=_send)
    return ctx


def _board(ctx):
    boards = [m for kw, m in ctx.channel.posted
              if kw.get("embed") is not None and kw["embed"].title == "🏇 Race Starting!"]
    assert len(boards) == 1, [kw.get("embed") and kw["embed"].title for kw, _ in ctx.channel.posted]
    return boards[0]


def _final(ctx):
    return _board(ctx).edit.await_args.kwargs


async def _run_cmd(ctx, *args):
    cog = RaceCog(bot=ctx.bot)
    await cog.cmd_race.callback(cog, ctx, *args)


# ── outcomes ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_win_pays_double_records_event_and_sets_record(db, monkeypatch):
    await _economy.add_balance(1, 1_000)
    _script(monkeypatch, human=3, bot=1)
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx, "100")

    assert await _economy.get_balance(1) == 1_100
    assert _state.gambling_today_by_user[(GID, "1")] == {"gained": 100, "lost": 0}
    final = _final(ctx)
    assert final["embed"].title == "🏁 Race Finished!"
    assert "🏆 **player** wins **200 🪙**!" in final["embed"].description
    assert "Balance: 1,100 🪙" in final["embed"].description
    assert (await load_records(GID))["race"]["value"] == 200
    assert _state.active_race_games == {}


@pytest.mark.asyncio
async def test_loss_forfeits_stake(db, monkeypatch):
    await _economy.add_balance(1, 1_000)
    _script(monkeypatch, human=1, bot=3)
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx, "100")

    assert await _economy.get_balance(1) == 900
    assert _state.gambling_today_by_user[(GID, "1")] == {"gained": 0, "lost": 100}
    desc = _final(ctx)["embed"].description
    assert "🤖 **Bot** wins — **player** lost **100 🪙**." in desc
    assert "race" not in await load_records(GID)


@pytest.mark.asyncio
async def test_photo_finish_tie_is_a_push(db, monkeypatch):
    await _economy.add_balance(1, 1_000)
    _script(monkeypatch, human=3, bot=3)   # both cross on the same tick, same distance
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx, "100")

    assert await _economy.get_balance(1) == 1_000
    assert (GID, "1") not in _state.gambling_today_by_user
    assert "🤝 Photo finish — a tie! **player** gets the **100 🪙** stake back." in _final(ctx)["embed"].description


@pytest.mark.asyncio
async def test_k_shorthand_stake(db, monkeypatch):
    await _economy.add_balance(1, 5_000)
    _script(monkeypatch, human=3, bot=1)
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx, "2.5k")

    assert await _economy.get_balance(1) == 7_500


# ── free race / affordability ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_free_race_runs_with_no_coins_at_all(db, monkeypatch):
    """`!race @Bot` with no amount is a free race — a broke player can still
    race, and nothing is charged, paid, or recorded."""
    _script(monkeypatch, human=3, bot=1)
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx)

    assert await _economy.get_balance(1) == 0
    assert (GID, "1") not in _state.gambling_today_by_user
    titles = [kw["embed"].title for kw, _ in ctx.channel.posted if kw.get("embed") is not None]
    assert "💸 Insufficient Funds" not in titles
    final = _final(ctx)
    assert final["embed"].description.endswith("🏆 **player** wins!")
    assert "race" not in await load_records(GID)
    # A free race still offers a rematch — just no "2x" of nothing.
    assert [b.label for b in final["view"].children] == ["Race Again · free"]


@pytest.mark.asyncio
async def test_unaffordable_stake_is_refused_before_the_board(db, monkeypatch):
    await _economy.add_balance(1, 50)
    _script(monkeypatch, human=3, bot=1)
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx, "100")

    assert await _economy.get_balance(1) == 50
    titles = [kw["embed"].title for kw, _ in ctx.channel.posted if kw.get("embed") is not None]
    assert titles == ["💸 Insufficient Funds"]


# ── roster / channel rules ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bot_plus_other_invitees_is_rejected(db, monkeypatch):
    await _economy.add_balance(1, 1_000)
    _script(monkeypatch, human=3, bot=1)
    ctx = _ctx(mentions=[_bot_member(), FakeMember(uid=2, display_name="friend")])

    await _run_cmd(ctx, "100")

    assert await _economy.get_balance(1) == 1_000
    assert [e.title for e in ctx.sent_embeds] == ["❌ Bot Races Are One-on-One"]
    assert ctx.channel.posted == []
    assert _state.active_race_games == {}


@pytest.mark.asyncio
async def test_bot_race_takes_no_channel_slot(db, monkeypatch):
    """Two players can race the bot in one channel at once — the house race
    never claims state.active_race_games, so neither blocks the other and a
    PvP 'Game Active' gate never fires."""
    await _economy.add_balance(1, 1_000)
    await _economy.add_balance(2, 1_000)
    _script(monkeypatch, human=3, bot=1)
    seen_slots = []
    real_rolls = _race._tick_rolls

    def _spy(players):
        seen_slots.append(dict(_state.active_race_games))
        return real_rolls(players)
    monkeypatch.setattr(_race, "_tick_rolls", _spy)
    a = _ctx(uid=1, mentions=[_bot_member()])
    b = _ctx(uid=2, mentions=[_bot_member()])

    await asyncio.gather(_run_cmd(a, "100"), _run_cmd(b, "100"))

    assert await _economy.get_balance(1) == 1_100
    assert await _economy.get_balance(2) == 1_100
    assert seen_slots and all(s == {} for s in seen_slots)


@pytest.mark.asyncio
async def test_bot_race_still_allowed_during_a_pvp_race(db, monkeypatch):
    await _economy.add_balance(1, 1_000)
    _script(monkeypatch, human=3, bot=1)
    _state.active_race_games[100] = {"players": [7, 8], "names": {}, "positions": {}, "amount": 0}
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx, "100")

    assert await _economy.get_balance(1) == 1_100
    assert 100 in _state.active_race_games   # the PvP race is untouched


# ── failure / godmode ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_board_failure_mid_race_refunds_the_stake(db, monkeypatch):
    await _economy.add_balance(1, 1_000)
    _script(monkeypatch, human=1, bot=1)   # several ticks, so an edit happens
    ctx = _ctx(mentions=[_bot_member()], edit_raises=discord.NotFound(_Resp(), "gone"))

    await _run_cmd(ctx, "100")

    assert await _economy.get_balance(1) == 1_000
    assert (GID, "1") not in _state.gambling_today_by_user


@pytest.mark.asyncio
async def test_godmode_races_free_and_wins_nothing(db, monkeypatch):
    _state.godmode_users.add(1)
    _script(monkeypatch, human=3, bot=1)
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx, "100")

    assert await _economy.get_balance(1) == 0
    assert (GID, "1") not in _state.gambling_today_by_user
    assert "🏆 **player** wins **200 🪙**!" in _final(ctx)["embed"].description


# ── play_bot_race directly (the dailies path) ─────────────────────────────────

@pytest.mark.asyncio
async def test_record_exclude_shrinks_the_record_not_the_payout(db, monkeypatch):
    """The dailies claim passes its property-revenue portion as
    record_exclude: the payout is still 2× the whole stake, but the record
    only sees the non-property part."""
    await _economy.add_balance(1, 1_000)
    _script(monkeypatch, human=3, bot=1)
    ctx = _ctx(mentions=[])

    await play_bot_race(ctx.author, ctx.channel, ctx.guild, 1_000, _bot_member(), record_exclude=400)

    assert await _economy.get_balance(1) == 2_000
    assert (await load_records(GID))["race"]["value"] == 1_200
    # One-shot: no Race Again buttons unless asked for.
    assert "view" not in _final(ctx)


@pytest.mark.asyncio
async def test_command_attaches_race_again_and_2x_buttons(db, monkeypatch):
    await _economy.add_balance(1, 1_000)
    _script(monkeypatch, human=3, bot=1)
    ctx = _ctx(mentions=[_bot_member()])

    await _run_cmd(ctx, "100")

    view = _final(ctx)["view"]
    assert isinstance(view, PlayAgainView)
    assert view.message is _board(ctx)
    assert [(b.label, b.stake) for b in view.children] == [
        ("Race Again · 100 🪙", 100), ("2x · 200 🪙", 200),
    ]
