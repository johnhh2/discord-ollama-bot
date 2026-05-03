"""
Tests for bot.py helper functions.

Run: pytest -v
Dev deps: pip install pytest pytest-asyncio
"""
import time
from collections import Counter

import pytest

import src as bot


# ─────────────────────────────────────────────────────────────────────────────
# Text transforms
# ─────────────────────────────────────────────────────────────────────────────

class TestMockingFont:
    def test_basic(self):
        assert bot.mocking_font("hello") == "hElLo"

    def test_empty(self):
        assert bot.mocking_font("") == ""

    def test_non_alpha_preserved_without_flipping(self):
        # '!' is non-alpha; the uppercase toggle should not advance
        assert bot.mocking_font("hi!") == "hI!"

    def test_numbers_preserved(self):
        assert bot.mocking_font("a1b2") == "a1B2"

    def test_all_non_alpha(self):
        assert bot.mocking_font("123!?") == "123!?"


class TestCurseFont:
    def test_basic(self):
        assert bot.curse_font("hello") == "HeLlO"

    def test_empty(self):
        assert bot.curse_font("") == ""

    def test_non_alpha_preserved_without_flipping(self):
        assert bot.curse_font("hi!") == "Hi!"

    def test_all_uppercase_input_normalised(self):
        # curse_font uses char.upper()/.lower(), so input case doesn't matter
        assert bot.curse_font("HELLO") == "HeLlO"


# ─────────────────────────────────────────────────────────────────────────────
# Blackjack helpers
# ─────────────────────────────────────────────────────────────────────────────

def _card(rank, suit="♠"):
    return {"rank": rank, "suit": suit}


class TestHandValue:
    def test_simple_sum(self):
        assert bot.hand_value([_card("2"), _card("3")]) == 5

    def test_face_cards_worth_ten(self):
        assert bot.hand_value([_card("K"), _card("Q"), _card("J")]) == 30

    def test_ace_soft(self):
        assert bot.hand_value([_card("K"), _card("A")]) == 21

    def test_ace_downgrade_single(self):
        # A + A + 9 → 11+11+9=31 → downgrade one A → 21
        assert bot.hand_value([_card("A"), _card("A"), _card("9")]) == 21

    def test_ace_downgrade_multiple(self):
        # A+A+A+9 → 11+11+11+9=42 → downgrade 2 aces → 12+9=12... wait:
        # 42-10=32, 32-10=22, 22-10=12 → yes 12
        assert bot.hand_value([_card("A"), _card("A"), _card("A"), _card("9")]) == 12

    def test_blackjack(self):
        assert bot.hand_value([_card("A"), _card("K")]) == 21

    def test_bust_no_ace(self):
        assert bot.hand_value([_card("K"), _card("K"), _card("5")]) == 25


class TestFormatHand:
    def test_full_hand(self):
        hand = [_card("A", "♠"), _card("K", "♥")]
        assert bot.format_hand(hand) == "A♠  K♥"

    def test_hide_second_card(self):
        hand = [_card("A", "♠"), _card("K", "♥")]
        result = bot.format_hand(hand, hide_second=True)
        assert result.startswith("A♠")
        assert "🂠" in result
        assert "K♥" not in result

    def test_hide_second_single_card(self):
        hand = [_card("A", "♠")]
        # Only one card — hide_second only applies when len >= 2
        result = bot.format_hand(hand, hide_second=True)
        assert "A♠" in result


class TestNewDeck:
    def test_52_cards(self):
        deck = bot.new_deck()
        assert len(deck) == 52

    def test_all_ranks_present(self):
        deck = bot.new_deck()
        rank_counts = Counter(c["rank"] for c in deck)
        for rank in bot.RANKS:
            assert rank_counts[rank] == 4, f"rank {rank} should appear 4 times"

    def test_all_suits_present(self):
        deck = bot.new_deck()
        suit_counts = Counter(c["suit"] for c in deck)
        for suit in bot.SUITS:
            assert suit_counts[suit] == 13, f"suit {suit} should appear 13 times"


class TestDrawCard:
    def test_reduces_deck_length(self):
        deck = bot.new_deck()
        card = bot.draw_card(deck)
        assert len(deck) == 51
        assert "rank" in card
        assert "suit" in card


class TestBuildBlackjackDisplay:
    def test_hidden_dealer(self):
        player = [_card("K", "♠"), _card("A", "♥")]
        dealer = [_card("7", "♦"), _card("J", "♣")]
        result = bot.build_blackjack_display(player, dealer, 21, hide_dealer=True)
        assert "**Dealer:**" in result
        assert "**You (21):**" in result
        assert "🂠" in result

    def test_revealed_dealer(self):
        player = [_card("K", "♠"), _card("A", "♥")]
        dealer = [_card("7", "♦"), _card("J", "♣")]
        result = bot.build_blackjack_display(player, dealer, 21, hide_dealer=False, dval=17)
        assert "**Dealer (17):**" in result
        assert "🂠" not in result

    def test_no_dval_uses_plain_dealer_label(self):
        player = [_card("5", "♠")]
        dealer = [_card("9", "♦")]
        result = bot.build_blackjack_display(player, dealer, 5, hide_dealer=False, dval=None)
        assert "**Dealer:**" in result


