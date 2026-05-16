"""cmd_stop forfeit + payout coverage.

cmd_stop in src/cogs/ai_cog.py walks every active activity registry in
the channel/uid and either ends it (paying out the wager pot to the
opponent for PvP games, refunding nothing for solo games like blackjack)
or releases the user's seat (AI thread invited group).

The 3 PvP games (ttt, c4, chess) all route through the new
_stop_pvp_game helper. Race uses its own multi-player branch. Blackjack,
hangman, AI thread, and puzzle each have their own shape.

These tests exercise the helper and each branch end-to-end via
`await cog.cmd_stop.callback(cog, ctx)`. _edit_board is stubbed so the
tests don't need a real board message.
"""
import asyncio

import pytest

import src.state as _state
import src.economy as _economy
import src.cogs.ai_cog as _ai_cog
from src.cogs.ai_cog import AICog

from tests.fakes.discord import FakeMember, FakeGuild, FakeCtx


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _stub_edit_board(monkeypatch):
    """cmd_stop edits the persistent board message asynchronously. The
    embed-render itself isn't the test concern — only the state mutation
    + payout. Stub it so no fetch_message is required."""
    edit_calls = []
    async def _stub(channel, game, embed, *, file=None):
        edit_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_ai_cog, "_edit_board", _stub)
    return edit_calls


@pytest.fixture(autouse=True)
def _reset_game_registries():
    """conftest doesn't reset the game registries that cmd_stop walks
    (active_blackjack/hangman/ttt/c4/race/puzzles). Clear them between
    tests so tests can't leak state to each other."""
    yield
    _state.active_blackjack_games.clear()
    _state.active_hangman_games.clear()
    _state.active_ttt_games.clear()
    _state.active_c4_games.clear()
    _state.active_race_games.clear()
    _state.active_puzzles.clear()


def _ctx(author: FakeMember, channel_id: int = 100) -> FakeCtx:
    """A FakeCtx with a channel whose id matches what cmd_stop uses for cid."""
    guild = FakeGuild(gid=42)
    guild.members = [author]
    ctx = FakeCtx(author=author, guild=guild)
    ctx.channel.id = channel_id
    return ctx


# ── _stop_pvp_game (ttt / c4 / chess) ─────────────────────────────────────────

async def test_stop_ttt_with_wager_pays_opponent_double(db, _stub_edit_board):
    cog = AICog(bot=None)
    forfeiter = FakeMember(uid=1001)
    opponent = FakeMember(uid=1002)
    ctx = _ctx(forfeiter, channel_id=500)

    _state.active_ttt_games[500] = {
        "players": [forfeiter.id, opponent.id],
        "amount": 250,
        "board": [None] * 9,
        "turn": 0,
    }

    await cog.cmd_stop.callback(cog, ctx)
    # cmd_stop schedules the board-edit via asyncio.create_task; yield once
    # so the spy fires before we assert on it.
    await asyncio.sleep(0)

    # Game cleared.
    assert 500 not in _state.active_ttt_games
    # Opponent got 2× the wager (their original stake + the forfeiter's stake).
    assert await _economy.get_balance(opponent.id) == 500
    # Edit-board called for the 🏳️ embed.
    assert len(_stub_edit_board) == 1
    embed = _stub_edit_board[0][2]
    assert "Forfeited" in embed.title


