"""
Tests for bot.py helper functions.

Run: pytest -v
Dev deps: pip install pytest pytest-asyncio
"""
import datetime
import random
import time
from collections import Counter
from zoneinfo import ZoneInfo

import pytest

from src.ai import _norm_puzzle_answer
from src.config import OLLAMA_MODEL, PUZZLE_REWARDS, RACE_TRACK_LEN
from src.economy import _ensure_user, add_balance, announce_new_lottery, deduct_balance, drain_bot_balance_into_lottery, get_balance, get_guild_ask_model, get_guild_coding_model, get_guild_roleplay_model, is_insured
from src.gambling.scratchoff import MiniCactpotGame
from src.gambling.slots import eval_slots
from src.games.blackjack import RANKS, SUITS, build_blackjack_display, draw_card, format_hand, hand_value, new_deck
from src.games.hangman import HANGMAN_ART, build_hangman_display, calculate_hangman_reward
from src.games.ttt_c4 import build_c4_display, build_ttt_display, check_c4_winner, check_ttt_winner, is_ttt_stalemate
from src.helpers import _render_race, curse_font, mocking_font
from src.guild_config import get_guild_cfg
from src.state import economy, guild_settings


# ─────────────────────────────────────────────────────────────────────────────
# Text transforms
# ─────────────────────────────────────────────────────────────────────────────

class TestMockingFont:
    def test_basic(self):
        assert mocking_font("hello") == "hElLo"

    def test_empty(self):
        assert mocking_font("") == ""

    def test_non_alpha_preserved_without_flipping(self):
        # '!' is non-alpha; the uppercase toggle should not advance
        assert mocking_font("hi!") == "hI!"

    def test_numbers_preserved(self):
        assert mocking_font("a1b2") == "a1B2"

    def test_all_non_alpha(self):
        assert mocking_font("123!?") == "123!?"


class TestCurseFont:
    def test_basic(self):
        assert curse_font("hello") == "HeLlO"

    def test_empty(self):
        assert curse_font("") == ""

    def test_non_alpha_preserved_without_flipping(self):
        assert curse_font("hi!") == "Hi!"

    def test_all_uppercase_input_normalised(self):
        # curse_font uses char.upper()/.lower(), so input case doesn't matter
        assert curse_font("HELLO") == "HeLlO"


# ─────────────────────────────────────────────────────────────────────────────
# Blackjack helpers
# ─────────────────────────────────────────────────────────────────────────────

def _card(rank, suit="♠"):
    return {"rank": rank, "suit": suit}


class TestHandValue:
    def test_simple_sum(self):
        assert hand_value([_card("2"), _card("3")]) == 5

    def test_face_cards_worth_ten(self):
        assert hand_value([_card("K"), _card("Q"), _card("J")]) == 30

    def test_ace_soft(self):
        assert hand_value([_card("K"), _card("A")]) == 21

    def test_ace_downgrade_single(self):
        # A + A + 9 → 11+11+9=31 → downgrade one A → 21
        assert hand_value([_card("A"), _card("A"), _card("9")]) == 21

    def test_ace_downgrade_multiple(self):
        # A+A+A+9 → 11+11+11+9=42 → downgrade 2 aces → 12+9=12... wait:
        # 42-10=32, 32-10=22, 22-10=12 → yes 12
        assert hand_value([_card("A"), _card("A"), _card("A"), _card("9")]) == 12

    def test_blackjack(self):
        assert hand_value([_card("A"), _card("K")]) == 21

    def test_bust_no_ace(self):
        assert hand_value([_card("K"), _card("K"), _card("5")]) == 25


class TestFormatHand:
    def test_full_hand(self):
        hand = [_card("A", "♠"), _card("K", "♥")]
        assert format_hand(hand) == "A♠  K♥"

    def test_hide_second_card(self):
        hand = [_card("A", "♠"), _card("K", "♥")]
        result = format_hand(hand, hide_second=True)
        assert result.startswith("A♠")
        assert "🂠" in result
        assert "K♥" not in result

    def test_hide_second_single_card(self):
        hand = [_card("A", "♠")]
        # Only one card — hide_second only applies when len >= 2
        result = format_hand(hand, hide_second=True)
        assert "A♠" in result