# ─────────────────────────────────────────────────────────────────────────────
# Hangman helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestCalculateHangmanReward:
    def test_short_word_no_rare_letters(self):
        # "abc": base=10, length_bonus=0, unique=3→9, rare=0 → total=19
        assert bot.calculate_hangman_reward("abc") == 19

    def test_ultra_rare_letters(self):
        # "quiz": q (ultra-rare), z (ultra-rare), u, i
        # base=10, length_bonus=(4-3)*6=6, unique=4→12
        # ultra_rare_count=2 (q, z) → rare_bonus=100
        # total = 128
        assert bot.calculate_hangman_reward("quiz") == 128

    def test_rare_letters(self):
        # "sky": s, k (rare), y (rare)
        # base=10, length_bonus=0, unique=3→9
        # rare_count=2 (k, y) → rare_bonus=50
        # total=69
        assert bot.calculate_hangman_reward("sky") == 69

    def test_longer_word(self):
        # "python": 6 chars, p,y(rare),t,h,o,n → 6 unique
        # base=10, length_bonus=(6-3)*6=18, unique=6→18
        # rare_count=1 (y) → rare_bonus=25
        # total=71
        assert bot.calculate_hangman_reward("python") == 71


class TestBuildHangmanDisplay:
    def _make_game(self, word, guessed, wrong):
        return {"word": word, "guessed_letters": set(guessed), "wrong_guesses": wrong}

    def test_empty_gallows(self):
        game = self._make_game("hello", [], 0)
        result = bot.build_hangman_display(game)
        assert bot.HANGMAN_ART[0] in result

    def test_full_gallows(self):
        game = self._make_game("hello", [], 6)
        result = bot.build_hangman_display(game)
        assert bot.HANGMAN_ART[6] in result

    def test_partial_reveal(self):
        game = self._make_game("hello", ["h", "e"], 2)
        result = bot.build_hangman_display(game)
        assert "h e _ _ _" in result

    def test_no_guesses_all_blanks(self):
        game = self._make_game("hi", [], 0)
        result = bot.build_hangman_display(game)
        assert "_ _" in result

    def test_lives_remaining(self):
        game = self._make_game("test", [], 3)
        result = bot.build_hangman_display(game)
        assert "Lives left: 3" in result

    def test_guessed_letters_sorted(self):
        game = self._make_game("test", ["z", "a"], 1)
        result = bot.build_hangman_display(game)
        assert "a, z" in result


# ─────────────────────────────────────────────────────────────────────────────
# Slots
# ─────────────────────────────────────────────────────────────────────────────

class TestEvalSlots:
    def test_jackpot_sufficient_bet(self):
        assert bot.eval_slots(["7️⃣", "7️⃣", "7️⃣"], 25) == ("jackpot", 75)

    def test_jackpot_insufficient_bet(self):
        assert bot.eval_slots(["7️⃣", "7️⃣", "7️⃣"], 10) == ("nothing", 0)

    def test_three_bar(self):
        assert bot.eval_slots(["🎰", "🎰", "🎰"], 25) == ("3bar", 15)

    def test_three_bell(self):
        assert bot.eval_slots(["🔔", "🔔", "🔔"], 25) == ("3bell", 7)

    def test_three_lemon(self):
        assert bot.eval_slots(["🍋", "🍋", "🍋"], 25) == ("3lemon", 4)

    def test_three_cherry(self):
        assert bot.eval_slots(["🍒", "🍒", "🍒"], 25) == ("3cherry", 3)

    def test_two_cherries(self):
        assert bot.eval_slots(["🍒", "🍒", "🍋"], 25) == ("2cherry", 2)

    def test_one_cherry(self):
        assert bot.eval_slots(["🍒", "🍋", "🍋"], 25) == ("1cherry", 1)

    def test_nothing(self):
        assert bot.eval_slots(["🍋", "🔔", "🎰"], 25) == ("nothing", 0)

    def test_cherry_in_middle_counts(self):
        assert bot.eval_slots(["🍋", "🍒", "🎰"], 25) == ("1cherry", 1)


# ─────────────────────────────────────────────────────────────────────────────
# Tic-tac-toe
# ─────────────────────────────────────────────────────────────────────────────

def _empty_ttt_board():
    return [None] * 9


