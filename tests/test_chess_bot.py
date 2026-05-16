"""Stockfish bot-opponent tests: chess_bot helpers + cog integration.

Engine-spawning tests (pick_move) skip cleanly when the stockfish binary
isn't on PATH. CI's Docker image installs it via apt; local dev usually
won't have it. The cog tests stub chess_bot.pick_move so they exercise the
full move pipeline (mutate → save → render → bump → reply) without a real
engine subprocess.
"""
import asyncio
import shutil
from types import SimpleNamespace

import chess
import pytest

import src.state as _state
import src.cogs.ai_cog as _ai_cog
from src.games import chess_bot, chess_engine
from src.games.chess import ChessCog, _initial_pgn

from tests.fakes.discord import FakeMember, FakeGuild, FakeCtx, FakeMessage


_aio = pytest.mark.asyncio
_STOCKFISH_AVAILABLE = shutil.which("stockfish") is not None


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers — no engine spawn
# ─────────────────────────────────────────────────────────────────────────────


class TestClampElo:
    def test_below_min(self):
        assert chess_bot.clamp_elo(50) == chess_bot.ELO_MIN
        assert chess_bot.clamp_elo(-100) == chess_bot.ELO_MIN

    def test_above_max(self):
        assert chess_bot.clamp_elo(5000) == chess_bot.ELO_MAX
        assert chess_bot.clamp_elo(chess_bot.ELO_MAX + 1) == chess_bot.ELO_MAX

    def test_inside_range(self):
        for elo in (100, 500, 1320, 2000, 3190):
            assert chess_bot.clamp_elo(elo) == elo

    def test_boundary_values(self):
        assert chess_bot.clamp_elo(chess_bot.ELO_MIN) == chess_bot.ELO_MIN
        assert chess_bot.clamp_elo(chess_bot.ELO_MAX) == chess_bot.ELO_MAX


class TestResolveStockfishPath:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("STOCKFISH_PATH", "/custom/path/sf")
        # Even if shutil.which finds something, env wins.
        monkeypatch.setattr(chess_bot.shutil, "which", lambda _: "/usr/bin/stockfish")
        assert chess_bot._resolve_stockfish_path() == "/custom/path/sf"

    def test_path_lookup_when_no_env(self, monkeypatch):
        monkeypatch.delenv("STOCKFISH_PATH", raising=False)
        monkeypatch.setattr(chess_bot.shutil, "which", lambda _: "/usr/local/bin/stockfish")
        assert chess_bot._resolve_stockfish_path() == "/usr/local/bin/stockfish"

    def test_debian_fallback_when_neither_set(self, monkeypatch):
        """Bug we shipped on the first try: relying on PATH alone fails in
        Docker because /usr/games is not on the default non-login PATH."""
        monkeypatch.delenv("STOCKFISH_PATH", raising=False)
        monkeypatch.setattr(chess_bot.shutil, "which", lambda _: None)
        assert chess_bot._resolve_stockfish_path() == "/usr/games/stockfish"

    def test_empty_env_treated_as_unset(self, monkeypatch):
        """STOCKFISH_PATH='' should not be treated as an explicit override."""
        monkeypatch.setenv("STOCKFISH_PATH", "")
        monkeypatch.setattr(chess_bot.shutil, "which", lambda _: "/usr/bin/stockfish")
        assert chess_bot._resolve_stockfish_path() == "/usr/bin/stockfish"


class TestSkillLevelForElo:
    def test_floor_maps_to_zero(self):
        assert chess_bot.skill_level_for_elo(100) == 0

    def test_ceiling_maps_to_twenty(self):
        # The ceiling for the sub-native map is one Elo below native floor.
        assert chess_bot.skill_level_for_elo(chess_bot.STOCKFISH_NATIVE_ELO_MIN - 1) == 20

    def test_monotonic_in_range(self):
        prev = -1
        for elo in range(chess_bot.ELO_MIN, chess_bot.STOCKFISH_NATIVE_ELO_MIN, 60):
            cur = chess_bot.skill_level_for_elo(elo)
            assert cur >= prev, f"skill level not monotonic at elo={elo}"
            prev = cur

    def test_within_bounds(self):
        for elo in range(chess_bot.ELO_MIN, chess_bot.STOCKFISH_NATIVE_ELO_MIN, 60):
            sl = chess_bot.skill_level_for_elo(elo)
            assert 0 <= sl <= 20