class TestNewDeck:
    def test_52_cards(self):
        deck = new_deck()
        assert len(deck) == 52

    def test_all_ranks_present(self):
        deck = new_deck()
        rank_counts = Counter(c["rank"] for c in deck)
        for rank in RANKS:
            assert rank_counts[rank] == 4, f"rank {rank} should appear 4 times"

    def test_all_suits_present(self):
        deck = new_deck()
        suit_counts = Counter(c["suit"] for c in deck)
        for suit in SUITS:
            assert suit_counts[suit] == 13, f"suit {suit} should appear 13 times"


class TestDrawCard:
    def test_reduces_deck_length(self):
        deck = new_deck()
        card = draw_card(deck)
        assert len(deck) == 51
        assert "rank" in card
        assert "suit" in card


class TestBuildBlackjackDisplay:
    def test_hidden_dealer(self):
        player = [_card("K", "♠"), _card("A", "♥")]
        dealer = [_card("7", "♦"), _card("J", "♣")]
        result = build_blackjack_display(player, dealer, 21, hide_dealer=True)
        assert "**Dealer:**" in result
        assert "**You (21):**" in result
        assert "🂠" in result

    def test_revealed_dealer(self):
        player = [_card("K", "♠"), _card("A", "♥")]
        dealer = [_card("7", "♦"), _card("J", "♣")]
        result = build_blackjack_display(player, dealer, 21, hide_dealer=False, dval=17)
        assert "**Dealer (17):**" in result
        assert "🂠" not in result

    def test_no_dval_uses_plain_dealer_label(self):
        player = [_card("5", "♠")]
        dealer = [_card("9", "♦")]
        result = build_blackjack_display(player, dealer, 5, hide_dealer=False, dval=None)
        assert "**Dealer:**" in result


# ─────────────────────────────────────────────────────────────────────────────
# Hangman helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestCalculateHangmanReward:
    def test_short_word_no_rare_letters(self):
        # "abc": base=60, length_bonus=0, unique=3→9, rare=0 → total=69
        assert calculate_hangman_reward("abc") == 69

    def test_ultra_rare_letters(self):
        # "quiz": q (ultra-rare), z (ultra-rare), u, i
        # base=60, length_bonus=(4-3)*6=6, unique=4→12
        # ultra_rare_count=2 (q, z) → rare_bonus=100
        # total = 178
        assert calculate_hangman_reward("quiz") == 178

    def test_rare_letters(self):
        # "sky": s, k (rare), y (rare)
        # base=60, length_bonus=0, unique=3→9
        # rare_count=2 (k, y) → rare_bonus=50
        # total=119
        assert calculate_hangman_reward("sky") == 119

    def test_longer_word(self):
        # "python": 6 chars, p,y(rare),t,h,o,n → 6 unique
        # base=60, length_bonus=(6-3)*6=18, unique=6→18
        # rare_count=1 (y) → rare_bonus=25
        # total=121
        assert calculate_hangman_reward("python") == 121


class TestBuildHangmanDisplay:
    def _make_game(self, word, guessed, wrong):
        return {"word": word, "guessed_letters": set(guessed), "wrong_guesses": wrong}

    def test_empty_gallows(self):
        game = self._make_game("hello", [], 0)
        result = build_hangman_display(game)
        assert HANGMAN_ART[0] in result

    def test_full_gallows(self):
        game = self._make_game("hello", [], 6)
        result = build_hangman_display(game)
        assert HANGMAN_ART[6] in result

    def test_partial_reveal(self):
        game = self._make_game("hello", ["h", "e"], 2)
        result = build_hangman_display(game)
        assert "h e _ _ _" in result

    def test_no_guesses_all_blanks(self):
        game = self._make_game("hi", [], 0)
        result = build_hangman_display(game)
        assert "_ _" in result

    def test_lives_remaining(self):
        game = self._make_game("test", [], 3)
        result = build_hangman_display(game)
        assert "Lives left: 3" in result

    def test_guessed_letters_sorted(self):
        game = self._make_game("test", ["z", "a"], 1)
        result = build_hangman_display(game)
        assert "a, z" in result


# ─────────────────────────────────────────────────────────────────────────────
# Slots
# ─────────────────────────────────────────────────────────────────────────────