class TestCheckTttWinner:
    def test_no_winner_empty(self):
        assert bot.check_ttt_winner(_empty_ttt_board()) is None

    def test_row_win(self):
        board = ["❌", "❌", "❌", None, None, None, None, None, None]
        assert bot.check_ttt_winner(board) == "❌"

    def test_col_win(self):
        board = ["⭕", None, None, "⭕", None, None, "⭕", None, None]
        assert bot.check_ttt_winner(board) == "⭕"

    def test_diagonal_win(self):
        board = ["❌", None, None, None, "❌", None, None, None, "❌"]
        assert bot.check_ttt_winner(board) == "❌"

    def test_anti_diagonal_win(self):
        board = [None, None, "⭕", None, "⭕", None, "⭕", None, None]
        assert bot.check_ttt_winner(board) == "⭕"

    def test_partial_no_winner(self):
        board = ["❌", "⭕", "❌", "⭕", "❌", "⭕", "⭕", "❌", "⭕"]
        # Draw — no three in a row for either
        assert bot.check_ttt_winner(board) is None


class TestIsTttStalemate:
    def test_empty_board_not_stalemate(self):
        assert not bot.is_ttt_stalemate(_empty_ttt_board())

    def test_one_move_not_stalemate(self):
        board = ["❌", None, None, None, None, None, None, None, None]
        assert not bot.is_ttt_stalemate(board)

    def test_full_draw_board_is_stalemate(self):
        # Classic draw: no three in a row for either
        board = ["❌", "⭕", "❌", "⭕", "❌", "⭕", "⭕", "❌", "⭕"]
        assert bot.is_ttt_stalemate(board)

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
        assert bot.is_ttt_stalemate(board)

    def test_open_winning_line_not_stalemate(self):
        # ❌ can still win on bottom row (indices 6,7,8)
        board = ["❌", "⭕", "❌",
                 "⭕", "⭕", "❌",
                 None, None, None]
        assert not bot.is_ttt_stalemate(board)

    def test_winner_on_board_not_reported_as_stalemate(self):
        # check_ttt_winner is called first in the game loop, but stalemate
        # should also return False when a winning line exists
        board = ["❌", "❌", "❌", "⭕", "⭕", None, None, None, None]
        assert not bot.is_ttt_stalemate(board)


class TestBuildTttDisplay:
    def test_empty_board_shows_numbers(self):
        game = {"board": _empty_ttt_board()}
        result = bot.build_ttt_display(game)
        assert "1️⃣" in result
        assert "9️⃣" in result

    def test_marks_shown_in_correct_position(self):
        board = ["❌"] + [None] * 8
        game = {"board": board}
        result = bot.build_ttt_display(game)
        assert result.startswith("❌")


# ─────────────────────────────────────────────────────────────────────────────
# Lottery
# ─────────────────────────────────────────────────────────────────────────────

import datetime
from zoneinfo import ZoneInfo

_CT = ZoneInfo("America/Chicago")


def _ct(year, month, day, hour, minute=0):
    """Return a timezone-aware datetime in America/Chicago."""
    return datetime.datetime(year, month, day, hour, minute, tzinfo=_CT)


def _should_draw(lottery: dict, now_ct: datetime.datetime) -> bool:
    """Mirror the gate logic in lottery_scheduler / on_ready."""
    from src.economy import lottery_week_key
    is_saturday = now_ct.weekday() == 5
    is_6pm = now_ct.hour == 18 and now_ct.minute == 0
    current_week = lottery_week_key(now_ct)
    return is_saturday and is_6pm and current_week != lottery.get("last_posted_week", 0)


GUILD_ID = 111222333


class TestDrainBotBalanceIntoLottery:
    @pytest.mark.asyncio
    async def test_transfers_house_balance_to_pool(self):
        bot.economy["guild_house"] = {str(GUILD_ID): 500}
        lottery = {"prize_pool": 100, "players": {}}
        transferred = await bot.drain_bot_balance_into_lottery(lottery, GUILD_ID)
        assert transferred == 500
        assert lottery["prize_pool"] == 600
        assert bot.economy["guild_house"][str(GUILD_ID)] == 0

    @pytest.mark.asyncio
    async def test_noop_when_house_balance_zero(self):
        bot.economy["guild_house"] = {str(GUILD_ID): 0}
        lottery = {"prize_pool": 200, "players": {}}
        transferred = await bot.drain_bot_balance_into_lottery(lottery, GUILD_ID)
        assert transferred == 0
        assert lottery["prize_pool"] == 200

    @pytest.mark.asyncio
    async def test_noop_when_no_guild_entry(self):
        bot.economy["guild_house"] = {}
        lottery = {"prize_pool": 50, "players": {}}
        transferred = await bot.drain_bot_balance_into_lottery(lottery, GUILD_ID)
        assert transferred == 0
        assert lottery["prize_pool"] == 50

    @pytest.mark.asyncio
    async def test_large_house_balance(self):
        bot.economy["guild_house"] = {str(GUILD_ID): 99999}
        lottery = {"prize_pool": 2000, "players": {}}
        await bot.drain_bot_balance_into_lottery(lottery, GUILD_ID)
        assert lottery["prize_pool"] == 101999


