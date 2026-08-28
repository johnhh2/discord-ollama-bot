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
# describe_capture — used to annotate "**Last move:**" with what got taken
# ─────────────────────────────────────────────────────────────────────────────


class TestDescribeCapture:
    def test_non_capture_returns_none(self):
        b = chess_engine.new_board()
        m = chess.Move.from_uci("e2e4")
        assert chess_engine.describe_capture(b, m) is None

    def test_pawn_takes_pawn(self):
        # After 1.e4 d5 2.exd5 — white pawn captures black pawn.
        b = chess_engine.new_board()
        b.push_san("e4")
        b.push_san("d5")
        m = b.parse_san("exd5")
        assert chess_engine.describe_capture(b, m) == "captured Black's pawn"

    def test_knight_takes_bishop(self):
        # White knight on f3 jumps to e5, capturing a black bishop.
        b = chess.Board("4k3/8/8/4b3/8/5N2/8/4K3 w - - 0 1")
        m = b.parse_san("Nxe5")
        assert chess_engine.describe_capture(b, m) == "captured Black's bishop"

    def test_en_passant_captures_pawn(self):
        # White plays exd6 e.p. — captured pawn is on d5, not d6.
        b = chess.Board("rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 3")
        m = b.parse_san("exd6")
        assert b.is_en_passant(m)
        assert chess_engine.describe_capture(b, m) == "captured Black's pawn"

    def test_black_captures_white_queen(self):
        # Black knight takes a white queen.
        b = chess.Board("4k3/8/3n4/8/4Q3/8/8/4K3 b - - 0 1")
        m = b.parse_san("Nxe4")
        assert chess_engine.describe_capture(b, m) == "captured White's queen"


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


class TestMovetextOnly:
    def test_strips_headers(self):
        from src.games.chess import _movetext_only
        pgn = _initial_pgn("Alice", "Bob", "Test Guild")
        pgn = _append_san_to_pgn(pgn, "e4")
        out = _movetext_only(pgn)
        assert "1. e4" in out
        assert "[Event" not in out
        assert "[White" not in out
        assert "Alice" not in out
        assert "Test Guild" not in out


class TestLastMoveInfoFromPgn:
    """_last_move_info_from_pgn returns (last_move, was_capture) by replaying
    the PGN and checking is_capture on the pre-move board."""

    def test_empty_game_returns_none_false(self):
        from src.games.chess import _last_move_info_from_pgn
        m, cap = _last_move_info_from_pgn(_initial_pgn("A", "B", None))
        assert m is None and cap is False

    def test_non_capture_last_move(self):
        from src.games.chess import _last_move_info_from_pgn
        pgn = _initial_pgn("A", "B", None)
        pgn = _append_san_to_pgn(pgn, "e4")
        m, cap = _last_move_info_from_pgn(pgn)
        assert m == chess.Move.from_uci("e2e4")
        assert cap is False

    def test_capture_last_move(self):
        from src.games.chess import _last_move_info_from_pgn
        # 1.e4 d5 2.exd5 — the last move is a pawn capture.
        pgn = _initial_pgn("A", "B", None)
        for san in ("e4", "d5", "exd5"):
            pgn = _append_san_to_pgn(pgn, san)
        m, cap = _last_move_info_from_pgn(pgn)
        assert m == chess.Move.from_uci("e4d5")
        assert cap is True

    def test_en_passant_counts_as_capture(self):
        from src.games.chess import _last_move_info_from_pgn
        # 1.e4 a6 2.e5 d5 3.exd6 (en passant).
        pgn = _initial_pgn("A", "B", None)
        for san in ("e4", "a6", "e5", "d5", "exd6"):
            pgn = _append_san_to_pgn(pgn, san)
        m, cap = _last_move_info_from_pgn(pgn)
        assert cap is True

    def test_garbage_pgn_returns_none_false(self):
        from src.games.chess import _last_move_info_from_pgn
        m, cap = _last_move_info_from_pgn("not a real pgn")
        # python-chess parses garbage as an empty game; should not crash.
        assert m is None and cap is False