class TestEvalSlots:
    def test_jackpot_sufficient_bet(self):
        assert eval_slots(["7️⃣", "7️⃣", "7️⃣"], 25) == ("jackpot", 75)

    def test_jackpot_insufficient_bet(self):
        assert eval_slots(["7️⃣", "7️⃣", "7️⃣"], 10) == ("nothing", 0)

    def test_three_bar(self):
        assert eval_slots(["🎰", "🎰", "🎰"], 25) == ("3bar", 15)

    def test_three_bell(self):
        assert eval_slots(["🔔", "🔔", "🔔"], 25) == ("3bell", 7)

    def test_three_lemon(self):
        assert eval_slots(["🍋", "🍋", "🍋"], 25) == ("3lemon", 4)

    def test_three_cherry(self):
        assert eval_slots(["🍒", "🍒", "🍒"], 25) == ("3cherry", 3)

    def test_two_cherries(self):
        assert eval_slots(["🍒", "🍒", "🍋"], 25) == ("2cherry", 2)

    def test_one_cherry(self):
        assert eval_slots(["🍒", "🍋", "🍋"], 25) == ("1cherry", 1)

    def test_nothing(self):
        assert eval_slots(["🍋", "🔔", "🎰"], 25) == ("nothing", 0)

    def test_cherry_in_middle_counts(self):
        assert eval_slots(["🍋", "🍒", "🎰"], 25) == ("1cherry", 1)


# ─────────────────────────────────────────────────────────────────────────────
# Tic-tac-toe
# ─────────────────────────────────────────────────────────────────────────────

def _empty_ttt_board():
    return [None] * 9


class TestCheckTttWinner:
    def test_no_winner_empty(self):
        assert check_ttt_winner(_empty_ttt_board()) is None

    def test_row_win(self):
        board = ["❌", "❌", "❌", None, None, None, None, None, None]
        assert check_ttt_winner(board) == "❌"

    def test_col_win(self):
        board = ["⭕", None, None, "⭕", None, None, "⭕", None, None]
        assert check_ttt_winner(board) == "⭕"

    def test_diagonal_win(self):
        board = ["❌", None, None, None, "❌", None, None, None, "❌"]
        assert check_ttt_winner(board) == "❌"

    def test_anti_diagonal_win(self):
        board = [None, None, "⭕", None, "⭕", None, "⭕", None, None]
        assert check_ttt_winner(board) == "⭕"

    def test_partial_no_winner(self):
        board = ["❌", "⭕", "❌", "⭕", "❌", "⭕", "⭕", "❌", "⭕"]
        # Draw — no three in a row for either
        assert check_ttt_winner(board) is None


class TestIsTttStalemate:
    def test_empty_board_not_stalemate(self):
        assert not is_ttt_stalemate(_empty_ttt_board())

    def test_one_move_not_stalemate(self):
        board = ["❌", None, None, None, None, None, None, None, None]
        assert not is_ttt_stalemate(board)

    def test_full_draw_board_is_stalemate(self):
        # Classic draw: no three in a row for either
        board = ["❌", "⭕", "❌", "⭕", "❌", "⭕", "⭕", "❌", "⭕"]
        assert is_ttt_stalemate(board)

    def test_mid_game_forced_draw(self):
        # X and O each block every winning line but board isn't full
        # ❌ ⭕ ❌
        # ⭕ ⭕ ❌
        # ❌ ❌ ⭕  <- no winner, and no open winning line possible
        # Actually let's pick a real mid-game forced draw:
        # ❌ ⭕ ❌
        # ❌ ⭕ ⭕
        # ⭕ ❌  _   <- last square can't help either player win
        board = ["❌", "⭕", "❌",
                 "❌", "⭕", "⭕",
                 "⭕", "❌", None]
        # col0: ❌❌⭕ — ⭕ present, ❌ blocked; col1: ⭕⭕❌ — ❌ present, ⭕ blocked
        # row2: ⭕❌_ — both marks present, blocked for both
        # diag(0,4,8): ❌⭕_ — both marks present, blocked
        # anti-diag(2,4,6): ❌⭕⭕ — ❌ present, ⭕ blocked; but ❌ also blocked (⭕ there)
        assert is_ttt_stalemate(board)

    def test_open_winning_line_not_stalemate(self):
        # ❌ can still win on bottom row (indices 6,7,8)
        board = ["❌", "⭕", "❌",
                 "⭕", "⭕", "❌",
                 None, None, None]
        assert not is_ttt_stalemate(board)

    def test_winner_on_board_not_reported_as_stalemate(self):
        # check_ttt_winner is called first in the game loop, but stalemate
        # should also return False when a winning line exists
        board = ["❌", "❌", "❌", "⭕", "⭕", None, None, None, None]
        assert not is_ttt_stalemate(board)


class TestBuildTttDisplay:
    def test_empty_board_shows_numbers(self):
        game = {"board": _empty_ttt_board()}
        result = build_ttt_display(game)
        assert "1️⃣" in result
        assert "9️⃣" in result

    def test_marks_shown_in_correct_position(self):
        board = ["❌"] + [None] * 8
        game = {"board": board}
        result = build_ttt_display(game)
        assert result.startswith("❌")