class TestLotteryTicketPurchase:
    """Test the dict-mutation logic that cmd_lottery performs (no Discord needed)."""

    async def _buy(self, lottery: dict, uid: int, tickets: int):
        """Replicate the purchase logic from cmd_lottery lines 6184-6197."""
        cost = tickets * 10
        await bot.deduct_balance(uid, cost)
        players = lottery.setdefault("players", {})
        was_new = str(uid) not in players
        players[str(uid)] = players.get(str(uid), 0) + tickets
        lottery.setdefault("prize_pool", 0)
        lottery["prize_pool"] += cost
        if was_new:
            lottery["prize_pool"] += 1000
        return was_new

    @pytest.mark.asyncio
    async def test_first_buyer_gets_bonus(self):
        uid = 1001
        await bot.add_balance(uid, 500)
        lottery = {"prize_pool": 2000, "players": {}}
        was_new = await self._buy(lottery, uid, 5)
        assert was_new is True
        assert lottery["prize_pool"] == 2000 + 50 + 1000
        assert lottery["players"][str(uid)] == 5

    @pytest.mark.asyncio
    async def test_returning_buyer_no_bonus(self):
        uid = 1002
        await bot.add_balance(uid, 500)
        lottery = {"prize_pool": 2000, "players": {str(uid): 3}}
        was_new = await self._buy(lottery, uid, 2)
        assert was_new is False
        assert lottery["prize_pool"] == 2000 + 20  # no bonus
        assert lottery["players"][str(uid)] == 5

    @pytest.mark.asyncio
    async def test_each_new_player_adds_bonus(self):
        lottery = {"prize_pool": 2000, "players": {}}
        for uid in [2001, 2002, 2003]:
            await bot.add_balance(uid, 200)
            await self._buy(lottery, uid, 1)
        # 3 new players × (10 ticket cost + 1000 bonus) = 3030 added
        assert lottery["prize_pool"] == 2000 + 3 * 10 + 3 * 1000

    @pytest.mark.asyncio
    async def test_ticket_count_accumulates(self):
        uid = 3001
        await bot.add_balance(uid, 1000)
        lottery = {"prize_pool": 0, "players": {}}
        await self._buy(lottery, uid, 4)
        await self._buy(lottery, uid, 6)
        assert lottery["players"][str(uid)] == 10

    @pytest.mark.asyncio
    async def test_balance_deducted(self):
        uid = 4001
        await bot.add_balance(uid, 300)
        lottery = {"prize_pool": 0, "players": {}}
        await self._buy(lottery, uid, 10)  # costs 100
        assert await bot.get_balance(uid) == 200

    @pytest.mark.asyncio
    async def test_ticket_cost_is_ten_per_ticket(self):
        uid = 5001
        await bot.add_balance(uid, 1000)
        lottery = {"prize_pool": 0, "players": {}}
        await self._buy(lottery, uid, 7)
        # pool gets 70 (cost) + 1000 (new player bonus)
        assert lottery["prize_pool"] == 1070


class TestLotteryWeekTiming:
    """Test the draw-trigger gate logic using _should_draw."""

    def _lottery(self, last_posted_week=0):
        return {"prize_pool": 5000, "players": {"9": 3}, "last_posted_week": last_posted_week}

    def test_saturday_6pm_cdt_triggers(self):
        # June 7 2025 is a Saturday; Chicago is in CDT (UTC-5)
        now = _ct(2025, 6, 7, 18, 0)
        assert _should_draw(self._lottery(), now) is True

    def test_saturday_6pm_cst_triggers(self):
        # Jan 4 2025 is a Saturday; Chicago is in CST (UTC-6)
        now = _ct(2025, 1, 4, 18, 0)
        assert _should_draw(self._lottery(), now) is True

    def test_saturday_before_6pm_does_not_trigger(self):
        now = _ct(2025, 6, 7, 17, 59)
        assert _should_draw(self._lottery(), now) is False

    def test_saturday_after_6pm_does_not_trigger(self):
        now = _ct(2025, 6, 7, 18, 1)
        assert _should_draw(self._lottery(), now) is False

    def test_non_saturday_does_not_trigger(self):
        # June 6 2025 is a Friday
        now = _ct(2025, 6, 6, 18, 0)
        assert _should_draw(self._lottery(), now) is False

    def test_already_posted_this_week_does_not_retrigger(self):
        from src.economy import lottery_week_key
        now = _ct(2025, 6, 7, 18, 0)
        week = lottery_week_key(now)
        assert _should_draw(self._lottery(last_posted_week=week), now) is False

    def test_different_week_triggers_again(self):
        # Last week's encoded key differs from this week's → should draw
        from src.economy import lottery_week_key
        now = _ct(2025, 6, 7, 18, 0)               # ISO week 23 of 2025
        last_week_key = lottery_week_key(_ct(2025, 5, 31, 18, 0))  # week 22
        assert _should_draw(self._lottery(last_posted_week=last_week_key), now) is True

    def test_year_boundary_does_not_silently_skip(self):
        """A year-old week 1 must not collide with the new year's week 1.
        Previously last_drawn_week was a bare int 1..52 with no year, so a
        year of uptime could suppress the draw. Encoding as YYYYWW fixes it.
        """
        from src.economy import lottery_week_key
        # Saturday in ISO week 1 of 2027 is Jan 9 (Jan 2 is week 53 of 2026).
        now = _ct(2027, 1, 9, 18, 0)
        last_year_key = lottery_week_key(_ct(2026, 1, 3, 18, 0))  # week 1 of 2026
        assert _should_draw(self._lottery(last_posted_week=last_year_key), now) is True