class TestCapturesSummary:
    """_captures_summary formats per-side captures as 'glyph,glyph+N'."""

    def test_no_captures_returns_empty(self):
        from src.games.chess import _captures_summary
        b = chess.Board()
        assert _captures_summary(b, chess.WHITE) == ""
        assert _captures_summary(b, chess.BLACK) == ""

    def test_single_pawn_capture(self):
        from src.games.chess import _captures_summary
        # 1.e4 d5 2.exd5 — white captured one black pawn. Black queen
        # didn't recapture (it's white's turn now).
        b = chess.Board()
        for san in ("e4", "d5", "exd5"):
            b.push_san(san)
        # White has captured one black pawn → glyph ♟, no '+N'.
        assert _captures_summary(b, chess.WHITE) == "♟"
        assert _captures_summary(b, chess.BLACK) == ""

    def test_two_captures_no_extra(self):
        from src.games.chess import _captures_summary
        # 1.e4 d5 2.exd5 Qxd5 — black queen recaptured. Now black has
        # captured one white pawn and white has captured one black pawn.
        b = chess.Board()
        for san in ("e4", "d5", "exd5", "Qxd5"):
            b.push_san(san)
        # Each side has one pawn capture.
        assert _captures_summary(b, chess.WHITE) == "♟"
        assert _captures_summary(b, chess.BLACK) == "♙"

    def test_higher_value_pieces_sort_first(self):
        from src.games.chess import _captures_summary
        # Construct a board by removing black queen, rook, and pawn from
        # the starting position — equivalent to white having captured
        # all three.
        b = chess.Board()
        b.remove_piece_at(chess.D8)  # black queen
        b.remove_piece_at(chess.A8)  # black rook
        b.remove_piece_at(chess.E7)  # black pawn
        # Top two by value: Q, R. Extra: 1 (the pawn).
        assert _captures_summary(b, chess.WHITE) == "♛,♜+1"

    def test_uses_opponent_color_glyphs(self):
        from src.games.chess import _captures_summary
        # White captured a black pawn → uses BLACK pawn glyph (♟).
        b = chess.Board()
        b.remove_piece_at(chess.E7)
        assert _captures_summary(b, chess.WHITE) == "♟"
        # Black captured a white pawn → uses WHITE pawn glyph (♙).
        b2 = chess.Board()
        b2.remove_piece_at(chess.E2)
        assert _captures_summary(b2, chess.BLACK) == "♙"

    def test_promotion_does_not_inflate_captures(self):
        """When the captor's pawn promotes, their own queen count goes up.
        That should NOT change the opponent's captured-from count, because
        the helper measures based on the OPPONENT'S remaining pieces."""
        from src.games.chess import _captures_summary
        # Realistic line: white plays a pawn from the starting position
        # through to a8 promotion without any captures by either side.
        # 1.a4 a5 2.h4 h5 ... we need black to not block the a-file. The
        # standard "rook-pawn race" doesn't promote without captures, so
        # we cheat slightly: hand-edit the board to remove black's a-pawn
        # and a-rook so the white pawn has a clear runway. That counts as
        # 2 captured black pieces for the test setup.
        b = chess.Board()
        b.remove_piece_at(chess.A7)  # treat as if white had captured this pawn
        b.remove_piece_at(chess.A8)  # treat as if white had captured this rook
        baseline = _captures_summary(b, chess.WHITE)
        assert baseline == "♜,♟"  # 1 rook + 1 pawn captured so far
        # Now walk a pawn from a2 → a8 with promotion. No further captures.
        for san in ("a4", "h6", "a5", "h5", "a6", "h4", "a7", "Rh6", "a8=Q"):
            b.push_san(san)
        # Captures unchanged: still 1 rook + 1 pawn taken from black.
        # White's promotion turned a white pawn into a white queen — no
        # impact on what black has lost.
        assert _captures_summary(b, chess.WHITE) == "♜,♟"

    def test_many_captures_top_two_plus_count(self):
        from src.games.chess import _captures_summary
        # Black has lost queen + 2 rooks + 3 pawns = 6 pieces captured by white.
        b = chess.Board()
        b.remove_piece_at(chess.D8)  # queen
        b.remove_piece_at(chess.A8)  # rook
        b.remove_piece_at(chess.H8)  # rook
        b.remove_piece_at(chess.A7)  # pawn
        b.remove_piece_at(chess.B7)  # pawn
        b.remove_piece_at(chess.C7)  # pawn
        # Top two: Q, R. Extra: 4 (1 rook + 3 pawns).
        assert _captures_summary(b, chess.WHITE) == "♛,♜+4"


class TestFormatSeconds:
    """_format_seconds auto-scales to s / m:ss / h:mm:ss based on duration."""

    def test_under_minute_uses_s_suffix(self):
        from src.games.chess import _format_seconds
        assert _format_seconds(0) == "0s"
        assert _format_seconds(12) == "12s"
        assert _format_seconds(59) == "59s"

    def test_under_hour_uses_m_ss(self):
        from src.games.chess import _format_seconds
        assert _format_seconds(60) == "1:00"
        assert _format_seconds(273) == "4:33"
        assert _format_seconds(3599) == "59:59"

    def test_hours_and_above_uses_h_mm_ss(self):
        from src.games.chess import _format_seconds
        assert _format_seconds(3600) == "1:00:00"
        assert _format_seconds(5110) == "1:25:10"
        assert _format_seconds(7325) == "2:02:05"

    def test_negative_clamped_to_zero(self):
        """Defensive: negative input (e.g. clock drift) renders as 0s."""
        from src.games.chess import _format_seconds
        assert _format_seconds(-5) == "0s"


class TestRecordTurnTime:
    """_record_turn_time stops the mover's clock and starts the opponent's."""

    def test_no_op_when_turn_started_at_unset(self):
        """First move ever: turn_started_at gets set, but no time is added
        because there's no baseline. Subsequent moves will tick properly."""
        from src.games.chess import _record_turn_time
        game = {"white_id": 1, "black_id": 2, "white_seconds": 0, "black_seconds": 0}
        _record_turn_time(game, mover_id=1)
        assert game["white_seconds"] == 0
        assert game["black_seconds"] == 0
        assert game["turn_started_at"] is not None

    def test_adds_elapsed_to_white_when_white_moves(self, monkeypatch):
        """White moves after 30 seconds of thinking → white_seconds += 30."""
        import time as _time
        from src.games.chess import _record_turn_time
        # Pin time.time to a constant "now" — turn was started at t=100,
        # current time is t=130, so elapsed = 30s.
        monkeypatch.setattr(_time, "time", lambda: 130.0)
        game = {
            "white_id": 1, "black_id": 2,
            "turn_started_at": 100, "white_seconds": 0, "black_seconds": 0,
        }
        _record_turn_time(game, mover_id=1)
        assert game["white_seconds"] == 30
        assert game["black_seconds"] == 0
        assert game["turn_started_at"] == 130  # opponent's clock starts now

    def test_adds_elapsed_to_black_when_black_moves(self, monkeypatch):
        import time as _time
        from src.games.chess import _record_turn_time
        monkeypatch.setattr(_time, "time", lambda: 250.0)
        game = {
            "white_id": 1, "black_id": 2,
            "turn_started_at": 200, "white_seconds": 5, "black_seconds": 10,
        }
        _record_turn_time(game, mover_id=2)
        assert game["white_seconds"] == 5
        assert game["black_seconds"] == 60  # 10 + (250 - 200)
        assert game["turn_started_at"] == 250

    def test_accumulates_across_multiple_turns(self, monkeypatch):
        import time as _time
        from src.games.chess import _record_turn_time
        # Sequence: turn started at 0, white moves at 10, black moves at 30,
        # white moves at 45 → white = 10 + 15 = 25; black = 20.
        clock = {"now": 10.0}
        monkeypatch.setattr(_time, "time", lambda: clock["now"])
        game = {
            "white_id": 1, "black_id": 2,
            "turn_started_at": 0, "white_seconds": 0, "black_seconds": 0,
        }
        _record_turn_time(game, mover_id=1)  # white moved at t=10 (10s)
        clock["now"] = 30.0
        _record_turn_time(game, mover_id=2)  # black moved at t=30 (+20s)
        clock["now"] = 45.0
        _record_turn_time(game, mover_id=1)  # white moved at t=45 (+15s)
        assert game["white_seconds"] == 25
        assert game["black_seconds"] == 20


