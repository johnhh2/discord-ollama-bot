"""Tier 6: game-mechanic helpers that previously had shallow coverage.

To make these meaningfully testable, three small helpers were extracted from
their callsites in src/games/:

- drop_in_column   (ttt_c4.py)   — Connect 4 gravity
- advance_player   (race.py)     — race position clamping
- dealer_play      (blackjack.py) — dealer hits on ≤16

Each test pins the *contract* of the helper so a future refactor that
changes gravity direction, finish-line clamping, or the dealer's stand
threshold fails loudly.

Chess special moves (castling, en-passant, promotion, check/checkmate) and
mini-cactpot reveal mechanics aren't tested here — those features either
aren't implemented or live in interactive command flows that require a
real Discord gateway.
"""

from src.games.ttt_c4 import drop_in_column, check_c4_winner
from src.games.race import advance_player
from src.games.blackjack import dealer_play, hand_value, new_deck
from src.config import RACE_TRACK_LEN


# ── Connect 4 gravity ─────────────────────────────────────────────────────────

def _empty_c4_board() -> list:
    return [[None] * 7 for _ in range(6)]


class TestDropInColumn:
    def test_drop_into_empty_column_lands_at_bottom(self):
        board = _empty_c4_board()
        assert drop_in_column(board, col=3) == 5  # bottom row index

    def test_drop_into_partially_full_column_lands_above_pieces(self):
        board = _empty_c4_board()
        # Place a piece at the bottom of column 0
        board[5][0] = "X"
        assert drop_in_column(board, col=0) == 4

    def test_drop_stacks_correctly_through_column(self):
        """Drop six pieces; each lands one row above the previous."""
        board = _empty_c4_board()
        landed_rows = []
        for _ in range(6):
            row = drop_in_column(board, col=2)
            assert row is not None
            board[row][2] = "X"
            landed_rows.append(row)
        assert landed_rows == [5, 4, 3, 2, 1, 0]

    def test_full_column_returns_none(self):
        board = _empty_c4_board()
        for r in range(6):
            board[r][4] = "Y"
        assert drop_in_column(board, col=4) is None

    def test_other_columns_unaffected_by_full_column(self):
        board = _empty_c4_board()
        for r in range(6):
            board[r][0] = "X"
        assert drop_in_column(board, col=0) is None
        assert drop_in_column(board, col=1) == 5  # adjacent col still empty

    def test_gravity_is_independent_per_column(self):
        board = _empty_c4_board()
        # Stack column 3 halfway full
        for r in range(5, 2, -1):
            board[r][3] = "X"
        # Column 3 next slot should be row 2
        assert drop_in_column(board, col=3) == 2
        # Column 6 still empty all the way down
        assert drop_in_column(board, col=6) == 5


class TestC4WinnerAfterDrop:
    """Compose drop_in_column + check_c4_winner to verify they cooperate."""
    def test_horizontal_four_in_bottom_row(self):
        board = _empty_c4_board()
        for col in range(4):
            row = drop_in_column(board, col)
            board[row][col] = "X"
        assert check_c4_winner(board) == "X"

    def test_vertical_four_in_one_column(self):
        board = _empty_c4_board()
        for _ in range(4):
            row = drop_in_column(board, col=2)
            board[row][2] = "O"
        assert check_c4_winner(board) == "O"


# ── Race ──────────────────────────────────────────────────────────────────────

class TestAdvancePlayer:
    def test_advance_within_track(self):
        assert advance_player(pos=0, delta=2) == 2
        assert advance_player(pos=5, delta=3) == 8

    def test_clamp_at_finish(self):
        assert advance_player(pos=RACE_TRACK_LEN - 1, delta=5) == RACE_TRACK_LEN

    def test_already_at_finish_stays_at_finish(self):
        assert advance_player(pos=RACE_TRACK_LEN, delta=10) == RACE_TRACK_LEN

    def test_zero_delta_is_no_op(self):
        assert advance_player(pos=7, delta=0) == 7

    def test_custom_finish_overrides_default(self):
        assert advance_player(pos=8, delta=5, finish=10) == 10

    def test_finish_detection_predicate(self):
        """Race winner check: positions[uid] >= finish.
        Pin both sides of the boundary."""
        not_yet = advance_player(pos=RACE_TRACK_LEN - 2, delta=1)
        assert not_yet < RACE_TRACK_LEN

        crossed = advance_player(pos=RACE_TRACK_LEN - 2, delta=2)
        assert crossed >= RACE_TRACK_LEN


# ── Blackjack dealer rule ─────────────────────────────────────────────────────

def _card(rank: str, suit: str = "♠") -> dict:
    """Minimal card dict matching new_deck()'s shape."""
    return {"rank": rank, "suit": suit}


class TestDealerPlay:
    def test_dealer_stands_on_hard_17(self):
        deck = [_card("9")]   # would push past 17 if drawn
        dealer = [_card("10"), _card("7")]   # hard 17
        dealer_play(deck, dealer)
        assert hand_value(dealer) == 17
        assert len(dealer) == 2  # didn't draw

    def test_dealer_hits_on_16_until_at_least_17(self):
        # Dealer starts at 16, draws — final hand >= 17.
        deck = [_card("5")]   # 16 + 5 = 21
        dealer = [_card("10"), _card("6")]
        dealer_play(deck, dealer)
        assert hand_value(dealer) >= 17
        assert len(dealer) == 3

    def test_dealer_hits_repeatedly_until_threshold(self):
        # Stack the deck so the dealer needs multiple hits.
        # Cards are drawn from the END of the deck (deck.pop), so the LAST card
        # in the list is drawn first.
        deck = [_card("9"), _card("4"), _card("3"), _card("2")]  # drawn order: 2,3,4,9
        dealer = [_card("5"), _card("3")]   # starts at 8
        dealer_play(deck, dealer)
        # 8 + 2 = 10, +3 = 13, +4 = 17 → stand. Should NOT consume the 9.
        assert hand_value(dealer) == 17
        assert deck == [_card("9")]  # one card left undrawn

    def test_dealer_can_bust(self):
        # Deck forces a bust on the dealer.
        deck = [_card("Q")]   # 16 + 10 = 26
        dealer = [_card("K"), _card("6")]
        dealer_play(deck, dealer)
        assert hand_value(dealer) > 21  # busted
        assert len(dealer) == 3

    def test_dealer_stands_on_soft_17_value_path(self):
        """Soft 17 = Ace + 6. The bot's rule (`<= 16`) means dealer stands on
        ANY 17, soft or hard. Pin that against a future "hit on soft 17" change.
        """
        # Build a soft 17: A + 6
        dealer = [_card("A"), _card("6")]
        assert hand_value(dealer) == 17  # soft 17
        deck = [_card("9")]
        dealer_play(deck, dealer)
        # Dealer stood; the 9 is still in the deck.
        assert len(dealer) == 2
        assert deck == [_card("9")]


class TestNewDeckIntegration:
    """Sanity check: dealer_play works with a fresh new_deck()."""
    def test_dealer_play_terminates_with_real_deck(self):
        # Worst case: dealer starts at 4, draws 1s forever — but new_deck has
        # a finite supply, and ranks include 10/J/Q/K which guarantee progress.
        deck = new_deck()
        dealer = [deck.pop(), deck.pop()]
        dealer_play(deck, dealer)
        assert hand_value(dealer) >= 17 or hand_value(dealer) > 21