class TestLotteryWinnerPayout:
    @pytest.mark.asyncio
    async def test_sole_player_always_wins(self):
        uid = 7001
        await bot.add_balance(uid, 0)
        pool = 5000
        players = {str(uid): 10}
        # Replicate scheduler payout: pick winner, add_balance
        winner_id = int(list(players.keys())[0])
        await bot.add_balance(winner_id, pool)
        assert await bot.get_balance(uid) == pool

    @pytest.mark.asyncio
    async def test_winner_receives_full_pool(self):
        uid = 7002
        await bot.add_balance(uid, 100)
        pool = 8000
        await bot.add_balance(uid, pool)
        assert await bot.get_balance(uid) == 8100

    @pytest.mark.asyncio
    async def test_random_choice_winner(self, monkeypatch):
        players = {"8001": 5, "8002": 3, "8003": 1}
        for uid_str in players:
            await bot.add_balance(int(uid_str), 0)
        pool = 3000
        # Force random.choice to always pick player 8002
        monkeypatch.setattr("src.random.choice", lambda seq: "8002")
        winner_id = int(bot.random.choice(list(players.keys())))
        await bot.add_balance(winner_id, pool)
        assert await bot.get_balance(8002) == pool
        assert await bot.get_balance(8001) == 0
        assert await bot.get_balance(8003) == 0


@pytest.mark.asyncio
class TestAnnounceNewLotteryTimestamp:
    async def _send_and_capture(self, prize_pool, now_ct):
        """Call announce_new_lottery with a mock channel, return the sent embed."""
        sent = []

        class FakeChannel:
            async def send(self, embed=None, **kwargs):
                sent.append(embed)

        now_utc = now_ct.astimezone(datetime.timezone.utc)
        await bot.announce_new_lottery(FakeChannel(), prize_pool=prize_pool, now=now_utc)
        return sent[0]

    async def test_embed_title(self):
        embed = await self._send_and_capture(5000, _ct(2025, 6, 7, 18))
        assert embed.title == "🎰 New Lottery Week"

    async def test_prize_pool_in_description(self):
        embed = await self._send_and_capture(12345, _ct(2025, 6, 7, 18))
        assert "12,345" in embed.description

    async def test_timestamp_points_to_next_saturday_cdt(self):
        # Called on a Tuesday in June (CDT); next Saturday is Jun 14
        now = _ct(2025, 6, 10, 12)  # Tuesday
        embed = await self._send_and_capture(2000, now)
        # Extract Unix timestamp from <t:XXXXXX:R>
        import re
        match = re.search(r"<t:(\d+):R>", embed.description)
        assert match, "No Discord timestamp found in description"
        ts = int(match.group(1))
        dt = datetime.datetime.fromtimestamp(ts, tz=_CT)
        assert dt.weekday() == 5       # Saturday
        assert dt.hour == 18
        assert dt.minute == 0
        assert dt.year == 2025
        assert dt.month == 6
        assert dt.day == 14

    async def test_timestamp_points_to_next_saturday_cst(self):
        # Called on a Tuesday in January (CST); next Saturday is Jan 11
        now = _ct(2025, 1, 7, 12)  # Tuesday
        embed = await self._send_and_capture(2000, now)
        import re
        match = re.search(r"<t:(\d+):R>", embed.description)
        assert match
        ts = int(match.group(1))
        dt = datetime.datetime.fromtimestamp(ts, tz=_CT)
        assert dt.weekday() == 5
        assert dt.hour == 18
        assert dt.month == 1
        assert dt.day == 11

    async def test_called_on_saturday_points_to_following_saturday(self):
        # If called exactly on Saturday, next draw is 7 days later
        now = _ct(2025, 6, 7, 18)  # Saturday
        embed = await self._send_and_capture(2000, now)
        import re
        match = re.search(r"<t:(\d+):R>", embed.description)
        assert match
        ts = int(match.group(1))
        dt = datetime.datetime.fromtimestamp(ts, tz=_CT)
        assert dt.weekday() == 5
        assert dt.day == 14  # one week later