class TestTimeSummaryBlock:
    """_time_summary_block renders per-player totals for the game-over embed."""

    def test_empty_when_no_time_recorded(self):
        from src.games.chess import _time_summary_block
        game = {"white_seconds": 0, "black_seconds": 0}
        assert _time_summary_block(game) == ""

    def test_renders_both_totals(self):
        from src.games.chess import _time_summary_block
        game = {"white_seconds": 273, "black_seconds": 45}
        block = _time_summary_block(game)
        assert "White 4:33" in block
        assert "Black 45s" in block
        assert block.startswith("\n")

    def test_renders_when_only_one_side_has_time(self):
        from src.games.chess import _time_summary_block
        game = {"white_seconds": 60, "black_seconds": 0}
        block = _time_summary_block(game)
        assert "White 1:00" in block
        assert "Black 0s" in block


class TestCapturesBlock:
    """_captures_block builds the two-line embed snippet."""

    def test_empty_at_start_of_game(self):
        from src.games.chess import _captures_block
        game = {"fen": chess.STARTING_FEN}
        assert _captures_block(game) == ""

    def test_shows_both_sides_when_anyone_has_captures(self):
        from src.games.chess import _captures_block
        # Position where white captured a black pawn but black has nothing.
        b = chess.Board()
        b.remove_piece_at(chess.E7)
        game = {"fen": b.fen()}
        block = _captures_block(game)
        assert "White captured: ♟" in block
        assert "Black captured: —" in block
        assert block.startswith("\n")  # caller appends directly

    def test_handles_unparseable_fen(self):
        from src.games.chess import _captures_block
        game = {"fen": "not-a-real-fen"}
        assert _captures_block(game) == ""

    def test_preserves_result_token(self):
        from src.games.chess import _movetext_only
        pgn = (
            '[Event "x"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n'
            '1. e4 e5 1-0\n'
        )
        out = _movetext_only(pgn)
        assert out.endswith("1-0")

    def test_garbage_input_does_not_crash(self):
        """python-chess parses unrecognized text as an empty game (returns '*').
        The helper must not raise on weird inputs."""
        from src.games.chess import _movetext_only
        out = _movetext_only("not a pgn at all")
        # Garbage parses to an empty game terminator; just confirm no crash + a string out.
        assert isinstance(out, str)
        assert len(out) <= 50  # no header leakage


# ─────────────────────────────────────────────────────────────────────────────
# ChessCog: !move flow + game-over -> chess_reports
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_chess_state():
    yield
    _state.active_chess_games.clear()


@pytest.fixture(autouse=True)
def _stub_chess_edit_board(monkeypatch):
    """chess.py uses _bump_board (delete-and-resend) and ai_cog still uses
    _edit_board for ttt/c4/hangman + _bump_chess_board for chess forfeit.
    Stub all three so cog calls don't try real channel I/O."""
    import src.games.chess as _chess_mod
    bump_calls = []
    async def _stub_bump(channel, game, embed, *, file=None, silent=True):
        # Mimic the real _bump_board side-effect: assign a fresh message id.
        game["board_msg_id"] = (game.get("board_msg_id") or 0) + 1
        bump_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_chess_mod, "_bump_board", _stub_bump)

    edit_calls = []
    async def _stub_edit(channel, game, embed, *, file=None):
        edit_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_ai_cog, "_edit_board", _stub_edit)
    monkeypatch.setattr(_ai_cog, "_bump_chess_board", _stub_bump)
    return bump_calls


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
    ctx.channel.guild = g  # _finalize_game reads channel.guild for per-guild lookups
    ctx.message = FakeMessage(author=member)
    return ctx