# ─────────────────────────────────────────────────────────────────────────────
# Engine spawn — actually run stockfish (CI only)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _STOCKFISH_AVAILABLE, reason="stockfish binary not on PATH")
@_aio
async def test_pick_move_returns_legal_move():
    """pick_move at native Elo returns a legal first move from the starting position."""
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 2000)
    board = chess.Board()
    assert move in board.legal_moves


@pytest.mark.skipif(not _STOCKFISH_AVAILABLE, reason="stockfish binary not on PATH")
@_aio
async def test_pick_move_sub_native_elo_returns_legal_move():
    """pick_move at sub-native Elo (Skill Level mapping) still returns legal."""
    move = await chess_bot.pick_move(chess_engine.STARTING_FEN, 500)
    board = chess.Board()
    assert move in board.legal_moves


# ─────────────────────────────────────────────────────────────────────────────
# Cog integration — bot-mention branch in cmd_chess
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_chess_state():
    yield
    _state.active_chess_games.clear()


@pytest.fixture(autouse=True)
def _stub_chess_helpers(monkeypatch):
    """Mirror of the autouse stub in test_chess.py — chess.py uses _bump_board
    + _delete_after + check_chess_channel. Stub all three for cog tests."""
    import src.games.chess as _chess_mod
    bump_calls = []
    async def _stub_bump(channel, game, embed, *, file=None):
        game["board_msg_id"] = (game.get("board_msg_id") or 0) + 1
        bump_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_chess_mod, "_bump_board", _stub_bump)

    async def _noop(_msg):
        return None
    monkeypatch.setattr(_chess_mod, "_delete_after", _noop)

    async def _allow(_ctx):
        return False
    monkeypatch.setattr(_chess_mod, "check_chess_channel", _allow)

    edit_calls = []
    async def _stub_edit(channel, game, embed, *, file=None):
        edit_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_ai_cog, "_edit_board", _stub_edit)
    monkeypatch.setattr(_ai_cog, "_bump_chess_board", _stub_bump)
    return bump_calls


def _make_bot_cog(bot_user_id: int = 999_000_001) -> ChessCog:
    """A ChessCog whose self.bot.user.id matches the given id so the
    cmd_chess bot-mention branch fires when a member with that id is mentioned."""
    fake_bot_user = SimpleNamespace(id=bot_user_id)
    fake_bot = SimpleNamespace(user=fake_bot_user)
    return ChessCog(bot=fake_bot)


def _ctx_for(member: FakeMember, channel_id: int, *, mentions=()) -> FakeCtx:
    g = FakeGuild(gid=42)
    g.members.append(member)
    for m in mentions:
        g.members.append(m)
    ctx = FakeCtx(author=member, guild=g)
    ctx.channel.id = channel_id
    ctx.message = FakeMessage(author=member)
    ctx.message.mentions = list(mentions)
    return ctx


@_aio
async def test_cmd_chess_bot_mention_default_elo(db, _stub_chess_helpers):
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2000, display_name="Alice")
    bot_member = FakeMember(uid=cog.bot.user.id, display_name="TheBot")
    ctx = _ctx_for(challenger, channel_id=1000, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx)

    assert 1000 in _state.active_chess_games
    g = _state.active_chess_games[1000]
    assert g["white_id"] == challenger.id
    assert g["black_id"] == cog.bot.user.id
    assert g["current_id"] == challenger.id
    assert g["elo"] == chess_bot.ELO_DEFAULT
    assert g["amount"] == 0


@_aio
async def test_cmd_chess_bot_mention_custom_elo(db, _stub_chess_helpers):
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2001)
    bot_member = FakeMember(uid=cog.bot.user.id)
    ctx = _ctx_for(challenger, channel_id=1001, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx, "1500")

    assert _state.active_chess_games[1001]["elo"] == 1500