# ─────────────────────────────────────────────────────────────────────────────
# Connect 4
# ─────────────────────────────────────────────────────────────────────────────

def _empty_c4_board():
    return [[None] * 7 for _ in range(6)]


class TestCheckC4Winner:
    def test_no_winner_empty(self):
        assert bot.check_c4_winner(_empty_c4_board()) is None

    def test_horizontal_win(self):
        board = _empty_c4_board()
        for c in range(4):
            board[5][c] = "🔴"
        assert bot.check_c4_winner(board) == "🔴"

    def test_vertical_win(self):
        board = _empty_c4_board()
        for r in range(4):
            board[r][0] = "🟡"
        assert bot.check_c4_winner(board) == "🟡"

    def test_diagonal_ascending_win(self):
        board = _empty_c4_board()
        for i in range(4):
            board[i][i] = "🔴"
        assert bot.check_c4_winner(board) == "🔴"

    def test_diagonal_descending_win(self):
        # (↖) diagonal: rows 3,2,1,0; cols 0,1,2,3
        board = _empty_c4_board()
        board[3][0] = "🔴"
        board[2][1] = "🔴"
        board[1][2] = "🔴"
        board[0][3] = "🔴"
        assert bot.check_c4_winner(board) == "🔴"


class TestBuildC4Display:
    def test_empty_board_shows_column_headers(self):
        game = {"board": _empty_c4_board()}
        result = bot.build_c4_display(game)
        assert "1️⃣" in result
        assert "7️⃣" in result

    def test_empty_cells_shown_as_black_circle(self):
        game = {"board": _empty_c4_board()}
        result = bot.build_c4_display(game)
        assert "⚫" in result


# ─────────────────────────────────────────────────────────────────────────────
# Chess helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestCreateChessBoard:
    def test_board_dimensions(self):
        board = bot.create_chess_board()
        assert len(board) == 8
        assert all(len(row) == 8 for row in board)

    def test_white_pieces_on_rank_1(self):
        board = bot.create_chess_board()
        assert board[7][0] == "♖"  # white rook
        assert board[7][4] == "♔"  # white king
        assert board[7][7] == "♖"  # white rook

    def test_black_pieces_on_rank_8(self):
        board = bot.create_chess_board()
        assert board[0][4] == "♚"  # black king
        assert board[0][0] == "♜"  # black rook

    def test_white_pawns_on_rank_2(self):
        board = bot.create_chess_board()
        assert all(board[6][c] == "♙" for c in range(8))

    def test_black_pawns_on_rank_7(self):
        board = bot.create_chess_board()
        assert all(board[1][c] == "♟" for c in range(8))


class TestParseChessMove:
    def test_valid_move_e2e4(self):
        assert bot.parse_chess_move("e2e4") == (6, 4, 4, 4)

    def test_valid_move_a1a8(self):
        assert bot.parse_chess_move("a1a8") == (7, 0, 0, 0)

    def test_case_insensitive(self):
        assert bot.parse_chess_move("E2E4") == (6, 4, 4, 4)

    def test_space_separated(self):
        assert bot.parse_chess_move("e2 e4") == (6, 4, 4, 4)

    def test_too_short(self):
        assert bot.parse_chess_move("e2") is None

    def test_out_of_bounds_rank(self):
        assert bot.parse_chess_move("a9a1") is None

    def test_garbage(self):
        assert bot.parse_chess_move("invalid") is None


class TestPieceIdentification:
    def test_white_pieces(self):
        for p in "♔♕♖♗♘♙":
            assert bot.is_white_piece(p) is True

    def test_black_pieces_not_white(self):
        for p in "♚♛♜♝♞♟":
            assert bot.is_white_piece(p) is False

    def test_none_not_white(self):
        assert bot.is_white_piece(None) is False

    def test_black_pieces(self):
        for p in "♚♛♜♝♞♟":
            assert bot.is_black_piece(p) is True

    def test_white_pieces_not_black(self):
        for p in "♔♕♖♗♘♙":
            assert bot.is_black_piece(p) is False

    def test_none_not_black(self):
        assert bot.is_black_piece(None) is False