def _seed_chess_game(channel_id: int, white_id: int, black_id: int, amount: int = 0,
                     *, fen: str | None = None, current_id: int | None = None):
    starting_fen = fen if fen is not None else chess_engine.STARTING_FEN
    _state.active_chess_games[channel_id] = {
        "fen": starting_fen,
        "pgn": _initial_pgn("White", "Black", None, starting_fen=starting_fen),
        "white_id": white_id,
        "black_id": black_id,
        "current_id": current_id if current_id is not None else white_id,
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
async def test_capture_move_annotates_last_move_with_captured_piece(
    db, _stub_chess_edit_board,
):
    """When a move captures, the rendered "Last move" line should name the
    captured piece (e.g. "captured Black's pawn")."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1108, display_name="White")
    black = FakeMember(uid=1109, display_name="Black")
    # Position after 1.e4 d5: white to move, exd5 captures a black pawn.
    fen_after_e4_d5 = "rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2"
    _seed_chess_game(750, white.id, black.id, fen=fen_after_e4_d5)
    ctx = _ctx_for(white, channel_id=750)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "exd5")

    g = _state.active_chess_games[750]
    assert g["last_move"] == "White played exd5 — captured Black's pawn"


@_aio
async def test_non_capture_move_leaves_last_move_unannotated(
    db, _stub_chess_edit_board,
):
    """Non-capturing moves render without a trailing capture phrase."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1110, display_name="White")
    black = FakeMember(uid=1111, display_name="Black")
    _seed_chess_game(751, white.id, black.id)
    ctx = _ctx_for(white, channel_id=751)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")

    g = _state.active_chess_games[751]
    assert g["last_move"] == "White played e4"


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
async def test_checkmate_headline_uses_pgn_name_when_guild_cache_misses(
    db, _stub_chess_edit_board,
):
    """Regression: when a human wins a PvP game, the game-over headline must
    show the player's display name, not the raw 18-digit Discord user id.

    Previously, the headline did `guild.get_member(winner_id).display_name`
    which silently returned None for members missing from the cache (common
    without the privileged members intent), falling through to str(uid).
    Fix: prefer the [White]/[Black] PGN-header names captured at game start.
    """
    cog = ChessCog(bot=None)
    white = FakeMember(uid=999_888_001, display_name="WhitePlayer")
    black = FakeMember(uid=999_888_002, display_name="BlackPlayer")
    _seed_chess_game(704, white.id, black.id)
    ctx_w = _ctx_for(white, channel_id=704)
    ctx_b = _ctx_for(black, channel_id=704, guild=ctx_w.guild)
    # Deliberately don't add `black` to ctx_w.guild.members so guild.get_member(black.id)
    # returns None — simulating the privileged-members-intent cache miss.

    # Fool's mate: black wins.
    await cog.cmd_move_chess.callback(cog, ctx_w, "f3")
    await cog.cmd_move_chess.callback(cog, ctx_b, "e5")
    await cog.cmd_move_chess.callback(cog, ctx_w, "g4")
    await cog.cmd_move_chess.callback(cog, ctx_b, "Qh4#")

    game_over_embeds = [
        embed for _ch, _g, embed, _f in _stub_chess_edit_board
        if embed.title and "Game Over" in embed.title
    ]
    assert game_over_embeds, "expected a game-over embed"
    desc = game_over_embeds[-1].description or ""
    # Raw uid must NOT appear in the headline.
    assert str(black.id) not in desc, f"raw uid leaked: {desc!r}"
    # Display name (from PGN header) DOES appear.
    assert "BlackPlayer" in desc


def test_names_from_pgn_extracts_white_and_black():
    """Unit test of the PGN-name extractor used by the winner-name resolver."""
    from src.games.chess import _names_from_pgn
    pgn = (
        '[Event "Discord chess"]\n'
        '[Site "Discord"]\n'
        '[Date "2026.05.18"]\n'
        '[Round "?"]\n'
        '[White "Alice Display"]\n'
        '[Black "Bob Display"]\n'
        '[Result "*"]\n\n'
        '1. e4 *\n'
    )
    white, black = _names_from_pgn(pgn)
    assert white == "Alice Display"
    assert black == "Bob Display"


def test_names_from_pgn_handles_missing_headers():
    """Returns (None, None) when headers are absent — caller falls back."""
    from src.games.chess import _names_from_pgn
    assert _names_from_pgn("") == (None, None)
    assert _names_from_pgn("not a pgn") == (None, None)


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
    assert "White" in embed.description and "wins" in embed.description


@_aio
async def test_chess_view_message_is_ephemeral(db, monkeypatch):
    """!chess view sends through send_ephemeral so the message auto-expires
    after EPHEMERAL_DELETE_AFTER (60s). Spy on send_ephemeral to confirm
    the chess cog routes view replies through it, not raw ctx.send."""
    from src.persistence import save_chess_report
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=10, result="1-0",
        pgn='[Event "x"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n1. e4 1-0\n',
        final_fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    )

    import src.games.chess as _chess_mod
    calls = []
    async def _spy_send_ephemeral(ctx, *args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return await ctx.send(*args, **kwargs)
    monkeypatch.setattr(_chess_mod, "send_ephemeral", _spy_send_ephemeral)

    cog = ChessCog(bot=None)
    user = FakeMember(uid=1207)
    ctx = _ctx_for(user, channel_id=807)

    await cog._cmd_view(ctx, (str(rid),))

    assert len(calls) == 1, f"expected one send_ephemeral call, got {len(calls)}"
    # Also confirm the existing error branches go through send_ephemeral.
    await cog._cmd_view(ctx, ())
    await cog._cmd_view(ctx, ("not-a-number",))
    await cog._cmd_view(ctx, ("9999999",))
    assert len(calls) == 4


@_aio
async def test_chess_view_black_winner_branch(db):
    """Black-winner reports describe the outcome as 'Black wins' (different
    branch from white-winner test)."""
    from src.persistence import save_chess_report
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=20, result="0-1",
        pgn='[Event "x"]\n[White "A"]\n[Black "B"]\n[Result "0-1"]\n\n1. f3 e5 2. g4 Qh4# 0-1\n',
        final_fen="rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3",
    )

    cog = ChessCog(bot=None)
    user = FakeMember(uid=1203)
    ctx = _ctx_for(user, channel_id=803)

    await cog._cmd_view(ctx, (str(rid),))

    assert len(ctx.sent_embeds) == 1
    embed = ctx.sent_embeds[0]
    assert "Black" in embed.description and "wins" in embed.description
    assert "0-1" in embed.description


