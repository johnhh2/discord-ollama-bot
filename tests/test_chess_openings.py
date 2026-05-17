"""Opening book tests: probability curve, opening selection by Elo, and the
book-move lookup that the cog uses to feed pick_move."""
import random

import chess
import pytest

from src.games import chess_openings


# ─────────────────────────────────────────────────────────────────────────────
# Probability curve
# ─────────────────────────────────────────────────────────────────────────────


class TestOpeningProbabilityForElo:
    def test_anchor_points(self):
        anchors = [(100, 0.20), (400, 0.60), (800, 0.95),
                   (1100, 0.80), (1319, 0.50)]
        for elo, expected in anchors:
            actual = chess_openings.opening_probability_for_elo(elo)
            assert abs(actual - expected) < 0.001, (
                f"P(opening) at Elo {elo}: expected ~{expected}, got {actual}"
            )

    def test_peaks_at_800(self):
        # 800 is the explicit peak — neighbours should be lower or equal.
        p_peak = chess_openings.opening_probability_for_elo(800)
        for elo in (700, 900, 1000, 1200):
            assert chess_openings.opening_probability_for_elo(elo) <= p_peak

    def test_below_floor_clamps(self):
        assert chess_openings.opening_probability_for_elo(50) == 0.20
        assert chess_openings.opening_probability_for_elo(0) == 0.20

    def test_above_top_clamps(self):
        assert chess_openings.opening_probability_for_elo(2000) == 0.50
        assert chess_openings.opening_probability_for_elo(9999) == 0.50


# ─────────────────────────────────────────────────────────────────────────────
# Opening definitions — sanity-check the OPENINGS table
# ─────────────────────────────────────────────────────────────────────────────


class TestOpeningTable:
    def test_every_opening_has_even_move_count(self):
        # The dataclass validator catches this at construction, but
        # double-check defensively.
        for op in chess_openings.OPENINGS:
            assert len(op.moves) % 2 == 0, (
                f"{op.name} has {len(op.moves)} moves; must be even (paired w/b)"
            )

    def test_every_opening_is_legal_from_starting_position(self):
        """Replay each opening's moves on a fresh board. If any move is
        illegal in the position it claims to apply to, the opening is
        broken."""
        for op in chess_openings.OPENINGS:
            board = chess.Board()
            for ply, uci in enumerate(op.moves):
                move = chess.Move.from_uci(uci)
                assert move in board.legal_moves, (
                    f"{op.name} move {ply + 1} ({uci}) is illegal in "
                    f"position {board.fen()}"
                )
                board.push(move)

    def test_elo_ranges_are_sensible(self):
        for op in chess_openings.OPENINGS:
            assert op.elo_min <= op.elo_max
            assert op.elo_min >= 100
            assert op.elo_max <= 1319


# ─────────────────────────────────────────────────────────────────────────────
# openings_for_elo / pick_opening_for_elo
# ─────────────────────────────────────────────────────────────────────────────


class TestOpeningsForElo:
    def test_elo_in_range_appears(self):
        # Italian Game has elo_min=400, elo_max=1319.
        result = chess_openings.openings_for_elo(500)
        names = {op.name for op in result}
        assert "Italian Game (Black)" in names

    def test_elo_out_of_range_filtered(self):
        # Italian Game's elo_min is 400; at Elo 100 it shouldn't appear.
        result = chess_openings.openings_for_elo(100)
        names = {op.name for op in result}
        assert "Italian Game (Black)" not in names

    def test_endpoints_inclusive(self):
        # An opening with elo_min=X should appear exactly at Elo X.
        for op in chess_openings.OPENINGS:
            assert op in chess_openings.openings_for_elo(op.elo_min)
            assert op in chess_openings.openings_for_elo(op.elo_max)