class TestIsValidChessMove:
    def setup_method(self):
        self.board = bot.create_chess_board()

    def test_white_pawn_forward(self):
        assert bot.is_valid_chess_move(self.board, 6, 0, 5, 0, True) is True

    def test_white_cannot_move_black_piece(self):
        # board[0][0] is black rook
        assert bot.is_valid_chess_move(self.board, 0, 0, 1, 0, True) is False

    def test_white_cannot_capture_own_piece(self):
        # board[6][0] and board[6][1] are both white pawns
        assert bot.is_valid_chess_move(self.board, 6, 0, 6, 1, True) is False

    def test_out_of_bounds_target(self):
        assert bot.is_valid_chess_move(self.board, 6, 0, 8, 0, True) is False

    def test_empty_square_cannot_move(self):
        assert bot.is_valid_chess_move(self.board, 4, 4, 3, 4, True) is False

    def test_black_can_move_black_piece(self):
        # board[1][0] is black pawn
        assert bot.is_valid_chess_move(self.board, 1, 0, 2, 0, False) is True


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
        result = bot._render_race(game)
        # Position 0: horse at start, full track of empty squares
        assert "🏇" in result
        assert "░" * bot.RACE_TRACK_LEN in result
        assert "Alice" in result

    def test_player_advanced(self):
        game = {
            "players": [1],
            "positions": {1: 5},
            "names": {1: "Bob"},
        }
        result = bot._render_race(game)
        assert "▓" * 5 in result
        assert "░" * (bot.RACE_TRACK_LEN - 5) in result

    def test_multiple_players(self):
        game = {
            "players": [1, 2],
            "positions": {1: 0, 2: 10},
            "names": {1: "Alice", 2: "Bob"},
        }
        result = bot._render_race(game)
        lines = result.split("\n")
        assert len(lines) == 2
        assert "Alice" in lines[0]
        assert "Bob" in lines[1]


# ─────────────────────────────────────────────────────────────────────────────
# MiniCactpot
# ─────────────────────────────────────────────────────────────────────────────

class TestMiniCactpot:
    def setup_method(self):
        self.game = bot.MiniCactpotGame(1)
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
        await bot._ensure_user(1)
        assert "1" in bot.economy["users"]
        assert bot.economy["users"]["1"]["balance"] == 0

    @pytest.mark.asyncio
    async def test_ensure_user_idempotent(self):
        await bot._ensure_user(1)
        await bot._ensure_user(1)
        assert bot.economy["users"]["1"]["balance"] == 0

    @pytest.mark.asyncio
    async def test_get_balance_new_user(self):
        assert await bot.get_balance(1) == 0

    @pytest.mark.asyncio
    async def test_add_balance(self):
        await bot.add_balance(1, 100)
        assert await bot.get_balance(1) == 100

    @pytest.mark.asyncio
    async def test_add_balance_accumulates(self):
        await bot.add_balance(1, 50)
        await bot.add_balance(1, 75)
        assert await bot.get_balance(1) == 125

    @pytest.mark.asyncio
    async def test_deduct_balance_sufficient_funds(self):
        await bot.add_balance(1, 100)
        result = await bot.deduct_balance(1, 60)
        assert result is True
        assert await bot.get_balance(1) == 40

    @pytest.mark.asyncio
    async def test_deduct_balance_insufficient_funds(self):
        await bot.add_balance(1, 10)
        result = await bot.deduct_balance(1, 50)
        assert result is False
        assert await bot.get_balance(1) == 10  # unchanged

    @pytest.mark.asyncio
    async def test_deduct_exact_balance(self):
        await bot.add_balance(1, 50)
        result = await bot.deduct_balance(1, 50)
        assert result is True
        assert await bot.get_balance(1) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Guild settings helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestGuildSettings:
    def test_get_guild_cfg_creates_entry(self):
        cfg = bot.get_guild_cfg(42)
        assert cfg == {}
        assert "42" in bot.guild_settings

    def test_get_guild_cfg_idempotent(self):
        bot.get_guild_cfg(42)
        cfg = bot.get_guild_cfg(42)
        assert cfg == {}

    def test_get_guild_ask_model_default(self):
        model = bot.get_guild_ask_model(42)
        assert model == bot.OLLAMA_MODEL

    def test_get_guild_ask_model_custom(self, monkeypatch):
        monkeypatch.setattr(bot, "guild_settings", {"42": {"ask_model": "llama3:8b"}})
        assert bot.get_guild_ask_model(42) == "llama3:8b"

    def test_get_guild_roleplay_model_default(self):
        assert bot.get_guild_roleplay_model(42) == bot.OLLAMA_MODEL

    def test_get_guild_roleplay_model_custom(self, monkeypatch):
        monkeypatch.setattr(bot, "guild_settings", {"42": {"roleplay_model": "mistral:7b"}})
        assert bot.get_guild_roleplay_model(42) == "mistral:7b"


# ─────────────────────────────────────────────────────────────────────────────
# Insurance
# ─────────────────────────────────────────────────────────────────────────────