@_aio
async def test_chess_view_draw_branch(db):
    """Draw reports (winner_id NULL) describe the outcome as a draw."""
    from src.persistence import save_chess_report
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=None, result="1/2-1/2",
        pgn='[Event "x"]\n[White "A"]\n[Black "B"]\n[Result "1/2-1/2"]\n\n1. e4 e5 1/2-1/2\n',
        final_fen="rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
    )

    cog = ChessCog(bot=None)
    user = FakeMember(uid=1204)
    ctx = _ctx_for(user, channel_id=804)

    await cog._cmd_view(ctx, (str(rid),))

    assert len(ctx.sent_embeds) == 1
    embed = ctx.sent_embeds[0]
    assert "Draw" in embed.description
    assert "1/2-1/2" in embed.description


@_aio
async def test_chess_view_truncates_huge_movetext(db, monkeypatch):
    """Movetext longer than the embed description budget gets truncated and
    marked. The truncation runs on the OUTPUT of _movetext_only, so we patch
    that helper to return a known-huge string rather than constructing a
    legitimately-long valid game."""
    from src.persistence import save_chess_report
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=10, result="1-0",
        pgn='[Event "x"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n1. e4 e5 1-0\n',
        final_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    )

    import src.games.chess as _chess_mod
    huge = " ".join(f"{i}. Nf3 Nf6" for i in range(1, 600))  # ~6000 chars
    monkeypatch.setattr(_chess_mod, "_movetext_only", lambda _pgn: huge)

    cog = ChessCog(bot=None)
    user = FakeMember(uid=1205)
    ctx = _ctx_for(user, channel_id=805)

    await cog._cmd_view(ctx, (str(rid),))

    assert len(ctx.sent_embeds) == 1
    embed = ctx.sent_embeds[0]
    # Description must fit Discord's 4096-char limit and signal truncation.
    assert len(embed.description) <= 4096
    assert "truncated" in embed.description.lower()


@_aio
async def test_chess_view_strips_pgn_headers_in_embed(db):
    """The default !chess view shows movetext only, not the [Event]/[Site]/etc.
    header block (that's what !chess pgn is for)."""
    from src.persistence import save_chess_report
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=10, result="1-0",
        pgn=(
            '[Event "Test Event Headerline"]\n'
            '[Site "TestSite"]\n'
            '[Date "2026.05.17"]\n'
            '[Round "?"]\n'
            '[White "Alice"]\n'
            '[Black "Bob"]\n'
            '[Result "1-0"]\n\n'
            '1. e4 e5 2. Nf3 Nc6 3. Bb5 1-0\n'
        ),
        final_fen="r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    )

    cog = ChessCog(bot=None)
    user = FakeMember(uid=1206)
    ctx = _ctx_for(user, channel_id=806)

    await cog._cmd_view(ctx, (str(rid),))

    desc = ctx.sent_embeds[0].description
    # Movetext present.
    assert "1. e4 e5 2. Nf3 Nc6 3. Bb5 1-0" in desc
    # Headers absent.
    assert "[Event" not in desc
    assert "Test Event Headerline" not in desc
    assert "[Site" not in desc
    # PGN hint points at the full-PGN subcommand.
    assert f"!chess pgn {rid}" in desc


@_aio
async def test_chess_pgn_returns_full_headered_pgn(db):
    """!chess pgn <id> shows the entire PGN with headers (for lichess import)."""
    from src.persistence import save_chess_report
    full_pgn = (
        '[Event "Discord chess"]\n'
        '[Site "Discord"]\n'
        '[Date "2026.05.17"]\n'
        '[Round "?"]\n'
        '[White "Alice"]\n'
        '[Black "Bob"]\n'
        '[Result "1-0"]\n\n'
        '1. e4 e5 2. Nf3 Nc6 3. Bb5 1-0\n'
    )
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=10, result="1-0", pgn=full_pgn,
        final_fen="r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    )

    cog = ChessCog(bot=None)
    user = FakeMember(uid=1207)
    ctx = _ctx_for(user, channel_id=807)

    await cog._cmd_pgn(ctx, (str(rid),))

    assert len(ctx.sent_embeds) == 1
    desc = ctx.sent_embeds[0].description
    # Headers present (this is the point of the pgn subcommand).
    assert "[Event" in desc
    assert '[White "Alice"]' in desc
    # Movetext also there.
    assert "1. e4 e5" in desc


@_aio
async def test_chess_pgn_missing_id_sends_usage(db):
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1208)
    ctx = _ctx_for(user, channel_id=808)

    await cog._cmd_pgn(ctx, ())

    assert len(ctx.sent_embeds) == 1
    assert "Usage" in ctx.sent_embeds[0].title


@_aio
async def test_chess_pgn_nonexistent_report(db):
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1209)
    ctx = _ctx_for(user, channel_id=809)

    await cog._cmd_pgn(ctx, ("999999",))

    assert len(ctx.sent_embeds) == 1
    assert "Not Found" in ctx.sent_embeds[0].title