@_aio
async def test_cmd_chess_bot_mention_elo_below_min_rejected(db, _stub_chess_helpers):
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2002)
    bot_member = FakeMember(uid=cog.bot.user.id)
    ctx = _ctx_for(challenger, channel_id=1002, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx, "50")

    # No game created; error embed sent.
    assert 1002 not in _state.active_chess_games
    assert len(ctx.sent_embeds) >= 1
    assert "Invalid Elo" in ctx.sent_embeds[-1].title


@_aio
async def test_cmd_chess_bot_mention_elo_above_max_rejected(db, _stub_chess_helpers):
    cog = _make_bot_cog()
    challenger = FakeMember(uid=2003)
    bot_member = FakeMember(uid=cog.bot.user.id)
    ctx = _ctx_for(challenger, channel_id=1003, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx, "9999")

    assert 1003 not in _state.active_chess_games
    assert "Invalid Elo" in ctx.sent_embeds[-1].title


@_aio
async def test_cmd_chess_bot_mention_skips_setup_pvp_game(db, _stub_chess_helpers, monkeypatch):
    """Bot games must NOT route through _setup_pvp_game (no confirmation, no
    opponent balance, no wager)."""
    import src.games.chess as _chess_mod
    setup_called = []
    async def _spy(*args, **kwargs):
        setup_called.append((args, kwargs))
        return True
    monkeypatch.setattr(_chess_mod, "_setup_pvp_game", _spy)

    cog = _make_bot_cog()
    challenger = FakeMember(uid=2004)
    bot_member = FakeMember(uid=cog.bot.user.id)
    ctx = _ctx_for(challenger, channel_id=1004, mentions=[bot_member])

    await cog.cmd_chess.callback(cog, ctx)

    assert 1004 in _state.active_chess_games
    assert setup_called == [], "_setup_pvp_game should not be called for bot games"


@_aio
async def test_cmd_chess_human_opponent_still_goes_through_setup(db, _stub_chess_helpers, monkeypatch):
    """Regression: mentioning a non-bot user still routes through _setup_pvp_game
    (trailing int treated as a wager, not an Elo)."""
    import src.games.chess as _chess_mod
    setup_called = []
    async def _spy(ctx, opponent, amount, invite_title):
        setup_called.append({"opponent": opponent, "amount": amount})
        return True
    monkeypatch.setattr(_chess_mod, "_setup_pvp_game", _spy)

    cog = _make_bot_cog(bot_user_id=999_000_002)
    challenger = FakeMember(uid=2005)
    human_opp = FakeMember(uid=2006)
    ctx = _ctx_for(challenger, channel_id=1005, mentions=[human_opp])

    await cog.cmd_chess.callback(cog, ctx, "500")

    assert len(setup_called) == 1
    assert setup_called[0]["opponent"] is human_opp
    assert setup_called[0]["amount"] == 500
    assert _state.active_chess_games[1005]["amount"] == 500
    assert "elo" not in _state.active_chess_games[1005]


# ─────────────────────────────────────────────────────────────────────────────
# Cog integration — _play_bot_reply fires after a human move against the bot
# ─────────────────────────────────────────────────────────────────────────────


def _seed_bot_chess_game(channel_id: int, white_id: int, bot_id: int, *, elo: int = 1320,
                         fen: str | None = None, current_id: int | None = None):
    starting_fen = fen if fen is not None else chess_engine.STARTING_FEN
    _state.active_chess_games[channel_id] = {
        "fen": starting_fen,
        "pgn": _initial_pgn("White", f"Stockfish ({elo} Elo)", None, starting_fen=starting_fen),
        "white_id": white_id,
        "black_id": bot_id,
        "current_id": current_id if current_id is not None else white_id,
        "amount": 0,
        "elo": elo,
        "last_move": "",
        "board_msg_id": 1000,
    }