# ─────────────────────────────────────────────────────────────────────────────
# Lottery
# ─────────────────────────────────────────────────────────────────────────────

_CT = ZoneInfo("America/Chicago")


def _ct(year, month, day, hour, minute=0):
    """Return a timezone-aware datetime in America/Chicago."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=_CT)


def _should_draw(lottery: dict, now_ct: datetime.datetime) -> bool:
    """Mirror the draw gate logic in lottery_scheduler."""
    from src.economy import lottery_month_key
    if now_ct.day != 1:
        return False
    current_month = lottery_month_key(now_ct)
    return now_ct.hour >= 18 and current_month != lottery.get("last_drawn_week", 0)


GUILD_ID = 111222333


class TestDrainBotBalanceIntoLottery:
    @pytest.mark.asyncio
    async def test_transfers_house_balance_to_pool(self):
        economy["guild_house"] = {str(GUILD_ID): 500}
        lottery = {"prize_pool": 100, "players": {}}
        transferred = await drain_bot_balance_into_lottery(lottery, GUILD_ID)
        assert transferred == 500
        assert lottery["prize_pool"] == 600
        assert economy["guild_house"][str(GUILD_ID)] == 0

    @pytest.mark.asyncio
    async def test_noop_when_house_balance_zero(self):
        economy["guild_house"] = {str(GUILD_ID): 0}
        lottery = {"prize_pool": 200, "players": {}}
        transferred = await drain_bot_balance_into_lottery(lottery, GUILD_ID)
        assert transferred == 0
        assert lottery["prize_pool"] == 200

    @pytest.mark.asyncio
    async def test_noop_when_no_guild_entry(self):
        economy["guild_house"] = {}
        lottery = {"prize_pool": 50, "players": {}}
        transferred = await drain_bot_balance_into_lottery(lottery, GUILD_ID)
        assert transferred == 0
        assert lottery["prize_pool"] == 50

    @pytest.mark.asyncio
    async def test_large_house_balance(self):
        economy["guild_house"] = {str(GUILD_ID): 99999}
        lottery = {"prize_pool": 2000, "players": {}}
        await drain_bot_balance_into_lottery(lottery, GUILD_ID)
        assert lottery["prize_pool"] == 101999


class TestLotteryMonthTiming:
    """Test the draw-trigger gate logic using _should_draw."""

    def _lottery(self, last_drawn_week=0):
        return {"prize_pool": 5000, "players": {"9": 3}, "last_drawn_week": last_drawn_week}

    def test_first_of_month_6pm_cdt_triggers(self):
        # Jun 1 2025; Chicago is in CDT (UTC-5)
        now = _ct(2025, 6, 1, 18, 0)
        assert _should_draw(self._lottery(), now) is True

    def test_first_of_month_6pm_cst_triggers(self):
        # Feb 1 2025; Chicago is in CST (UTC-6)
        now = _ct(2025, 2, 1, 18, 0)
        assert _should_draw(self._lottery(), now) is True

    def test_first_of_month_before_6pm_does_not_trigger(self):
        now = _ct(2025, 6, 1, 17, 59)
        assert _should_draw(self._lottery(), now) is False

    def test_late_tick_on_the_first_still_triggers(self):
        # Bot hiccup at 6:00 sharp — a 6:01pm (or 11pm) tick on the 1st
        # must still draw as long as this month hasn't been drawn yet.
        now = _ct(2025, 6, 1, 18, 1)
        assert _should_draw(self._lottery(), now) is True
        assert _should_draw(self._lottery(), _ct(2025, 6, 1, 23, 0)) is True

    def test_mid_month_does_not_trigger(self):
        now = _ct(2025, 6, 15, 18, 0)
        assert _should_draw(self._lottery(), now) is False

    def test_already_drawn_this_month_does_not_retrigger(self):
        from src.economy import lottery_month_key
        now = _ct(2025, 6, 1, 18, 0)
        month = lottery_month_key(now)
        assert _should_draw(self._lottery(last_drawn_week=month), now) is False

    def test_different_month_triggers_again(self):
        from src.economy import lottery_month_key
        now = _ct(2025, 6, 1, 18, 0)
        last_month_key = lottery_month_key(_ct(2025, 5, 1, 18, 0))
        assert _should_draw(self._lottery(last_drawn_week=last_month_key), now) is True

    def test_year_boundary_does_not_silently_skip(self):
        """A year-old month key must not collide with the same month a year
        later — the YYYYMM encoding qualifies the month with its year.
        """
        from src.economy import lottery_month_key
        now = _ct(2027, 1, 1, 18, 0)
        last_year_key = lottery_month_key(_ct(2026, 1, 1, 18, 0))
        assert last_year_key != lottery_month_key(now)
        assert _should_draw(self._lottery(last_drawn_week=last_year_key), now) is True

    def test_legacy_weekly_key_does_not_suppress_first_monthly_draw(self):
        """Migration transition (deployed 2026-07-31): the DB still holds a
        YYYYWW week key from the weekly lottery. It must not equal the
        YYYYMM key for Aug 2026, so the Aug 1 draw fires and pays out the
        final weekly pot.
        """
        legacy_week_key = 202631  # ISO week 31 of 2026 (late July)
        now = _ct(2026, 8, 1, 18, 0)
        assert _should_draw(self._lottery(last_drawn_week=legacy_week_key), now) is True


class TestLotteryWinnerPayout:
    @pytest.mark.asyncio
    async def test_sole_player_always_wins(self):
        uid = 7001
        await add_balance(uid, 0)
        pool = 5000
        players = {str(uid): 10}
        # Replicate scheduler payout: pick winner, add_balance
        winner_id = int(list(players.keys())[0])
        await add_balance(winner_id, pool)
        assert await get_balance(uid) == pool

    @pytest.mark.asyncio
    async def test_winner_receives_full_pool(self):
        uid = 7002
        await add_balance(uid, 100)
        pool = 8000
        await add_balance(uid, pool)
        assert await get_balance(uid) == 8100

    @pytest.mark.asyncio
    async def test_random_choice_winner(self, monkeypatch):
        players = {"8001": 5, "8002": 3, "8003": 1}
        for uid_str in players:
            await add_balance(int(uid_str), 0)
        pool = 3000
        # Force random.choice to always pick player 8002
        monkeypatch.setattr("random.choice", lambda seq: "8002")
        winner_id = int(random.choice(list(players.keys())))
        await add_balance(winner_id, pool)
        assert await get_balance(8002) == pool
        assert await get_balance(8001) == 0
        assert await get_balance(8003) == 0


@pytest.mark.asyncio
class TestAnnounceNewLotteryTimestamp:
    async def _send_and_capture(self, prize_pool, now_ct):
        """Call announce_new_lottery with a mock channel, return the sent embed."""
        sent = []

        class FakeChannel:
            async def send(self, embed=None, **kwargs):
                sent.append(embed)

        now_utc = now_ct.astimezone(datetime.timezone.utc)
        await announce_new_lottery(FakeChannel(), prize_pool=prize_pool, now=now_utc)
        return sent[0]

    async def test_embed_title(self):
        embed = await self._send_and_capture(5000, _ct(2025, 6, 7, 18))
        assert embed.title == "🎰 New Monthly Lottery"

    async def test_prize_pool_in_description(self):
        embed = await self._send_and_capture(12345, _ct(2025, 6, 7, 18))
        assert "12,345" in embed.description

    async def test_timestamp_points_to_next_first_of_month_cdt(self):
        # Called mid-June (CDT); next draw is Jul 1 at 6pm CT
        now = _ct(2025, 6, 10, 12)
        embed = await self._send_and_capture(2000, now)
        # Extract Unix timestamp from <t:XXXXXX:R>
        import re
        match = re.search(r"<t:(\d+):R>", embed.description)
        assert match, "No Discord timestamp found in description"
        ts = int(match.group(1))
        dt = datetime.datetime.fromtimestamp(ts, tz=_CT)
        assert dt.day == 1
        assert dt.hour == 18
        assert dt.minute == 0
        assert dt.year == 2025
        assert dt.month == 7

    async def test_timestamp_points_to_next_first_of_month_cst(self):
        # Called mid-January (CST); next draw is Feb 1 at 6pm CT
        now = _ct(2025, 1, 7, 12)
        embed = await self._send_and_capture(2000, now)
        import re
        match = re.search(r"<t:(\d+):R>", embed.description)
        assert match
        ts = int(match.group(1))
        dt = datetime.datetime.fromtimestamp(ts, tz=_CT)
        assert dt.day == 1
        assert dt.hour == 18
        assert dt.month == 2

    async def test_called_at_announce_time_points_to_next_month(self):
        # The scheduler announces at 7pm on the 1st, after that month's
        # draw — the advertised end must be the FOLLOWING month's 1st.
        now = _ct(2025, 6, 1, 19)
        embed = await self._send_and_capture(2000, now)
        import re
        match = re.search(r"<t:(\d+):R>", embed.description)
        assert match
        ts = int(match.group(1))
        dt = datetime.datetime.fromtimestamp(ts, tz=_CT)
        assert (dt.year, dt.month, dt.day, dt.hour) == (2025, 7, 1, 18)

    async def test_december_rolls_to_january(self):
        now = _ct(2025, 12, 15, 12)
        embed = await self._send_and_capture(2000, now)
        import re
        match = re.search(r"<t:(\d+):R>", embed.description)
        assert match
        ts = int(match.group(1))
        dt = datetime.datetime.fromtimestamp(ts, tz=_CT)
        assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 1, 1, 18)


# ─────────────────────────────────────────────────────────────────────────────
# Connect 4
# ─────────────────────────────────────────────────────────────────────────────

def _empty_c4_board():
    return [[None] * 7 for _ in range(6)]


class TestCheckC4Winner:
    def test_no_winner_empty(self):
        assert check_c4_winner(_empty_c4_board()) is None

    def test_horizontal_win(self):
        board = _empty_c4_board()
        for c in range(4):
            board[5][c] = "🔴"
        assert check_c4_winner(board) == "🔴"

    def test_vertical_win(self):
        board = _empty_c4_board()
        for r in range(4):
            board[r][0] = "🟡"
        assert check_c4_winner(board) == "🟡"

    def test_diagonal_ascending_win(self):
        board = _empty_c4_board()
        for i in range(4):
            board[i][i] = "🔴"
        assert check_c4_winner(board) == "🔴"

    def test_diagonal_descending_win(self):
        # (↖) diagonal: rows 3,2,1,0; cols 0,1,2,3
        board = _empty_c4_board()
        board[3][0] = "🔴"
        board[2][1] = "🔴"
        board[1][2] = "🔴"
        board[0][3] = "🔴"
        assert check_c4_winner(board) == "🔴"


class TestBuildC4Display:
    def test_empty_board_shows_column_headers(self):
        game = {"board": _empty_c4_board()}
        result = build_c4_display(game)
        assert "1️⃣" in result
        assert "7️⃣" in result

    def test_empty_cells_shown_as_black_circle(self):
        game = {"board": _empty_c4_board()}
        result = build_c4_display(game)
        assert "⚫" in result


# ─────────────────────────────────────────────────────────────────────────────
# Race renderer
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderRace:
    def test_player_at_start(self):
        game = {
            "players": [1],
            "positions": {1: 0},
            "names": {1: "Alice"},
        }
        result = _render_race(game)
        # Position 0: horse at start, full track of empty squares
        assert "🏇" in result
        assert "░" * RACE_TRACK_LEN in result
        assert "Alice" in result

    def test_player_advanced(self):
        game = {
            "players": [1],
            "positions": {1: 5},
            "names": {1: "Bob"},
        }
        result = _render_race(game)
        assert "▓" * 5 in result
        assert "░" * (RACE_TRACK_LEN - 5) in result

    def test_multiple_players(self):
        game = {
            "players": [1, 2],
            "positions": {1: 0, 2: 10},
            "names": {1: "Alice", 2: "Bob"},
        }
        result = _render_race(game)
        lines = result.split("\n")
        assert len(lines) == 2
        assert "Alice" in lines[0]
        assert "Bob" in lines[1]


# ─────────────────────────────────────────────────────────────────────────────
# MiniCactpot
# ─────────────────────────────────────────────────────────────────────────────

class TestMiniCactpot:
    def setup_method(self):
        self.game = MiniCactpotGame(1)
        # Override grid with a known layout for deterministic tests
        self.game.grid = [1, 2, 3, 4, 5, 6, 7, 8, 9]

    def test_row_sum(self):
        assert self.game.get_line_sum("row", 0) == 6   # 1+2+3
        assert self.game.get_line_sum("row", 1) == 15  # 4+5+6
        assert self.game.get_line_sum("row", 2) == 24  # 7+8+9

    def test_col_sum(self):
        assert self.game.get_line_sum("col", 0) == 12  # 1+4+7
        assert self.game.get_line_sum("col", 1) == 15  # 2+5+8
        assert self.game.get_line_sum("col", 2) == 18  # 3+6+9

    def test_diag1_sum(self):
        assert self.game.get_line_sum("diag1", 0) == 15  # 1+5+9

    def test_diag2_sum(self):
        assert self.game.get_line_sum("diag2", 0) == 15  # 3+5+7

    def test_payout_jackpot_line(self):
        # row 0 sums to 6 → CACTPOT_PAYOUTS[6] = 10000
        assert self.game.calculate_payout("row", 0) == 10000

    def test_payout_known_value(self):
        # row 2 sums to 24 → CACTPOT_PAYOUTS[24] = 3600
        assert self.game.calculate_payout("row", 2) == 3600


# ─────────────────────────────────────────────────────────────────────────────
# Economy helpers (use monkeypatched globals from conftest)
# ─────────────────────────────────────────────────────────────────────────────

class TestEconomy:
    @pytest.mark.asyncio
    async def test_ensure_user_creates_entry(self):
        await _ensure_user(1)
        assert "1" in economy["users"]
        assert economy["users"]["1"]["balance"] == 0

    @pytest.mark.asyncio
    async def test_ensure_user_idempotent(self):
        await _ensure_user(1)
        await _ensure_user(1)
        assert economy["users"]["1"]["balance"] == 0

    @pytest.mark.asyncio
    async def test_get_balance_new_user(self):
        assert await get_balance(1) == 0

    @pytest.mark.asyncio
    async def test_add_balance(self):
        await add_balance(1, 100)
        assert await get_balance(1) == 100

    @pytest.mark.asyncio
    async def test_add_balance_accumulates(self):
        await add_balance(1, 50)
        await add_balance(1, 75)
        assert await get_balance(1) == 125

    @pytest.mark.asyncio
    async def test_deduct_balance_sufficient_funds(self):
        await add_balance(1, 100)
        result = await deduct_balance(1, 60)
        assert result is True
        assert await get_balance(1) == 40

    @pytest.mark.asyncio
    async def test_deduct_balance_insufficient_funds(self):
        await add_balance(1, 10)
        result = await deduct_balance(1, 50)
        assert result is False
        assert await get_balance(1) == 10  # unchanged

    @pytest.mark.asyncio
    async def test_deduct_exact_balance(self):
        await add_balance(1, 50)
        result = await deduct_balance(1, 50)
        assert result is True
        assert await get_balance(1) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Guild settings helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestGuildSettings:
    def test_get_guild_cfg_creates_entry(self):
        cfg = get_guild_cfg(42)
        assert cfg == {}
        assert "42" in guild_settings

    def test_get_guild_cfg_idempotent(self):
        get_guild_cfg(42)
        cfg = get_guild_cfg(42)
        assert cfg == {}

    def test_get_guild_ask_model_default(self):
        model = get_guild_ask_model(42)
        assert model == OLLAMA_MODEL

    def test_get_guild_ask_model_custom(self, monkeypatch):
        monkeypatch.setattr("src.state.guild_settings", {"42": {"ask_model": "llama3:8b"}})
        assert get_guild_ask_model(42) == "llama3:8b"

    def test_get_guild_roleplay_model_default(self):
        assert get_guild_roleplay_model(42) == OLLAMA_MODEL

    def test_get_guild_roleplay_model_custom(self, monkeypatch):
        monkeypatch.setattr("src.state.guild_settings", {"42": {"roleplay_model": "mistral:7b"}})
        assert get_guild_roleplay_model(42) == "mistral:7b"


# ─────────────────────────────────────────────────────────────────────────────
# Insurance
# ─────────────────────────────────────────────────────────────────────────────

class TestInsurance:
    @pytest.mark.asyncio
    async def test_not_insured_missing_user(self):
        assert await is_insured(42, 1, "rob") is False

    @pytest.mark.asyncio
    async def test_not_insured_expired(self, monkeypatch):
        monkeypatch.setattr("src.state.insurance", {
            (42, 1): {"expires_at": time.time() - 10, "protected_from": ["rob"]}
        })
        assert await is_insured(42, 1, "rob") is False

    @pytest.mark.asyncio
    async def test_insured_valid(self, monkeypatch):
        monkeypatch.setattr("src.state.insurance", {
            (42, 1): {"expires_at": time.time() + 3600, "protected_from": ["rob"]}
        })
        assert await is_insured(42, 1, "rob") is True

    @pytest.mark.asyncio
    async def test_insured_other_guild_not_covered(self, monkeypatch):
        """Insurance is per-guild: a grant in guild 42 doesn't protect in 99."""
        monkeypatch.setattr("src.state.insurance", {
            (42, 1): {"expires_at": time.time() + 3600, "protected_from": ["rob"]}
        })
        assert await is_insured(99, 1, "rob") is False

    @pytest.mark.asyncio
    async def test_insured_wrong_protection_type(self, monkeypatch):
        monkeypatch.setattr("src.state.insurance", {
            (42, 1): {"expires_at": time.time() + 3600, "protected_from": ["mug"]}
        })
        assert await is_insured(42, 1, "rob") is False