class TestPickOpeningForElo:
    def test_returns_an_opening_when_some_apply(self):
        # At Elo 700 multiple openings apply; we should get one.
        rng = random.Random(42)
        op = chess_openings.pick_opening_for_elo(700, rng=rng)
        assert op is not None
        assert op.elo_min <= 700 <= op.elo_max

    def test_returns_none_when_no_openings_apply(self):
        # Construct a synthetic "everyone out of range" case by clearing
        # OPENINGS via monkey-equivalent in-process state.
        # Easier: pick an Elo wildly out of every opening's range. With our
        # current curated set there's no such Elo in [100, 1319]; verify the
        # function's None branch by emptying the candidates manually.
        empty = []
        # openings_for_elo wraps a list comp on OPENINGS, so we test the
        # underlying logic by calling pick_opening_for_elo with a patched
        # OPENINGS — fall back to mocking.
        original = chess_openings.OPENINGS
        try:
            chess_openings.OPENINGS = tuple(empty)
            assert chess_openings.pick_opening_for_elo(500) is None
        finally:
            chess_openings.OPENINGS = original


# ─────────────────────────────────────────────────────────────────────────────
# book_move_for_position — the actual cog handoff logic
# ─────────────────────────────────────────────────────────────────────────────


class TestBookMoveForPosition:
    def test_first_book_move_after_white_plays_expected(self):
        # Italian Game: e2e4, e7e5, ...
        op = next(o for o in chess_openings.OPENINGS if o.name == "Italian Game (Black)")
        # After white plays e2e4, bot's reply (history length 1) is e7e5.
        move = chess_openings.book_move_for_position(op, ["e2e4"])
        assert move == chess.Move.from_uci("e7e5")

    def test_returns_none_when_white_deviates(self):
        op = next(o for o in chess_openings.OPENINGS if o.name == "Italian Game (Black)")
        # White plays d4 instead of e4 — not in book.
        move = chess_openings.book_move_for_position(op, ["d2d4"])
        assert move is None

    def test_returns_none_when_human_deviates_mid_line(self):
        # Italian Game: e2e4, e7e5, g1f3, b8c6, f1c4, f8c5
        op = next(o for o in chess_openings.OPENINGS if o.name == "Italian Game (Black)")
        # First two book plies match; third (white) is wrong (b1c3 not g1f3).
        history = ["e2e4", "e7e5", "b1c3"]
        move = chess_openings.book_move_for_position(op, history)
        assert move is None

    def test_continues_mid_line_when_history_matches(self):
        op = next(o for o in chess_openings.OPENINGS if o.name == "Italian Game (Black)")
        # After e2e4, e7e5, g1f3 (history length 3, white's turn done),
        # bot's next move is b8c6.
        history = ["e2e4", "e7e5", "g1f3"]
        move = chess_openings.book_move_for_position(op, history)
        assert move == chess.Move.from_uci("b8c6")

    def test_returns_none_when_past_end_of_book(self):
        op = next(o for o in chess_openings.OPENINGS if o.name == "Italian Game (Black)")
        # Replay the whole opening and ask for the move after — book is done.
        history = list(op.moves)
        history.append("d2d3")  # white plays something past book
        move = chess_openings.book_move_for_position(op, history)
        assert move is None

    def test_returns_none_on_wrong_turn(self):
        # If called with an even-length history, it's white's turn — the bot
        # (black) has no move to play. Defensive guard.
        op = next(o for o in chess_openings.OPENINGS if o.name == "Italian Game (Black)")
        move = chess_openings.book_move_for_position(op, [])  # empty = white to move
        assert move is None
        move2 = chess_openings.book_move_for_position(op, ["e2e4", "e7e5"])
        assert move2 is None


# ─────────────────────────────────────────────────────────────────────────────
# Dataclass validation
# ─────────────────────────────────────────────────────────────────────────────


class TestOpeningValidation:
    def test_odd_move_count_raises(self):
        with pytest.raises(ValueError, match="even length"):
            chess_openings.Opening(
                name="bad", moves=("e2e4", "e7e5", "g1f3"),
                elo_min=100, elo_max=1000,
            )

    def test_inverted_elo_range_raises(self):
        with pytest.raises(ValueError, match="elo_min"):
            chess_openings.Opening(
                name="bad", moves=("e2e4", "e7e5"),
                elo_min=1000, elo_max=500,
            )
