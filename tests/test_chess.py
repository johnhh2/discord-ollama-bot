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
    assert "White" in embed.description and "wins" in embed.description


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
async def test_chess_view_truncates_huge_pgn(db):
    """PGNs longer than the embed description budget get truncated and marked."""
    from src.persistence import save_chess_report
    huge_pgn_body = " ".join([f"{i}. e4 e5" for i in range(1, 600)])  # ~6000 chars
    pgn = (
        '[Event "x"]\n[White "A"]\n[Black "B"]\n[Result "1-0"]\n\n'
        + huge_pgn_body + " 1-0\n"
    )
    rid = await save_chess_report(
        guild_id=42, channel_id=999, white_id=10, black_id=20,
        winner_id=10, result="1-0", pgn=pgn,
        final_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    )

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
async def test_chess_view_invalid_number(db):
    """!chess view <non-int> sends an error embed."""
    cog = ChessCog(bot=None)
    user = FakeMember(uid=1206)
    ctx = _ctx_for(user, channel_id=806)

    await cog._cmd_view(ctx, ("abc",))

    assert len(ctx.sent_embeds) == 1
    assert "Invalid" in ctx.sent_embeds[0].title


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