@_aio
async def test_cmd_chess_dispatches_pgn_subcommand(db):
    """`!chess pgn <id>` routes through cmd_chess to _cmd_pgn (not the
    new-game flow)."""
    from src.persistence import save_chess_report
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=10, result="1-0",
        pgn='[Event "x"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n1. e4 1-0\n',
        final_fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    )

    cog = ChessCog(bot=None)
    user = FakeMember(uid=1210)
    ctx = _ctx_for(user, channel_id=810)

    await cog.cmd_chess.callback(cog, ctx, "pgn", str(rid))

    # Got the pgn embed (with headers), not a new game state.
    assert 810 not in _state.active_chess_games
    assert len(ctx.sent_embeds) == 1
    assert "PGN" in ctx.sent_embeds[0].title
    assert "[Event" in ctx.sent_embeds[0].description


@_aio
async def test_chess_bare_shows_help_menu(db):
    """`!chess` with no args and no mention shows the help menu instead of
    trying to start a malformed game."""
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1300)
    ctx = _ctx_for(user, channel_id=900)

    await cog.cmd_chess.callback(cog, ctx)

    # No game created — went to help.
    assert 900 not in _state.active_chess_games
    assert len(ctx.sent_embeds) == 1
    e = ctx.sent_embeds[0]
    assert "Chess" in e.title and "Commands" in e.title
    desc = e.description or ""
    # All command groups present.
    assert "!chess @user" in desc
    assert "!chessbot" in desc
    assert "!move" in desc
    assert "!stop" in desc
    assert "!chess view" in desc
    assert "!chess pgn" in desc


@_aio
async def test_chess_help_subcommand_shows_help_menu(db):
    """`!chess help` and `!chess ?` both route to the help menu."""
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1301)
    ctx = _ctx_for(user, channel_id=901)

    await cog.cmd_chess.callback(cog, ctx, "help")
    assert "Commands" in ctx.sent_embeds[-1].title

    ctx2 = _ctx_for(user, channel_id=902)
    await cog.cmd_chess.callback(cog, ctx2, "?")
    assert "Commands" in ctx2.sent_embeds[-1].title


@_aio
async def test_chess_help_does_not_start_game(db):
    """The help menu must never create active game state."""
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1302)
    ctx = _ctx_for(user, channel_id=903)

    await cog.cmd_chess.callback(cog, ctx)

    assert 903 not in _state.active_chess_games


@_aio
async def test_chess_view_invalid_number(db):
    """!chess view <non-int> sends an error embed."""
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1206)
    ctx = _ctx_for(user, channel_id=806)

    await cog._cmd_view(ctx, ("abc",))

    assert len(ctx.sent_embeds) == 1
    assert "Invalid" in ctx.sent_embeds[0].title


# ─────────────────────────────────────────────────────────────────────────────
# !chessthreats — admin debug view / artifact unlock
# (access requires bot_admin or the chessthreats artifact; these tests use
# bot_admins — the artifact path is covered in tests/test_artifacts.py)
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_chessthreats_no_active_game(db, _stub_chess_edit_board):
    """!chessthreats sends an error embed when there's no game in the channel."""
    cog = ChessCog(bot=None)
    admin = FakeMember(uid=1500)
    _state.bot_admins.add(admin.id)
    ctx = _ctx_for(admin, channel_id=950)

    await cog.cmd_chessthreats.callback(cog, ctx)

    assert len(ctx.sent_embeds) == 1
    assert "No Game" in ctx.sent_embeds[0].title


@_aio
async def test_chessthreats_with_hanging_pieces_sends_image(db, _stub_chess_edit_board, monkeypatch):
    """!chessthreats on a game with hanging pieces sends an embed + file
    attachment. The description should mention the count of hanging pieces."""
    # Stub the renderer so the test doesn't require cairosvg locally.
    import src.games.chess as _chess_mod
    monkeypatch.setattr(
        _chess_mod.chess_render, "render_board_png",
        lambda board, **kwargs: b"FAKE_PNG_BYTES",
    )
    cog = ChessCog(bot=None)
    admin = FakeMember(uid=1501)
    _state.bot_admins.add(admin.id)
    other = FakeMember(uid=1502)
    # White queen on d1 hanging to black rook on d8 (no white defenders).
    _seed_chess_game(951, admin.id, other.id, fen="3rk3/8/8/8/8/8/8/3QK3 w - - 0 1")
    ctx = _ctx_for(admin, channel_id=951)

    await cog.cmd_chessthreats.callback(cog, ctx)

    # Embed sent + file attached.
    assert len(ctx.sent_embeds) == 1
    embed = ctx.sent_embeds[0]
    assert "Chess Threats" in embed.title
    # Both queen (d1, hanging) and rook (d8, attacked by queen) hang in this
    # position. The description mentions a count.
    assert "hanging piece" in embed.description


@_aio
async def test_chessthreats_no_hanging_pieces_shows_zero(db, _stub_chess_edit_board, monkeypatch):
    """!chessthreats on a quiet starting-position game shows '0 hanging pieces'."""
    import src.games.chess as _chess_mod
    monkeypatch.setattr(
        _chess_mod.chess_render, "render_board_png",
        lambda board, **kwargs: b"FAKE_PNG_BYTES",
    )
    cog = ChessCog(bot=None)
    admin = FakeMember(uid=1503)
    _state.bot_admins.add(admin.id)
    other = FakeMember(uid=1504)
    _seed_chess_game(952, admin.id, other.id)  # default starting position
    ctx = _ctx_for(admin, channel_id=952)

    await cog.cmd_chessthreats.callback(cog, ctx)

    assert len(ctx.sent_embeds) == 1
    # Empty starting position has no hanging pieces.
    assert "**0** hanging" in ctx.sent_embeds[0].description