@_aio
async def test_human_move_triggers_bot_reply(db, _stub_chess_helpers, monkeypatch):
    """After the human plays, _play_bot_reply runs (stubbed pick_move) and
    advances the board with Stockfish's move."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2100, display_name="Alice")
    _seed_bot_chess_game(1100, human.id, cog.bot.user.id)

    # Make sure ctx.guild knows about the bot user for mention rendering.
    ctx = _ctx_for(human, channel_id=1100)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    # Stub Stockfish to always play e7e5.
    async def _stub_pick(fen, elo):
        return chess.Move.from_uci("e7e5")
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")
    # _play_bot_reply runs as a background task — let the event loop drain it.
    await asyncio.sleep(0)
    # Pending tasks may need another nudge to fully complete.
    for _ in range(5):
        await asyncio.sleep(0)

    g = _state.active_chess_games[1100]
    # Both moves applied; back to white's turn.
    assert g["current_id"] == human.id
    assert "e4" in g["pgn"] and "e5" in g["pgn"]
    # FEN now reflects post-e5 position.
    assert g["fen"].startswith("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w")


@_aio
async def test_bot_reply_handles_engine_error(db, _stub_chess_helpers, monkeypatch):
    """If Stockfish raises, the user sees a friendly error and the game state
    isn't half-committed for the bot's turn."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2101)
    _seed_bot_chess_game(1101, human.id, cog.bot.user.id)
    ctx = _ctx_for(human, channel_id=1101)
    ctx.guild.members.append(FakeMember(uid=cog.bot.user.id))

    async def _broken_pick(fen, elo):
        raise RuntimeError("simulated stockfish crash")
    monkeypatch.setattr(chess_bot, "pick_move", _broken_pick)

    # Capture channel.send (used by the error path inside _play_bot_reply).
    sent = []
    async def _record_send(*args, **kwargs):
        sent.append((args, kwargs))
        return FakeMessage()
    ctx.channel.send = _record_send

    await cog.cmd_move_chess.callback(cog, ctx, "e4")
    for _ in range(5):
        await asyncio.sleep(0)

    # User's move applied; bot's didn't; current_id is still bot (waiting on stockfish).
    g = _state.active_chess_games[1101]
    assert g["current_id"] == cog.bot.user.id
    assert "e4" in g["pgn"]
    # Error message was posted to the channel.
    assert any(
        kwargs.get("embed") is not None and "Stockfish" in kwargs["embed"].title
        for _, kwargs in sent
    )


@_aio
async def test_bot_reply_no_op_when_game_ended(db, _stub_chess_helpers, monkeypatch):
    """If the human resigns between !move and the bot's reply, _play_bot_reply
    silently returns rather than crashing."""
    cog = _make_bot_cog()
    human = FakeMember(uid=2102)
    _seed_bot_chess_game(1102, human.id, cog.bot.user.id)
    # Don't even queue the bot reply — directly invoke _play_bot_reply after
    # clearing state to simulate the race.
    _state.active_chess_games.pop(1102, None)

    channel = FakeCtx(author=human).channel
    channel.id = 1102

    # Should not raise.
    await cog._play_bot_reply(channel, 1102)


@_aio
async def test_bot_reply_does_not_fire_for_pvp_game(db, _stub_chess_helpers, monkeypatch):
    """Regression: a PvP move between two humans must NOT trigger _play_bot_reply."""
    cog = _make_bot_cog()
    white = FakeMember(uid=2200)
    black = FakeMember(uid=2201)
    # PvP game seed — no 'elo' key, opponent_id != bot.user.id
    _state.active_chess_games[1200] = {
        "fen": chess_engine.STARTING_FEN,
        "pgn": _initial_pgn("White", "Black", None),
        "white_id": white.id,
        "black_id": black.id,
        "current_id": white.id,
        "amount": 0,
        "last_move": "",
        "board_msg_id": 555,
    }
    ctx = _ctx_for(white, channel_id=1200)
    ctx.guild.members.append(black)

    pick_called = []
    async def _stub_pick(fen, elo):
        pick_called.append((fen, elo))
        return chess.Move.from_uci("e7e5")
    monkeypatch.setattr(chess_bot, "pick_move", _stub_pick)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")
    for _ in range(3):
        await asyncio.sleep(0)

    assert pick_called == [], "pick_move should not be called in PvP games"
    # Black's turn now, as expected for a PvP move.
    assert _state.active_chess_games[1200]["current_id"] == black.id
