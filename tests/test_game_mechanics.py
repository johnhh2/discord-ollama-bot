"""Tier 6: game-mechanic helpers that previously had shallow coverage.

To make these meaningfully testable, three small helpers were extracted from
their callsites in src/games/:

- drop_in_column   (ttt_c4.py)   — Connect 4 gravity
- settle_tick      (race.py)     — race movement + photo finish
- dealer_play      (blackjack.py) — dealer hits on ≤16

Each test pins the *contract* of the helper so a future refactor that
changes gravity direction, photo-finish ranking, or the dealer's stand
threshold fails loudly.

Chess special moves (castling, en-passant, promotion, check/checkmate) and
mini-cactpot reveal mechanics aren't tested here — those features either
aren't implemented or live in interactive command flows that require a
real Discord gateway.
"""

from src.games.ttt_c4 import drop_in_column, check_c4_winner
from src.games.race import settle_tick
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

class TestSettleTick:
    A, B, C = 1, 2, 3

    def test_advance_within_track(self):
        pos = {self.A: 0, self.B: 5}
        assert settle_tick(pos, {self.A: 2, self.B: 3}) == []
        assert pos == {self.A: 2, self.B: 8}

    def test_lone_crosser_is_drawn_on_the_line(self):
        pos = {self.A: RACE_TRACK_LEN - 1, self.B: 4}
        assert settle_tick(pos, {self.A: 3, self.B: 1}) == [self.A]
        assert pos == {self.A: RACE_TRACK_LEN, self.B: 5}

    def test_already_at_finish_stays_at_finish(self):
        pos = {self.A: RACE_TRACK_LEN}
        assert settle_tick(pos, {self.A: 3}) == [self.A]
        assert pos[self.A] == RACE_TRACK_LEN

    def test_photo_finish_furthest_past_the_line_wins(self):
        """Both cross this tick, but A would have run 2 squares further.
        A is the sole winner; B is drawn one square short of the line."""
        pos = {self.A: RACE_TRACK_LEN - 1, self.B: RACE_TRACK_LEN - 2}
        assert settle_tick(pos, {self.A: 3, self.B: 2}) == [self.A]
        assert pos[self.A] == RACE_TRACK_LEN
        assert pos[self.B] == RACE_TRACK_LEN - 1

    def test_photo_finish_loser_never_advances_past_the_short_square(self):
        """A horse already one short that loses the photo finish stays put."""
        pos = {self.A: RACE_TRACK_LEN - 1, self.B: RACE_TRACK_LEN - 1}
        assert settle_tick(pos, {self.A: 1, self.B: 3}) == [self.B]
        assert pos[self.A] == RACE_TRACK_LEN - 1

    def test_same_distance_past_the_line_still_ties(self):
        pos = {self.A: RACE_TRACK_LEN - 2, self.B: RACE_TRACK_LEN - 1}
        assert settle_tick(pos, {self.A: 3, self.B: 2}) == [self.A, self.B]
        assert pos == {self.A: RACE_TRACK_LEN, self.B: RACE_TRACK_LEN}

    def test_three_way_photo_finish_ranks_all_crossers(self):
        pos = {self.A: 17, self.B: 18, self.C: 19}
        # raw: A=20, B=21, C=21 -> B and C tie, A loses the photo finish
        assert settle_tick(pos, {self.A: 3, self.B: 3, self.C: 2}) == [self.B, self.C]
        assert pos == {self.A: RACE_TRACK_LEN - 1, self.B: RACE_TRACK_LEN, self.C: RACE_TRACK_LEN}

    def test_winners_follow_positions_order(self):
        pos = {self.C: 19, self.A: 19}
        assert settle_tick(pos, {self.C: 2, self.A: 2}) == [self.C, self.A]

    def test_custom_finish_overrides_default(self):
        pos = {self.A: 8, self.B: 7}
        assert settle_tick(pos, {self.A: 3, self.B: 3}, finish=10) == [self.A]
        assert pos == {self.A: 10, self.B: 9}


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