# ─────────────────────────────────────────────────────────────────────────────
# Special-move flows through cmd_move_chess (castling, en passant, promotion)
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_castling_via_cmd_move(db, _stub_chess_edit_board):
    """O-O survives the full pipeline (try_move → push_with_san → PGN append →
    save → reload). Seeds a king-side-clear position and castles in one move."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1400, display_name="W")
    black = FakeMember(uid=1401, display_name="B")
    _seed_chess_game(
        910, white.id, black.id,
        fen="r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1",
    )
    ctx = _ctx_for(white, channel_id=910)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "O-O")

    g = _state.active_chess_games[910]
    # Post-castle: white king on g1, rook on f1. Only black retains castling rights.
    assert g["fen"].startswith("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R4RK1")
    assert " kq " in g["fen"]
    assert "O-O" in g["pgn"]
    assert g["current_id"] == black.id

    # Round-trip via init reload preserves castling-applied state.
    _state.active_chess_games.clear()
    import src.persistence as _persistence
    _persistence._init_db_state_done = False
    await _persistence.init_db_state()
    g2 = _state.active_chess_games[910]
    assert g2["fen"] == g["fen"]
    assert "O-O" in g2["pgn"]


@_aio
async def test_en_passant_via_cmd_move(db, _stub_chess_edit_board):
    """Black plays d7-d5 (sets ep target d6), white captures exd6 e.p. — the
    pawn lands on d6 and the captured d5 pawn is gone."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1402, display_name="W")
    black = FakeMember(uid=1403, display_name="B")
    _seed_chess_game(
        911, white.id, black.id,
        fen="4k3/3p4/8/4P3/8/8/8/4K3 b - - 0 1",
        current_id=1403,  # black to move
    )
    ctx_b = _ctx_for(black, channel_id=911)
    ctx_b.guild.members.append(white)
    ctx_w = _ctx_for(white, channel_id=911, guild=ctx_b.guild)

    await cog.cmd_move_chess.callback(cog, ctx_b, "d5")
    # After black's d5, the FEN must show en-passant target d6.
    assert " d6 " in _state.active_chess_games[911]["fen"]

    await cog.cmd_move_chess.callback(cog, ctx_w, "exd6")

    g = _state.active_chess_games[911]
    # White's pawn now on d6; black's d5 pawn captured.
    assert g["fen"].startswith("4k3/8/3P4/8/8/8/8/4K3")
    assert "exd6" in g["pgn"]


@_aio
async def test_promotion_via_cmd_move_uci(db, _stub_chess_edit_board):
    """UCI promotion suffix (a7a8q) flows through and renders as a8=Q in PGN."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1404, display_name="W")
    black = FakeMember(uid=1405, display_name="B")
    _seed_chess_game(
        912, white.id, black.id,
        fen="4k3/P7/8/8/8/8/8/4K3 w - - 0 1",
    )
    ctx = _ctx_for(white, channel_id=912)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "a7a8q")

    g = _state.active_chess_games[912]
    # Post-promotion: white queen on a8 (gives check).
    assert g["fen"].startswith("Q3k3/8/8/8/8/8/8/4K3")
    assert "a8=Q" in g["pgn"]
    assert g["current_id"] == black.id


# ─────────────────────────────────────────────────────────────────────────────
# cmd_move_chess edge cases
# ─────────────────────────────────────────────────────────────────────────────


@_aio
async def test_move_no_active_game(db, _stub_chess_edit_board):
    """!move in a channel with no game sends a help message and bails."""
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1500)
    ctx = _ctx_for(user, channel_id=920)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")

    # An error/help message was sent; no game created.
    assert 920 not in _state.active_chess_games
    assert len(ctx.sent_messages) + len(ctx.sent_embeds) >= 1


@_aio
async def test_move_with_no_args(db, _stub_chess_edit_board):
    """!move with no args sends usage and leaves state untouched."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1502, display_name="W")
    black = FakeMember(uid=1503, display_name="B")
    _seed_chess_game(921, white.id, black.id)
    ctx = _ctx_for(white, channel_id=921)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx)

    g = _state.active_chess_games[921]
    assert g["current_id"] == white.id  # still white's turn
    assert g["fen"] == chess_engine.STARTING_FEN
    assert len(ctx.sent_messages) + len(ctx.sent_embeds) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# cmd_chess game-start flow
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _stub_setup_pvp_game(monkeypatch):
    """Bypass _setup_pvp_game's confirmation/wager flow for game-start tests.
    Records calls so tests can assert it was invoked (or not)."""
    import src.games.chess as _chess_mod
    calls = []
    async def _stub(ctx, opponent, amount, invite_title):
        calls.append({"opponent": opponent, "amount": amount, "title": invite_title})
        return True
    monkeypatch.setattr(_chess_mod, "_setup_pvp_game", _stub)
    return calls


@_aio
async def test_chess_start_creates_active_game(db, _stub_chess_edit_board, _stub_setup_pvp_game):
    """!chess @opponent creates an active_chess_games entry with the right
    state shape, runs _setup_pvp_game, and saves the row."""
    cog = ChessCog(bot=None)
    challenger = FakeMember(uid=1600, display_name="Challenger")
    opponent = FakeMember(uid=1601, display_name="Opponent")
    ctx = _ctx_for(challenger, channel_id=940)
    ctx.guild.members.append(opponent)
    ctx.message.mentions = [opponent]

    await cog.cmd_chess.callback(cog, ctx)

    assert 940 in _state.active_chess_games
    g = _state.active_chess_games[940]
    assert g["white_id"] == challenger.id
    assert g["black_id"] == opponent.id
    assert g["current_id"] == challenger.id  # white moves first
    assert g["fen"] == chess_engine.STARTING_FEN
    assert g["amount"] == 0
    # _setup_pvp_game was called.
    assert len(_stub_setup_pvp_game) == 1
    assert _stub_setup_pvp_game[0]["opponent"] is opponent