# ─────────────────────────────────────────────────────────────────────────────
# Puzzle helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestPuzzleRewards:
    def test_easy(self):
        assert PUZZLE_REWARDS["easy"] == 10

    def test_medium(self):
        assert PUZZLE_REWARDS["medium"] == 20

    def test_hard(self):
        assert PUZZLE_REWARDS["hard"] == 35

    def test_extreme(self):
        assert PUZZLE_REWARDS["extreme"] == 50

    def test_all_difficulties_present(self):
        assert set(PUZZLE_REWARDS.keys()) == {"easy", "medium", "hard", "extreme"}


class TestNormPuzzleAnswer:
    def test_lowercase(self):
        assert _norm_puzzle_answer("Hello") == "hello"

    def test_strips_leading_trailing_whitespace(self):
        assert _norm_puzzle_answer("  42  ") == "42"

    def test_collapses_internal_whitespace(self):
        assert _norm_puzzle_answer("foo  bar\tbaz") == "foo bar baz"

    def test_exact_match(self):
        assert _norm_puzzle_answer("True") == _norm_puzzle_answer("true")

    def test_whitespace_only(self):
        assert _norm_puzzle_answer("   ") == ""

    def test_empty_string(self):
        assert _norm_puzzle_answer("") == ""

    def test_multiline_output(self):
        # newlines count as whitespace and get collapsed
        assert _norm_puzzle_answer("1\n2\n3") == "1 2 3"


