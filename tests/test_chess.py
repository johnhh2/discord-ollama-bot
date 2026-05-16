"""Tests for the chess overhaul: engine wrapper, cog flow, reports, view command.

The cog tests use the `db` fixture so save_chess_game / save_chess_report
exercise real SQL against the in-memory SQLite. Render-PNG paths are not
exercised here because cairosvg requires libcairo, which CI installs but
dev machines may not; render failures fall through to a text-only embed
and the assertions target state, not rendered bytes.
"""
import chess
import pytest

import src.state as _state
import src.cogs.ai_cog as _ai_cog
from src.games import chess_engine
from src.games.chess import (
    ChessCog,
    _append_san_to_pgn,
    _initial_pgn,
)
from src.persistence import load_chess_report

from tests.fakes.discord import FakeMember, FakeGuild, FakeCtx, FakeMessage


_aio = pytest.mark.asyncio


# ─────────────────────────────────────────────────────────────────────────────
# Engine wrapper — chess_engine.try_move + game_over_info
# ─────────────────────────────────────────────────────────────────────────────


class TestTryMove:
    def test_san_legal(self):
        b = chess_engine.new_board()
        m, err = chess_engine.try_move(b, "e4")
        assert err is None
        assert m == chess.Move.from_uci("e2e4")

    def test_uci_legal(self):
        b = chess_engine.new_board()
        m, err = chess_engine.try_move(b, "g1f3")
        assert err is None
        assert m == chess.Move.from_uci("g1f3")

    def test_uci_uppercase_normalized(self):
        b = chess_engine.new_board()
        m, err = chess_engine.try_move(b, "G1F3")
        assert err is None and m is not None

    def test_empty_input(self):
        b = chess_engine.new_board()
        m, err = chess_engine.try_move(b, "")
        assert m is None and "No move" in err

    def test_san_illegal_for_position(self):
        # Knight to f6 is for black; from white-to-move start it's illegal.
        b = chess_engine.new_board()
        m, err = chess_engine.try_move(b, "Nf6")
        assert m is None and "legal" in err.lower()

    def test_uci_illegal_returns_legal_msg_not_parse_error(self):
        b = chess_engine.new_board()
        # e2-e5 is a syntactically valid UCI but illegal (pawn can't jump 3).
        m, err = chess_engine.try_move(b, "e2e5")
        assert m is None
        assert "legal" in err.lower()

    def test_garbage_returns_parse_error(self):
        b = chess_engine.new_board()
        m, err = chess_engine.try_move(b, "xyz")
        assert m is None and "parse" in err.lower()

    def test_ambiguous_san(self):
        # Two knights on d2 and f2, both can go to e4.
        b = chess_engine.board_from_fen("4k3/8/8/8/8/8/3N1N2/4K3 w - - 0 1")
        m, err = chess_engine.try_move(b, "Ne4")
        assert m is None and "ambig" in err.lower()

    def test_castling_san(self):
        b = chess_engine.board_from_fen("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
        m, err = chess_engine.try_move(b, "O-O")
        assert err is None and m is not None
        assert m == chess.Move.from_uci("e1g1")

    def test_promotion_uci(self):
        b = chess_engine.board_from_fen("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        m, err = chess_engine.try_move(b, "a7a8q")
        assert err is None
        assert m == chess.Move.from_uci("a7a8q")


class TestGameOver:
    def test_in_progress(self):
        b = chess_engine.new_board()
        result, reason = chess_engine.game_over_info(b)
        assert result is None and reason is None

    def test_fools_mate(self):
        b = chess_engine.new_board()
        for san in ["f3", "e5", "g4", "Qh4"]:
            m, _ = chess_engine.try_move(b, san)
            chess_engine.push_with_san(b, m)
        result, reason = chess_engine.game_over_info(b)
        assert result == "0-1"
        assert reason == "checkmate"
        assert chess_engine.winner_color(b) is False

    def test_stalemate(self):
        # K vs K+Q stalemate position: black king on a8, white queen on b6, white king elsewhere.
        b = chess_engine.board_from_fen("k7/8/1Q6/8/8/8/8/7K b - - 0 1")
        result, reason = chess_engine.game_over_info(b)
        assert result == "1/2-1/2"
        assert reason == "stalemate"
        assert chess_engine.winner_color(b) is None

    def test_insufficient_material(self):
        # K+B vs K — insufficient.
        b = chess_engine.board_from_fen("4k3/8/8/8/8/8/8/4KB2 w - - 0 1")
        result, reason = chess_engine.game_over_info(b)
        assert result == "1/2-1/2"
        assert "insufficient" in reason


class TestPushWithSan:
    def test_returns_san_before_mutation(self):
        # SAN of g1f3 from starting position is "Nf3".
        b = chess_engine.new_board()
        m = chess.Move.from_uci("g1f3")
        san = chess_engine.push_with_san(b, m)
        assert san == "Nf3"
        assert b.peek() == m


# ─────────────────────────────────────────────────────────────────────────────
# PGN helpers
# ─────────────────────────────────────────────────────────────────────────────


class TestPgnHelpers:
    def test_initial_pgn_has_headers(self):
        pgn = _initial_pgn("Alice", "Bob", "My Guild")
        assert '[White "Alice"]' in pgn
        assert '[Black "Bob"]' in pgn
        assert '[Result "*"]' in pgn
        assert "My Guild" in pgn

    def test_append_san_writes_move(self):
        pgn = _initial_pgn("Alice", "Bob", None)
        pgn2 = _append_san_to_pgn(pgn, "e4")
        assert "1. e4" in pgn2

    def test_append_full_game_numbering(self):
        pgn = _initial_pgn("A", "B", None)
        for san in ["e4", "e5", "Nf3", "Nc6"]:
            pgn = _append_san_to_pgn(pgn, san)
        # Move numbering 1. and 2. both present.
        assert "1. e4 e5" in pgn
        assert "2. Nf3 Nc6" in pgn


# ─────────────────────────────────────────────────────────────────────────────
# ChessCog: !move flow + game-over -> chess_reports
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_chess_state():
    yield
    _state.active_chess_games.clear()


@pytest.fixture(autouse=True)
def _stub_chess_edit_board(monkeypatch):
    """Both the chess cog and ai_cog import _edit_board from src.helpers. Stub
    both bound names so cog.cmd_move_chess doesn't try to fetch_message."""
    import src.games.chess as _chess_mod
    edit_calls = []
    async def _stub(channel, game, embed, *, file=None):
        edit_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_chess_mod, "_edit_board", _stub)
    monkeypatch.setattr(_ai_cog, "_edit_board", _stub)
    return edit_calls


@pytest.fixture(autouse=True)
def _stub_delete_after(monkeypatch):
    import src.games.chess as _chess_mod
    async def _noop(_msg):
        return None
    monkeypatch.setattr(_chess_mod, "_delete_after", _noop)


@pytest.fixture(autouse=True)
def _stub_check_chess_channel(monkeypatch):
    """check_chess_channel reads guild settings; bypass it for tests."""
    import src.games.chess as _chess_mod
    async def _allow(_ctx):
        return False
    monkeypatch.setattr(_chess_mod, "check_chess_channel", _allow)


def _ctx_for(member: FakeMember, channel_id: int = 700, guild: FakeGuild | None = None) -> FakeCtx:
    g = guild or FakeGuild(gid=42)
    g.members.append(member)
    ctx = FakeCtx(author=member, guild=g)
    ctx.channel.id = channel_id
    ctx.message = FakeMessage(author=member)
    return ctx


def _seed_chess_game(channel_id: int, white_id: int, black_id: int, amount: int = 0):
    _state.active_chess_games[channel_id] = {
        "fen": chess_engine.STARTING_FEN,
        "pgn": _initial_pgn("White", "Black", None),
        "white_id": white_id,
        "black_id": black_id,
        "current_id": white_id,
        "amount": amount,
        "last_move": "",
        "board_msg_id": 12345,
    }


@_aio
async def test_move_rejects_when_not_your_turn(db, _stub_chess_edit_board):
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1100, display_name="White")
    black = FakeMember(uid=1101, display_name="Black")
    _seed_chess_game(700, white.id, black.id)
    ctx = _ctx_for(black, channel_id=700)
    ctx.guild.members.append(white)

    await cog.cmd_move_chess.callback(cog, ctx, "e7e5")

    # State unchanged: still white's turn.
    g = _state.active_chess_games[700]
    assert g["current_id"] == white.id
    assert g["fen"] == chess_engine.STARTING_FEN