@_aio
async def test_chess_start_with_wager_amount(db, _stub_chess_edit_board, _stub_setup_pvp_game):
    """Trailing int is parsed as the wager and passed to _setup_pvp_game."""
    cog = ChessCog(bot=None)
    challenger = FakeMember(uid=1602)
    opponent = FakeMember(uid=1603)
    ctx = _ctx_for(challenger, channel_id=941)
    ctx.guild.members.append(opponent)
    ctx.message.mentions = [opponent]

    await cog.cmd_chess.callback(cog, ctx, "500")

    assert _state.active_chess_games[941]["amount"] == 500
    assert _stub_setup_pvp_game[0]["amount"] == 500


@_aio
async def test_chess_start_channel_already_busy(db, _stub_chess_edit_board, _stub_setup_pvp_game):
    """If a TTT or chess game is already active in the channel, !chess refuses."""
    cog = ChessCog(bot=None)
    challenger = FakeMember(uid=1604)
    opponent = FakeMember(uid=1605)
    ctx = _ctx_for(challenger, channel_id=942)
    ctx.guild.members.append(opponent)
    ctx.message.mentions = [opponent]
    _state.active_ttt_games[942] = {"players": [challenger.id, opponent.id]}

    await cog.cmd_chess.callback(cog, ctx)

    # No chess game created, _setup_pvp_game never reached.
    assert 942 not in _state.active_chess_games
    assert len(_stub_setup_pvp_game) == 0
    # Cleanup
    _state.active_ttt_games.pop(942, None)


@_aio
async def test_chess_start_setup_rejection_no_game_created(db, _stub_chess_edit_board, monkeypatch):
    """If _setup_pvp_game returns False (no opponent, declined, etc.), the
    cog must not create the active game state."""
    import src.games.chess as _chess_mod
    async def _stub_rejected(ctx, opponent, amount, invite_title):
        return False
    monkeypatch.setattr(_chess_mod, "_setup_pvp_game", _stub_rejected)

    cog = ChessCog(bot=None)
    challenger = FakeMember(uid=1606)
    opponent = FakeMember(uid=1607)
    ctx = _ctx_for(challenger, channel_id=943)
    ctx.guild.members.append(opponent)
    ctx.message.mentions = [opponent]

    await cog.cmd_chess.callback(cog, ctx)

    assert 943 not in _state.active_chess_games


@_aio
async def test_chess_start_persists_via_init_reload(db, _stub_chess_edit_board, _stub_setup_pvp_game):
    """The game saved by cmd_chess survives a fresh init_db_state hydration."""
    cog = ChessCog(bot=None)
    challenger = FakeMember(uid=1608)
    opponent = FakeMember(uid=1609)
    ctx = _ctx_for(challenger, channel_id=944)
    ctx.guild.members.append(opponent)
    ctx.message.mentions = [opponent]

    await cog.cmd_chess.callback(cog, ctx)

    # Clear memory; reload from DB.
    _state.active_chess_games.clear()
    import src.persistence as _persistence
    _persistence._init_db_state_done = False
    await _persistence.init_db_state()

    assert 944 in _state.active_chess_games
    g = _state.active_chess_games[944]
    assert g["white_id"] == challenger.id
    assert g["black_id"] == opponent.id
    assert g["current_id"] == challenger.id


@_aio
async def test_same_user_two_moves_in_a_row_second_is_rejected(db, _stub_chess_edit_board):
    """White plays, then immediately tries to play again before black has
    moved. The second invocation must be rejected by the not-your-turn gate.

    Note: cmd_move_chess is structurally race-free — the gate check, move
    parsing, and current_id flip are all sync, with no await between them.
    This is a regression test against accidentally introducing an await in
    that critical section; it pins the user-visible behavior, not the
    internal ordering.
    """
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1300, display_name="W")
    black = FakeMember(uid=1301, display_name="B")
    _seed_chess_game(922, white.id, black.id)
    ctx_w = _ctx_for(white, channel_id=922)
    ctx_w.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx_w, "e4")
    await cog.cmd_move_chess.callback(cog, ctx_w, "Nf3")  # white's "second" move — should be rejected

    g = _state.active_chess_games[922]
    # Only e4 applied; the second white move bailed at the gate.
    assert g["fen"].startswith("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b")
    assert " Nf3" not in g["pgn"]
    assert g["current_id"] == black.id


@_aio
async def test_move_updates_board_msg_id_after_bump(db, _stub_chess_edit_board):
    """End-to-end: cmd_move_chess flips current_id and updates board_msg_id
    (the bump stub mimics _bump_board's id-reassignment side-effect)."""
    cog = ChessCog(bot=None)
    white = FakeMember(uid=1700)
    black = FakeMember(uid=1701)
    _seed_chess_game(950, white.id, black.id)
    initial_id = _state.active_chess_games[950]["board_msg_id"]
    ctx = _ctx_for(white, channel_id=950)
    ctx.guild.members.append(black)

    await cog.cmd_move_chess.callback(cog, ctx, "e4")

    g = _state.active_chess_games[950]
    assert g["board_msg_id"] != initial_id  # bump assigned a new id