class TestInsurance:
    @pytest.mark.asyncio
    async def test_not_insured_missing_user(self):
        assert await bot.is_insured(1, "rob") is False

    @pytest.mark.asyncio
    async def test_not_insured_expired(self, monkeypatch):
        monkeypatch.setattr(bot, "insurance", {
            "1": {"expires_at": time.time() - 10, "protected_from": ["rob"]}
        })
        assert await bot.is_insured(1, "rob") is False

    @pytest.mark.asyncio
    async def test_insured_valid(self, monkeypatch):
        monkeypatch.setattr(bot, "insurance", {
            "1": {"expires_at": time.time() + 3600, "protected_from": ["rob"]}
        })
        assert await bot.is_insured(1, "rob") is True

    @pytest.mark.asyncio
    async def test_insured_wrong_protection_type(self, monkeypatch):
        monkeypatch.setattr(bot, "insurance", {
            "1": {"expires_at": time.time() + 3600, "protected_from": ["mug"]}
        })
        assert await bot.is_insured(1, "rob") is False


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckRateLimit:
    def test_first_call_not_limited(self):
        # user_last_request is reset to {} by conftest autouse fixture
        assert bot.check_rate_limit(999) is False

    def test_second_call_immediately_limited(self):
        bot.check_rate_limit(999)
        assert bot.check_rate_limit(999) is True

    def test_different_users_independent(self):
        bot.check_rate_limit(1)
        assert bot.check_rate_limit(2) is False  # user 2 not rate-limited


# ─────────────────────────────────────────────────────────────────────────────
# Puzzle helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestPuzzleRewards:
    def test_easy(self):
        assert bot.PUZZLE_REWARDS["easy"] == 10

    def test_medium(self):
        assert bot.PUZZLE_REWARDS["medium"] == 20

    def test_hard(self):
        assert bot.PUZZLE_REWARDS["hard"] == 35

    def test_extreme(self):
        assert bot.PUZZLE_REWARDS["extreme"] == 50

    def test_all_difficulties_present(self):
        assert set(bot.PUZZLE_REWARDS.keys()) == {"easy", "medium", "hard", "extreme"}


class TestNormPuzzleAnswer:
    def test_lowercase(self):
        assert bot._norm_puzzle_answer("Hello") == "hello"

    def test_strips_leading_trailing_whitespace(self):
        assert bot._norm_puzzle_answer("  42  ") == "42"

    def test_collapses_internal_whitespace(self):
        assert bot._norm_puzzle_answer("foo  bar\tbaz") == "foo bar baz"

    def test_exact_match(self):
        assert bot._norm_puzzle_answer("True") == bot._norm_puzzle_answer("true")

    def test_whitespace_only(self):
        assert bot._norm_puzzle_answer("   ") == ""

    def test_empty_string(self):
        assert bot._norm_puzzle_answer("") == ""

    def test_multiline_output(self):
        # newlines count as whitespace and get collapsed
        assert bot._norm_puzzle_answer("1\n2\n3") == "1 2 3"


class TestGetGuildCodingModel:
    def test_default_falls_back_to_ollama_model(self):
        assert bot.get_guild_coding_model(42) == bot.OLLAMA_MODEL

    def test_custom_model_returned(self, monkeypatch):
        monkeypatch.setattr(bot, "guild_settings", {"42": {"coding_model": "deepseek-coder:6.7b"}})
        assert bot.get_guild_coding_model(42) == "deepseek-coder:6.7b"

    def test_missing_key_falls_back(self, monkeypatch):
        # Guild exists but coding_model not set
        monkeypatch.setattr(bot, "guild_settings", {"42": {"ask_model": "llama3:8b"}})
        assert bot.get_guild_coding_model(42) == bot.OLLAMA_MODEL

    def test_independent_from_ask_model(self, monkeypatch):
        monkeypatch.setattr(bot, "guild_settings", {
            "42": {"ask_model": "llama3:8b", "coding_model": "deepseek-coder:6.7b"}
        })
        assert bot.get_guild_coding_model(42) == "deepseek-coder:6.7b"
        assert bot.get_guild_ask_model(42) == "llama3:8b"

    def test_different_guilds_independent(self, monkeypatch):
        monkeypatch.setattr(bot, "guild_settings", {
            "1": {"coding_model": "model-a"},
            "2": {"coding_model": "model-b"},
        })
        assert bot.get_guild_coding_model(1) == "model-a"
        assert bot.get_guild_coding_model(2) == "model-b"


class TestSaveQuoteLog:
    @pytest.mark.asyncio
    async def test_trims_to_last_10(self, monkeypatch):
        import src.state as _state
        log = [str(i) for i in range(15)]
        await bot.save_quote_log(log)
        assert _state.quote_log == [str(i) for i in range(5, 15)]

    @pytest.mark.asyncio
    async def test_short_log_unchanged(self, monkeypatch):
        import src.state as _state
        log = ["a", "b", "c"]
        await bot.save_quote_log(log)
        assert _state.quote_log == ["a", "b", "c"]
