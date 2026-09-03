"""Game threads for tic-tac-toe, Connect 4 and hangman.

Mirrors the chess thread lifecycle (tests/test_chess.py, "Game threads"):
a started game opens a public thread under the channel it was started from
and is keyed by the thread's id (so the parent channel is free at once); at
game end the thread is renamed with the outcome and archived + locked; !stop
renames first and archives after the summary; a deleted thread cancels the
game (refunding wagers). Thread creation failure falls back to the channel.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import src.state as _state
import src.games.ttt_c4 as _ttt_mod
import src.games.hangman as _hm_mod
import src.cogs.ai_cog as _ai_cog
from src.cogs.ai_cog import AICog
from src.games.ttt_c4 import TttC4Cog, _apply_ttt_move, _apply_c4_move
from src.games.hangman import HangmanCog, _process_hangman_guess
from src.games.game_threads import _join_names
from src.economy import get_balance

from tests.fakes.discord import (
    FakeMember, FakeGuild, FakeCtx, FakeMessage, FakeThread,
)


pytestmark = pytest.mark.asyncio


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _stub_board_edits(monkeypatch):
    """No real board I/O: stub _edit_board in every module that calls it."""
    calls = []

    async def _stub(channel, game, embed, *, file=None):
        calls.append((channel, game, embed))
    monkeypatch.setattr(_ttt_mod, "_edit_board", _stub)
    monkeypatch.setattr(_hm_mod, "_edit_board", _stub)
    monkeypatch.setattr(_ai_cog, "_edit_board", _stub)
    return calls


@pytest.fixture(autouse=True)
def _stub_announce_record(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(_hm_mod, "announce_record", _noop)


@pytest.fixture
def _stub_setup_pvp_game(monkeypatch):
    """Bypass the invite/wager flow (ttt_c4 binds its own name)."""
    calls = []

    async def _stub(ctx, opponent, amount, invite_title, *, timeout=60.0):
        calls.append({"opponent": opponent, "amount": amount, "title": invite_title})
        return True
    monkeypatch.setattr(_ttt_mod, "_setup_pvp_game", _stub)
    return calls


def _board_message() -> FakeMessage:
    """A board message the reaction helpers can fully drive."""
    msg = FakeMessage(message_id=77)
    msg.remove_reaction = AsyncMock()
    return msg


def _ctx_for(member: FakeMember, channel_id: int, guild: FakeGuild | None = None) -> FakeCtx:
    g = guild or FakeGuild(gid=42)
    if member not in g.members:
        g.members.append(member)
    g.me = FakeMember(uid=999_000_001, display_name="bot")
    ctx = FakeCtx(author=member, guild=g)
    ctx.channel.id = channel_id
    ctx.channel.guild = g
    ctx.channel.fetch_message = AsyncMock(return_value=_board_message())
    ctx.message = FakeMessage(author=member)
    return ctx


def _game_thread(thread_id: int, guild: FakeGuild) -> FakeThread:
    thread = FakeThread(thread_id=thread_id)
    thread.guild = guild
    thread.fetch_message = AsyncMock(return_value=_board_message())
    return thread


def _seed_pvp(registry: dict, cid: int, p1: FakeMember, p2: FakeMember, *,
              kind: str, amount: int = 0) -> dict:
    board = [None] * 9 if kind == "ttt" else [[None] * 7 for _ in range(6)]
    marks = ("❌", "⭕") if kind == "ttt" else ("🔴", "🟡")
    registry[cid] = {
        "board": board,
        "players": [p1.id, p2.id],
        "marks": {p1.id: marks[0], p2.id: marks[1]},
        "names": {p1.id: p1.display_name, p2.id: p2.display_name},
        "current": p1.id,
        "amount": amount,
        "board_msg_id": 77,
        "last_move": "",
    }
    return registry[cid]


def _seed_hangman(cid: int, host: FakeMember, word: str = "python") -> dict:
    _state.active_hangman_games[cid] = {
        "word": word,
        "guessed_letters": set(),
        "guessed_words": set(),
        "wrong_guesses": 0,
        "user_id": host.id,
        "guild_id": 42,
        "active_players": {host.id},
        "invited_players": {host.id},
        "player_names": {host.id: host.display_name},
        "board_msg_id": 77,
        "last_move": "Game started!",
    }
    return _state.active_hangman_games[cid]


# ── _join_names ───────────────────────────────────────────────────────────────

async def test_join_names_caps_the_list():
    assert _join_names(["A"]) == "A"
    assert _join_names(["A", "B", "C"]) == "A, B, C"
    assert _join_names(["A", "B", "C", "D", "E"]) == "A, B, C +2"


# ── ttt / c4: start ───────────────────────────────────────────────────────────

async def test_ttt_start_opens_thread_and_keys_game_by_thread_id(db, _stub_setup_pvp_game):
    """An accepted match opens a thread named after the players; the game is
    keyed by the thread id (parent channel free), both players are pulled
    in, and the board + reactions land in the thread."""
    cog = TttC4Cog(bot=None)
    challenger = FakeMember(uid=2001, display_name="Challenger")
    opponent = FakeMember(uid=2002, display_name="Opponent")
    ctx = _ctx_for(challenger, channel_id=300)
    ctx.guild.members.append(opponent)
    thread = _game_thread(301, ctx.guild)
    ctx.channel.create_thread = AsyncMock(return_value=thread)

    await cog.cmd_ttt.callback(cog, ctx, opponent, "0")

    assert 301 in _state.active_ttt_games
    assert 300 not in _state.active_ttt_games
    game = _state.active_ttt_games[301]
    assert game["players"] == [challenger.id, opponent.id]
    assert game["names"] == {challenger.id: "Challenger", opponent.id: "Opponent"}
    kwargs = ctx.channel.create_thread.await_args.kwargs
    assert kwargs["name"] == "🎮 Challenger vs Opponent"
    assert kwargs["auto_archive_duration"] == 10080
    assert {c.args[0] for c in thread.add_user.await_args_list} == {challenger, opponent}
    # Board went to the thread, silently; the number reactions followed it.
    assert thread.send.await_count == 1
    assert thread.send.await_args.kwargs.get("silent") is True
    board = thread.fetch_message.return_value
    assert board.add_reaction.await_count == 9
    assert game["board_msg_id"] == board.id or game["board_msg_id"] is not None


async def test_c4_start_opens_thread(db, _stub_setup_pvp_game):
    cog = TttC4Cog(bot=None)
    challenger = FakeMember(uid=2003, display_name="Red")
    opponent = FakeMember(uid=2004, display_name="Yellow")
    ctx = _ctx_for(challenger, channel_id=302)
    ctx.guild.members.append(opponent)
    thread = _game_thread(303, ctx.guild)
    ctx.channel.create_thread = AsyncMock(return_value=thread)

    await cog.cmd_c4.callback(cog, ctx, opponent, "0")

    assert 303 in _state.active_c4_games
    assert 302 not in _state.active_c4_games
    assert ctx.channel.create_thread.await_args.kwargs["name"] == "🟡 Red vs Yellow"
    assert thread.fetch_message.return_value.add_reaction.await_count == 7


async def test_ttt_start_inside_thread_rejected(db, _stub_setup_pvp_game):
    """Threads can't nest — starting from inside one is refused before the
    invite flow runs."""
    cog = TttC4Cog(bot=None)
    challenger = FakeMember(uid=2005)
    opponent = FakeMember(uid=2006)
    guild = FakeGuild(gid=42)
    thread = _game_thread(304, guild)
    ctx = FakeCtx(author=challenger, guild=guild, channel=thread)

    await cog.cmd_ttt.callback(cog, ctx, opponent, "0")

    assert 304 not in _state.active_ttt_games
    assert _stub_setup_pvp_game == []
    assert any("Game Threads" in (e.title or "") for e in ctx.sent_embeds)


async def test_ttt_start_thread_failure_falls_back_to_channel(db, _stub_setup_pvp_game):
    """No Create Public Threads permission (or thread cap) degrades to the
    old in-channel game — wagers are already escrowed by then."""
    cog = TttC4Cog(bot=None)
    challenger = FakeMember(uid=2007)
    opponent = FakeMember(uid=2008)
    ctx = _ctx_for(challenger, channel_id=305)
    ctx.guild.members.append(opponent)
    resp = MagicMock(status=403, reason="Forbidden")
    ctx.channel.create_thread = AsyncMock(side_effect=discord.HTTPException(resp, "no perms"))

    await cog.cmd_ttt.callback(cog, ctx, opponent, "0")

    assert 305 in _state.active_ttt_games
    assert _state.active_ttt_games[305]["players"] == [challenger.id, opponent.id]
    assert ctx.channel.send.await_count == 1


async def test_ttt_start_releases_parent_slot_when_invite_declined(db, monkeypatch):
    cog = TttC4Cog(bot=None)
    challenger = FakeMember(uid=2009)
    opponent = FakeMember(uid=2010)
    ctx = _ctx_for(challenger, channel_id=306)

    async def _declined(ctx, opponent, amount, invite_title, *, timeout=60.0):
        return False
    monkeypatch.setattr(_ttt_mod, "_setup_pvp_game", _declined)

    await cog.cmd_ttt.callback(cog, ctx, opponent, "0")

    assert 306 not in _state.active_ttt_games


# ── ttt / c4: game end renames + closes the thread ───────────────────────────

async def test_ttt_win_renames_thread_with_crown_and_closes_it(db):
    x = FakeMember(uid=2101, display_name="Xavier")
    o = FakeMember(uid=2102, display_name="Olive")
    guild = FakeGuild(gid=42)
    guild.members.extend([x, o])
    guild.me = FakeMember(uid=999_000_001)
    thread = _game_thread(310, guild)
    _seed_pvp(_state.active_ttt_games, 310, x, o, kind="ttt")

    for mover, pos in ((x, 1), (o, 4), (x, 2), (o, 5), (x, 3)):
        await _apply_ttt_move(thread, guild, mover, pos)

    assert 310 not in _state.active_ttt_games
    kwargs = thread.edit.await_args.kwargs
    assert kwargs["name"] == "👑 Xavier won against Olive"
    assert kwargs["archived"] is True
    assert kwargs["locked"] is True


async def test_ttt_draw_renames_thread_with_scales_and_closes_it(db):
    x = FakeMember(uid=2103, display_name="Xavier")
    o = FakeMember(uid=2104, display_name="Olive")
    guild = FakeGuild(gid=42)
    guild.members.extend([x, o])
    guild.me = FakeMember(uid=999_000_001)
    thread = _game_thread(311, guild)
    game = _seed_pvp(_state.active_ttt_games, 311, x, o, kind="ttt")
    # X O X / X O O / O X . — X to play 9: full board, no line.
    game["board"] = ["❌", "⭕", "❌", "❌", "⭕", "⭕", "⭕", "❌", None]

    await _apply_ttt_move(thread, guild, x, 9)

    assert 311 not in _state.active_ttt_games
    kwargs = thread.edit.await_args.kwargs
    assert kwargs["name"] == "⚖️ Xavier drew with Olive"
    assert kwargs["archived"] is True


async def test_c4_win_pays_wager_renames_thread_and_closes_it(db):
    r = FakeMember(uid=2105, display_name="Red")
    y = FakeMember(uid=2106, display_name="Yellow")
    guild = FakeGuild(gid=42)
    guild.members.extend([r, y])
    guild.me = FakeMember(uid=999_000_001)
    thread = _game_thread(312, guild)
    _seed_pvp(_state.active_c4_games, 312, r, y, kind="c4", amount=100)

    for mover, col in ((r, 1), (y, 2), (r, 1), (y, 2), (r, 1), (y, 2), (r, 1)):
        await _apply_c4_move(thread, guild, mover, col)

    assert 312 not in _state.active_c4_games
    assert await get_balance(r.id) == 200
    kwargs = thread.edit.await_args.kwargs
    assert kwargs["name"] == "👑 Red won against Yellow"
    assert kwargs["archived"] is True


async def test_ttt_win_in_plain_channel_does_not_touch_threads(db):
    """The in-channel fallback game has no thread to rename or close."""
    x = FakeMember(uid=2107, display_name="X")
    o = FakeMember(uid=2108, display_name="O")
    guild = FakeGuild(gid=42)
    guild.members.extend([x, o])
    guild.me = FakeMember(uid=999_000_001)
    channel = SimpleNamespace(id=313, guild=guild, send=AsyncMock(),
                              fetch_message=AsyncMock(return_value=_board_message()),
                              edit=AsyncMock())
    _seed_pvp(_state.active_ttt_games, 313, x, o, kind="ttt")

    for mover, pos in ((x, 1), (o, 4), (x, 2), (o, 5), (x, 3)):
        await _apply_ttt_move(channel, guild, mover, pos)

    assert 313 not in _state.active_ttt_games
    channel.edit.assert_not_awaited()


# ── ttt / c4: !stop and thread deletion ──────────────────────────────────────

async def test_stop_in_ttt_thread_renames_then_closes(db, _stub_board_edits):
    """!stop stamps '👑 opponent won against forfeiter' first (board + summary
    still need to post), then archives after the ⏹️ Stopped summary."""
    cog = AICog(bot=None)
    quitter = FakeMember(uid=2201, display_name="Quitter")
    opponent = FakeMember(uid=2202, display_name="Champ")
    guild = FakeGuild(gid=42)
    guild.members.extend([quitter, opponent])
    thread = _game_thread(320, guild)
    ctx = FakeCtx(author=quitter, guild=guild, channel=thread)
    _seed_pvp(_state.active_ttt_games, 320, quitter, opponent, kind="ttt", amount=50)

    await cog.cmd_stop.callback(cog, ctx)

    assert 320 not in _state.active_ttt_games
    assert await get_balance(opponent.id) == 100
    assert thread.edit.await_count == 2
    rename, close = thread.edit.await_args_list
    assert rename.kwargs == {"name": "👑 Champ won against Quitter"}
    assert close.kwargs.get("archived") is True
    # The forfeit board landed before the archive.
    assert len(_stub_board_edits) == 1


async def test_stop_in_hangman_thread_renames_then_closes(db, _stub_board_edits):
    cog = AICog(bot=None)
    host = FakeMember(uid=2203, display_name="Host")
    guild = FakeGuild(gid=42)
    guild.members.append(host)
    thread = _game_thread(321, guild)
    ctx = FakeCtx(author=host, guild=guild, channel=thread)
    _seed_hangman(321, host, word="python")

    await cog.cmd_stop.callback(cog, ctx)

    assert 321 not in _state.active_hangman_games
    rename, close = thread.edit.await_args_list
    assert rename.kwargs == {"name": "🏳️ Host forfeited — the word was python"}
    assert close.kwargs.get("archived") is True
    assert len(_stub_board_edits) == 1


async def test_stop_during_invite_window_ignores_pending_placeholder(db):
    """The parent-channel slot holds {"pending": True} while the invite is
    out — !stop there must not crash on the missing players list."""
    cog = AICog(bot=None)
    user = FakeMember(uid=2204)
    ctx = FakeCtx(author=user, guild=FakeGuild(gid=42))
    ctx.channel.id = 322
    placeholder = {"pending": True}
    _state.active_ttt_games[322] = placeholder

    await cog.cmd_stop.callback(cog, ctx)

    assert _state.active_ttt_games[322] is placeholder
    assert any("Nothing to Stop" in (e.title or "") for e in ctx.sent_embeds)


async def test_thread_delete_cancels_ttt_game_and_refunds_wagers(db):
    cog = TttC4Cog(bot=None)
    p1 = FakeMember(uid=2205)
    p2 = FakeMember(uid=2206)
    _seed_pvp(_state.active_ttt_games, 323, p1, p2, kind="ttt", amount=250)

    await cog.on_thread_delete(SimpleNamespace(id=323))

    assert 323 not in _state.active_ttt_games
    assert await get_balance(p1.id) == 250
    assert await get_balance(p2.id) == 250


async def test_thread_delete_cancels_c4_game(db):
    cog = TttC4Cog(bot=None)
    p1 = FakeMember(uid=2207)
    p2 = FakeMember(uid=2208)
    _seed_pvp(_state.active_c4_games, 324, p1, p2, kind="c4", amount=0)

    await cog.on_thread_delete(SimpleNamespace(id=324))

    assert 324 not in _state.active_c4_games
    assert await get_balance(p1.id) == 0


# ── hangman: start ────────────────────────────────────────────────────────────

async def test_hangman_start_opens_thread_and_keys_game_by_thread_id(db):
    cog = HangmanCog(bot=None)
    host = FakeMember(uid=2301, display_name="Host")
    ctx = _ctx_for(host, channel_id=330)
    thread = _game_thread(331, ctx.guild)
    ctx.channel.create_thread = AsyncMock(return_value=thread)

    await cog.cmd_hangman.callback(cog, ctx)

    assert 331 in _state.active_hangman_games
    assert 330 not in _state.active_hangman_games
    game = _state.active_hangman_games[331]
    assert game["user_id"] == host.id
    kwargs = ctx.channel.create_thread.await_args.kwargs
    assert kwargs["name"] == "🔤 Hangman: Host"
    assert kwargs["auto_archive_duration"] == 10080
    thread.add_user.assert_awaited_once_with(host)
    assert thread.send.await_count == 1
    assert thread.send.await_args.kwargs.get("silent") is True
    assert game["board_msg_id"] is not None


async def test_hangman_start_with_invites_names_thread_after_lobby(db, monkeypatch):
    """Confirmed invitees are in the thread name and get pulled in; a
    decliner is neither."""
    cog = HangmanCog(bot=None)
    host = FakeMember(uid=2302, display_name="Host")
    friend = FakeMember(uid=2303, display_name="Friend")
    flake = FakeMember(uid=2304, display_name="Flake")
    ctx = _ctx_for(host, channel_id=332)
    ctx.guild.members.extend([friend, flake])
    ctx.message.mentions = [friend, flake]
    thread = _game_thread(333, ctx.guild)
    ctx.channel.create_thread = AsyncMock(return_value=thread)

    async def _confirm(ctx, invited_users, title="", timeout=60.0):
        return {friend.id}
    monkeypatch.setattr(_hm_mod, "_wait_for_confirmations", _confirm)

    await cog.cmd_hangman.callback(cog, ctx)

    game = _state.active_hangman_games[333]
    assert game["invited_players"] == {host.id, friend.id}
    assert ctx.channel.create_thread.await_args.kwargs["name"] == "🔤 Hangman: Host, Friend"
    assert {c.args[0] for c in thread.add_user.await_args_list} == {host, friend}


async def test_hangman_start_inside_thread_rejected(db):
    cog = HangmanCog(bot=None)
    host = FakeMember(uid=2305)
    guild = FakeGuild(gid=42)
    thread = _game_thread(334, guild)
    ctx = FakeCtx(author=host, guild=guild, channel=thread)

    await cog.cmd_hangman.callback(cog, ctx)

    assert 334 not in _state.active_hangman_games
    assert any("Game Threads" in (e.title or "") for e in ctx.sent_embeds)


async def test_hangman_start_thread_failure_falls_back_to_channel(db):
    cog = HangmanCog(bot=None)
    host = FakeMember(uid=2306)
    ctx = _ctx_for(host, channel_id=335)
    resp = MagicMock(status=403, reason="Forbidden")
    ctx.channel.create_thread = AsyncMock(side_effect=discord.HTTPException(resp, "no perms"))

    await cog.cmd_hangman.callback(cog, ctx)

    assert 335 in _state.active_hangman_games
    assert ctx.channel.send.await_count == 1


async def test_hangman_stop_during_invite_window_does_not_resurrect_game(db):
    """The host !stops (in the parent channel) while the invite is out: the
    lobby must not come back as a thread once the invite resolves."""
    cog = HangmanCog(bot=None)
    host = FakeMember(uid=2307)
    friend = FakeMember(uid=2308)
    ctx = _ctx_for(host, channel_id=336)
    ctx.message.mentions = [friend]
    ctx.channel.create_thread = AsyncMock(return_value=_game_thread(337, ctx.guild))

    async def _stopped_meanwhile(ctx, invited_users, title="", timeout=60.0):
        _state.active_hangman_games.pop(336, None)
        return {friend.id}
    _hm_mod_wait = _hm_mod._wait_for_confirmations
    _hm_mod._wait_for_confirmations = _stopped_meanwhile
    try:
        await cog.cmd_hangman.callback(cog, ctx)
    finally:
        _hm_mod._wait_for_confirmations = _hm_mod_wait

    assert _state.active_hangman_games == {}
    ctx.channel.create_thread.assert_not_awaited()


# ── hangman: game end renames + closes the thread ────────────────────────────

async def test_hangman_win_renames_thread_and_closes_it(db):
    host = FakeMember(uid=2401, display_name="Host")
    guild = FakeGuild(gid=42)
    guild.members.append(host)
    thread = _game_thread(340, guild)
    _seed_hangman(340, host, word="python")

    await _process_hangman_guess(thread, host.id, 340, "python", "Host")

    assert 340 not in _state.active_hangman_games
    kwargs = thread.edit.await_args.kwargs
    assert kwargs["name"] == "🎉 Host solved python"
    assert kwargs["archived"] is True
    assert kwargs["locked"] is True


async def test_hangman_multiplayer_win_names_every_guesser(db):
    host = FakeMember(uid=2402, display_name="Host")
    friend = FakeMember(uid=2403, display_name="Friend")
    guild = FakeGuild(gid=42)
    guild.members.extend([host, friend])
    thread = _game_thread(341, guild)
    game = _seed_hangman(341, host, word="ab")
    game["invited_players"].add(friend.id)

    await _process_hangman_guess(thread, host.id, 341, "a", "Host")
    assert 341 in _state.active_hangman_games
    await _process_hangman_guess(thread, friend.id, 341, "b", "Friend")

    assert 341 not in _state.active_hangman_games
    assert thread.edit.await_args.kwargs["name"] == "🎉 Host, Friend solved ab"


async def test_hangman_loss_renames_thread_and_closes_it(db):
    host = FakeMember(uid=2404, display_name="Host")
    guild = FakeGuild(gid=42)
    guild.members.append(host)
    thread = _game_thread(342, guild)
    _seed_hangman(342, host, word="python")

    for letter in "abcdef":  # six misses — out of lives
        await _process_hangman_guess(thread, host.id, 342, letter, "Host")

    assert 342 not in _state.active_hangman_games
    kwargs = thread.edit.await_args.kwargs
    assert kwargs["name"] == "💀 Host lost — the word was python"
    assert kwargs["archived"] is True


async def test_hangman_mid_game_guess_leaves_thread_alone(db):
    host = FakeMember(uid=2405, display_name="Host")
    guild = FakeGuild(gid=42)
    thread = _game_thread(343, guild)
    _seed_hangman(343, host, word="python")

    await _process_hangman_guess(thread, host.id, 343, "p", "Host")

    assert 343 in _state.active_hangman_games
    thread.edit.assert_not_awaited()


async def test_hangman_thread_delete_drops_game(db):
    cog = HangmanCog(bot=None)
    host = FakeMember(uid=2406)
    _seed_hangman(344, host)

    await cog.on_thread_delete(SimpleNamespace(id=344))

    assert 344 not in _state.active_hangman_games


async def test_hangman_thread_delete_ignores_unrelated_thread(db):
    cog = HangmanCog(bot=None)
    host = FakeMember(uid=2407)
    _seed_hangman(345, host)

    await cog.on_thread_delete(SimpleNamespace(id=999_345))

    assert 345 in _state.active_hangman_games


# ── the board edit must land before the archive ──────────────────────────────

async def test_stop_waits_for_forfeit_board_before_archiving(db, monkeypatch):
    """The forfeit board edit is fire-and-forget; cmd_stop must wait for it
    before archiving (nothing can be edited in an archived thread)."""
    order = []

    async def _slow_edit(channel, game, embed, *, file=None):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        order.append("board")
    monkeypatch.setattr(_ai_cog, "_edit_board", _slow_edit)

    cog = AICog(bot=None)
    quitter = FakeMember(uid=2501, display_name="Q")
    opponent = FakeMember(uid=2502, display_name="O")
    guild = FakeGuild(gid=42)
    guild.members.extend([quitter, opponent])
    thread = _game_thread(350, guild)

    async def _edit(**kwargs):
        if kwargs.get("archived"):
            order.append("archive")
    thread.edit = AsyncMock(side_effect=_edit)
    ctx = FakeCtx(author=quitter, guild=guild, channel=thread)
    _seed_pvp(_state.active_c4_games, 350, quitter, opponent, kind="c4")

    await cog.cmd_stop.callback(cog, ctx)

    assert order == ["board", "archive"]