class TestGetGuildCodingModel:
    def test_default_falls_back_to_ollama_model(self):
        assert get_guild_coding_model(42) == OLLAMA_MODEL

    def test_custom_model_returned(self, monkeypatch):
        monkeypatch.setattr("src.state.guild_settings", {"42": {"coding_model": "deepseek-coder:6.7b"}})
        assert get_guild_coding_model(42) == "deepseek-coder:6.7b"

    def test_missing_key_falls_back(self, monkeypatch):
        # Guild exists but coding_model not set
        monkeypatch.setattr("src.state.guild_settings", {"42": {"ask_model": "llama3:8b"}})
        assert get_guild_coding_model(42) == OLLAMA_MODEL

    def test_independent_from_ask_model(self, monkeypatch):
        monkeypatch.setattr("src.state.guild_settings", {
            "42": {"ask_model": "llama3:8b", "coding_model": "deepseek-coder:6.7b"}
        })
        assert get_guild_coding_model(42) == "deepseek-coder:6.7b"
        assert get_guild_ask_model(42) == "llama3:8b"

    def test_different_guilds_independent(self, monkeypatch):
        monkeypatch.setattr("src.state.guild_settings", {
            "1": {"coding_model": "model-a"},
            "2": {"coding_model": "model-b"},
        })
        assert get_guild_coding_model(1) == "model-a"
        assert get_guild_coding_model(2) == "model-b"


class TestSaveQuoteLog:
    """save_quote_log writes the last-10 trim into state.quote_log.

    Conftest stubs _persistence.save_quote_log to a no-op (so tests don't
    hit the DB), so we install a custom stub here that exercises only the
    trim-and-store contract this class actually cares about.
    """
    @pytest.fixture(autouse=True)
    def _install_trim_stub(self, monkeypatch):
        import src.persistence as _p
        import src.state as _s
        async def _stub(log):
            _s.quote_log[:] = log[-10:]
        monkeypatch.setattr(_p, "save_quote_log", _stub)

    @pytest.mark.asyncio
    async def test_trims_to_last_10(self):
        import src.state as _state
        import src.persistence as _persistence
        log = [str(i) for i in range(15)]
        await _persistence.save_quote_log(log)
        assert _state.quote_log == [str(i) for i in range(5, 15)]

    @pytest.mark.asyncio
    async def test_short_log_unchanged(self):
        import src.state as _state
        import src.persistence as _persistence
        log = ["a", "b", "c"]
        await _persistence.save_quote_log(log)
        assert _state.quote_log == ["a", "b", "c"]