@_aio
async def test_move_rejects_illegal_move(db, _stub_chess_edit_board):
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1102, display_name="White")
    black = FakeMember(uid=1103, display_name="Black")
    _seed_chess_game(701, white.id, black.id)
    ctx = _ctx_for(white, channel_id=701)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "e2e5")  # pawn can't jump 3

    g = _state.active_chess_games[701]
    assert g["current_id"] == white.id
    assert g["fen"] == chess_engine.STARTING_FEN


@_aio
async def test_move_applies_and_flips_turn(db, _stub_chess_edit_board):
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1104, display_name="White")
    black = FakeMember(uid=1105, display_name="Black")
    _seed_chess_game(702, white.id, black.id)
    ctx = _ctx_for(white, channel_id=702)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")

    g = _state.active_chess_games[702]
    assert g["current_id"] == black.id
    assert g["fen"] != chess_engine.STARTING_FEN
    assert "e4" in g["pgn"]


@_aio
async def test_checkmate_creates_report_and_deletes_active_game(db, _stub_chess_edit_board):
    """Play fool's mate and verify a chess_reports row is created and the
    chess_games row is deleted."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1106, display_name="White")
    black = FakeMember(uid=1107, display_name="Black")
    _seed_chess_game(703, white.id, black.id, amount=100)
    ctx_w = _ctx_for(white, channel_id=703)
    ctx_w.guild.members.append(black)
    ctx_b = _ctx_for(black, channel_id=703, guild=ctx_w.guild)

    # Fool's mate: f3, e5, g4, Qh4#
    await cog.cmd_move_chess.callback(cog, ctx_w, "f3")
    await cog.cmd_move_chess.callback(cog, ctx_b, "e5")
    await cog.cmd_move_chess.callback(cog, ctx_w, "g4")
    await cog.cmd_move_chess.callback(cog, ctx_b, "Qh4#")

    # Game removed from active state.
    assert 703 not in _state.active_chess_games

    # chess_reports row created with black as winner.
    report = await load_chess_report(1)
    assert report is not None
    assert report["winner_id"] == black.id
    assert report["result"] == "0-1"
    assert '[Result "0-1"]' in report["pgn"]


@_aio
async def test_stalemate_creates_draw_report(db, _stub_chess_edit_board):
    """A stalemate-in-one position. White plays Qb6 — black king on a8 has
    no legal moves and isn't in check. Report stores winner_id NULL, 1/2-1/2."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1108, display_name="W")
    black = FakeMember(uid=1109, display_name="B")
    _state.active_chess_games[704] = {
        "fen": "k7/8/K6Q/8/8/8/8/8 w - - 0 1",
        "pgn": '[Event "x"]\n[Site "Discord"]\n[Date "????.??.??"]\n[Round "?"]\n[White "W"]\n[Black "B"]\n[Result "*"]\n\n*\n',
        "white_id": white.id,
        "black_id": black.id,
        "current_id": white.id,
        "amount": 50,
        "last_move": "",
        "board_msg_id": 1,
    }
    ctx = _ctx_for(white, channel_id=704)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "Qb6")

    assert 704 not in _state.active_chess_games
    report = await load_chess_report(1)
    assert report is not None
    assert report["winner_id"] is None
    assert report["result"] == "1/2-1/2"