async def test_stop_ttt_no_wager_records_forfeit_without_payout(db, _stub_edit_board):
    cog = AICog(bot=None)
    forfeiter = FakeMember(uid=1003)
    opponent = FakeMember(uid=1004)
    ctx = _ctx(forfeiter, channel_id=501)

    _state.active_ttt_games[501] = {
        "players": [forfeiter.id, opponent.id],
        "amount": 0,
        "board": [None] * 9,
        "turn": 0,
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 501 not in _state.active_ttt_games
    # No coins moved.
    assert await _economy.get_balance(opponent.id) == 0


async def test_stop_c4_with_wager_pays_opponent(db, _stub_edit_board):
    """c4 routes through the same helper as ttt — sanity that the helper
    parameter binding is correct for the second registry."""
    cog = AICog(bot=None)
    forfeiter = FakeMember(uid=1005)
    opponent = FakeMember(uid=1006)
    ctx = _ctx(forfeiter, channel_id=502)

    _state.active_c4_games[502] = {
        "players": [forfeiter.id, opponent.id],
        "amount": 100,
        "board": [[None] * 7 for _ in range(6)],
        "turn": 0,
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 502 not in _state.active_c4_games
    assert await _economy.get_balance(opponent.id) == 200


def _starter_chess_game(white_id: int, black_id: int, amount: int = 0) -> dict:
    from src.games import chess_engine
    pgn = (
        '[Event "Discord chess"]\n[Site "Discord"]\n[Date "2026.05.16"]\n'
        '[Round "?"]\n[White "Alice"]\n[Black "Bob"]\n[Result "*"]\n\n*\n'
    )
    return {
        "fen": chess_engine.STARTING_FEN,
        "pgn": pgn,
        "white_id": white_id,
        "black_id": black_id,
        "current_id": white_id,
        "amount": amount,
        "last_move": "",
        "board_msg_id": None,
    }


async def test_stop_chess_as_black_with_wager_pays_white_and_archives_report(db, _stub_edit_board):
    cog = AICog(bot=None)
    white = FakeMember(uid=1007)
    black = FakeMember(uid=1008)
    ctx = _ctx(black, channel_id=503)

    _state.active_chess_games[503] = _starter_chess_game(white.id, black.id, amount=50)

    await cog.cmd_stop.callback(cog, ctx)

    assert 503 not in _state.active_chess_games
    # Wager pot (50 * 2 = 100) goes to opponent (white).
    assert await _economy.get_balance(white.id) == 100
    # Forfeit produced a chess_reports row so `!chess view <id>` works.
    from src.persistence import load_chess_report
    report = await load_chess_report(1)
    assert report is not None
    assert report["winner_id"] == white.id
    assert report["result"] == "1-0"
    assert '[Result "1-0"]' in report["pgn"]


async def test_stop_chess_as_white_with_wager_pays_black(db, _stub_edit_board):
    cog = AICog(bot=None)
    white = FakeMember(uid=1009)
    black = FakeMember(uid=1010)
    ctx = _ctx(white, channel_id=504)

    _state.active_chess_games[504] = _starter_chess_game(white.id, black.id, amount=50)

    await cog.cmd_stop.callback(cog, ctx)

    assert 504 not in _state.active_chess_games
    assert await _economy.get_balance(black.id) == 100
    from src.persistence import load_chess_report
    report = await load_chess_report(1)
    assert report is not None
    assert report["winner_id"] == black.id
    assert report["result"] == "0-1"


async def test_stop_pvp_game_in_other_channel_returns_none(db, _stub_edit_board):
    """Helper short-circuits when no game in this cid → no payout, no edit."""
    cog = AICog(bot=None)
    user = FakeMember(uid=1011)
    ctx = _ctx(user, channel_id=505)
    # Game registered for OTHER channel.
    _state.active_ttt_games[999] = {
        "players": [user.id, FakeMember(uid=1012).id],
        "amount": 100,
        "board": [None] * 9,
        "turn": 0,
    }

    await cog.cmd_stop.callback(cog, ctx)

    # Other-channel game untouched.
    assert 999 in _state.active_ttt_games
    # No board edit.
    assert _stub_edit_board == []


async def test_stop_pvp_game_user_not_in_players_returns_none(db, _stub_edit_board):
    """Helper short-circuits when the user isn't in players[]."""
    cog = AICog(bot=None)
    bystander = FakeMember(uid=1013)
    p1 = FakeMember(uid=1014)
    p2 = FakeMember(uid=1015)
    ctx = _ctx(bystander, channel_id=506)
    _state.active_ttt_games[506] = {
        "players": [p1.id, p2.id],
        "amount": 100,
        "board": [None] * 9,
        "turn": 0,
    }

    await cog.cmd_stop.callback(cog, ctx)

    # Game still active; bystander isn't a player so no forfeit happened.
    assert 506 in _state.active_ttt_games
    assert _stub_edit_board == []


# ── Race (multi-player split, separate branch) ────────────────────────────────

async def test_stop_race_splits_pot_among_opponents(db, _stub_edit_board):
    cog = AICog(bot=None)
    forfeiter = FakeMember(uid=2001)
    opp1 = FakeMember(uid=2002)
    opp2 = FakeMember(uid=2003)
    ctx = _ctx(forfeiter, channel_id=600)

    _state.active_race_games[600] = {
        "players": [forfeiter.id, opp1.id, opp2.id],
        "amount": 100,  # total wager pot = 300; share = 300 // 2 = 150
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 600 not in _state.active_race_games
    assert await _economy.get_balance(opp1.id) == 150
    assert await _economy.get_balance(opp2.id) == 150


async def test_stop_race_no_wager_skips_payout(db, _stub_edit_board):
    cog = AICog(bot=None)
    forfeiter = FakeMember(uid=2004)
    opp = FakeMember(uid=2005)
    ctx = _ctx(forfeiter, channel_id=601)

    _state.active_race_games[601] = {
        "players": [forfeiter.id, opp.id],
        "amount": 0,
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 601 not in _state.active_race_games
    assert await _economy.get_balance(opp.id) == 0


# ── Blackjack (solo game, no payout — wager is dropped) ───────────────────────

async def test_stop_blackjack_drops_wager(db, _stub_edit_board):
    cog = AICog(bot=None)
    user = FakeMember(uid=3001)
    ctx = _ctx(user, channel_id=700)
    _state.active_blackjack_games[user.id] = {
        "amount": 500,
        "channel_id": 700,
        "deck": [], "player_hand": [], "dealer_hand": [],
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert user.id not in _state.active_blackjack_games


# ── Hangman (host can stop; word revealed in summary) ─────────────────────────

async def test_stop_hangman_by_host_clears_game(db, _stub_edit_board):
    cog = AICog(bot=None)
    host = FakeMember(uid=4001)
    ctx = _ctx(host, channel_id=800)
    _state.active_hangman_games[800] = {
        "user_id": host.id,
        "word": "python",
        "guessed_letters": set(),
        "wrong_guesses": 0,
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 800 not in _state.active_hangman_games


async def test_stop_hangman_non_host_does_nothing(db, _stub_edit_board):
    """Hangman is per-channel; only the host (user_id) can stop it."""
    cog = AICog(bot=None)
    host = FakeMember(uid=4002)
    intruder = FakeMember(uid=4003)
    ctx = _ctx(intruder, channel_id=801)
    _state.active_hangman_games[801] = {
        "user_id": host.id,
        "word": "python",
        "guessed_letters": set(),
        "wrong_guesses": 0,
    }

    await cog.cmd_stop.callback(cog, ctx)

    # Game still active.
    assert 801 in _state.active_hangman_games


# ── AI thread (owner closes; invited just leaves) ─────────────────────────────

async def test_stop_ai_thread_by_owner_pops_thread(db, _stub_edit_board):
    cog = AICog(bot=None)
    owner = FakeMember(uid=5001)
    ctx = _ctx(owner, channel_id=900)
    _state.ai_threads[900] = {
        "kind": "story",
        "owner_id": owner.id,
        "invited_ids": {owner.id},
        "history": [],
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 900 not in _state.ai_threads


async def test_stop_ai_thread_by_invited_user_just_leaves_group(db, _stub_edit_board):
    """An invited (non-owner) user calling !stop should be removed from
    invited_ids but the thread stays open."""
    cog = AICog(bot=None)
    owner = FakeMember(uid=5002)
    invited = FakeMember(uid=5003)
    ctx = _ctx(invited, channel_id=901)
    _state.ai_threads[901] = {
        "kind": "ask",
        "owner_id": owner.id,
        "invited_ids": {owner.id, invited.id},
        "history": [],
    }

    await cog.cmd_stop.callback(cog, ctx)

    # Thread still active; invited user removed from group.
    assert 901 in _state.ai_threads
    assert invited.id not in _state.ai_threads[901]["invited_ids"]
    assert owner.id in _state.ai_threads[901]["invited_ids"]


# ── Puzzle (host or admin) ────────────────────────────────────────────────────

async def test_stop_puzzle_by_host_cancels(db, _stub_edit_board):
    cog = AICog(bot=None)
    host = FakeMember(uid=6001)
    ctx = _ctx(host, channel_id=1000)
    _state.active_puzzles[1000] = {
        "user_id": host.id, "answer": "echo", "reward": 100,
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 1000 not in _state.active_puzzles


async def test_stop_puzzle_by_non_host_does_not_cancel(db, _stub_edit_board):
    cog = AICog(bot=None)
    host = FakeMember(uid=6002)
    intruder = FakeMember(uid=6003)
    ctx = _ctx(intruder, channel_id=1001)
    _state.active_puzzles[1001] = {
        "user_id": host.id, "answer": "echo", "reward": 100,
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 1001 in _state.active_puzzles


# ── No active anything ────────────────────────────────────────────────────────

async def test_stop_in_idle_channel_replies_nothing_to_stop(db, _stub_edit_board):
    cog = AICog(bot=None)
    user = FakeMember(uid=7001)
    ctx = _ctx(user, channel_id=1100)

    await cog.cmd_stop.callback(cog, ctx)

    # Nothing-to-stop reply emitted via ctx.send.
    assert ctx.sent_embeds, "Expected an embed reply"
    embed = ctx.sent_embeds[0]
    assert "Nothing to Stop" in embed.title


# ── Multiple activities at once ───────────────────────────────────────────────

async def test_stop_clears_multiple_simultaneous_activities(db, _stub_edit_board):
    """One !stop in a channel where the user has both an AI thread AND a
    blackjack game pops both. Tests that the cmd_stop loop doesn't bail
    after the first hit."""
    cog = AICog(bot=None)
    user = FakeMember(uid=8001)
    ctx = _ctx(user, channel_id=1200)
    # AI thread on this channel
    _state.ai_threads[1200] = {
        "kind": "ask", "owner_id": user.id,
        "invited_ids": {user.id}, "history": [],
    }
    # Blackjack game owned by this uid (not channel-keyed)
    _state.active_blackjack_games[user.id] = {
        "amount": 250, "channel_id": 1200,
        "deck": [], "player_hand": [], "dealer_hand": [],
    }

    await cog.cmd_stop.callback(cog, ctx)

    assert 1200 not in _state.ai_threads
    assert user.id not in _state.active_blackjack_games