# ─────────────────────────────────────────────────────────────────────────────
# !chess view <id>
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_chess_view_missing_id(db):
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1200)
    ctx = _ctx_for(user, channel_id=800)

    await cog._cmd_view(ctx, ())

    # Sent an error embed.
    assert len(ctx.sent_embeds) == 1


@_aio
async def test_chess_view_nonexistent_report(db):
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1201)
    ctx = _ctx_for(user, channel_id=801)

    await cog._cmd_view(ctx, ("999999",))

    assert len(ctx.sent_embeds) == 1
    assert "Not Found" in ctx.sent_embeds[0].title


@_aio
async def test_chess_view_loads_report(db):
    """A report stored via save_chess_report is renderable via !chess view."""
    from src.persistence import save_chess_report
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=10, result="1-0",
        pgn='[Event "x"]\n[White "Alice"]\n[Black "Bob"]\n[Result "1-0"]\n\n1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0\n',
        final_fen="r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4",
    )

    cog = ChessCog(bot=None)
    user = FakeMember(uid=1202)
    ctx = _ctx_for(user, channel_id=802)

    await cog._cmd_view(ctx, (str(rid),))

    assert len(ctx.sent_embeds) == 1
    embed = ctx.sent_embeds[0]
    assert f"#{rid}" in embed.title
    assert "Qxf7#" in embed.description


# ─────────────────────────────────────────────────────────────────────────────
# Concurrency: gate-and-claim under interleaved !move invocations
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_concurrent_moves_only_one_applies(db, _stub_chess_edit_board, monkeypatch):
    """Per CLAUDE.md concurrency rules: cmd_move_chess must flip current_id
    synchronously before any await, so a racing second !move from the same
    user bails at the not-your-turn gate."""
    import asyncio
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1300, display_name="W")
    black = FakeMember(uid=1301, display_name="B")
    _seed_chess_game(900, white.id, black.id)
    ctx1 = _ctx_for(white, channel_id=900)
    ctx1.guild.members.append(black)
    ctx2 = _ctx_for(white, channel_id=900, guild=ctx1.guild)

    # Force save_chess_game to yield so two concurrent invocations can interleave.
    import src.games.chess as _chess_mod
    real_save = _chess_mod.save_chess_game
    async def _yielding_save(channel_id):
        await asyncio.sleep(0)  # real event-loop yield
        return await real_save(channel_id)
    monkeypatch.setattr(_chess_mod, "save_chess_game", _yielding_save)

    # Two simultaneous e4 invocations.
    await asyncio.gather(
        cog.cmd_move_chess.callback(cog, ctx1, "e4"),
        cog.cmd_move_chess.callback(cog, ctx2, "e4"),
    )

    g = _state.active_chess_games[900]
    # Exactly one move applied — board is at the post-e4 position, not post-e4-e4-then-something.
    expected_fen_prefix = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b"
    assert g["fen"].startswith(expected_fen_prefix), f"unexpected fen: {g['fen']}"
    assert g["current_id"] == black.id
