# Re-exports for test compatibility.
# Tests do: import src as bot (or import bot when bot.py is removed)
# and access attributes like bot.hand_value, bot.economy, etc.
import random  # re-exported for monkeypatching in tests

from src.config import OLLAMA_MODEL, RACE_TRACK_LEN

from src.persistence import (
    get_guild_cfg,
    save_quote_log,
)

from src.state import (
    economy, guild_settings, insurance, user_last_request,
    slot_jackpot, bot_roles, bot_admins, godmode_users, bot_settings,
    active_blackjack_games, active_hangman_games,
    active_race_games, active_chess_games,
    active_puzzles, active_ragebaits, active_ttt_games, active_c4_games,
    rigged_slots, gambler_streak, quote_log, channel_histories,
    ai_threads, active_mocks, active_taxes,
    active_curses,
)

from src.economy import (
    _ensure_user, get_balance, add_balance, deduct_balance,
    get_guild_house_balance, add_guild_house, drain_bot_balance_into_lottery,
    announce_new_lottery, is_insured,
    get_guild_ask_model, get_guild_roleplay_model, get_guild_coding_model,
    _ct_now, _ct_today, do_daily_reset,
)

from src.helpers import (
    mocking_font, curse_font, emb,
    C_GREEN, C_RED, C_GOLD, C_ORANGE, C_BLUE, C_PURPLE, C_GREY,
    _render_race,
)

from src.permissions import check_rate_limit

from src.ai import (
    FEATURE_COSTS, _norm_puzzle_answer,
    enforce_cost, insufficient_funds, check_ollama_connected,
)

from src.games.blackjack import (
    SUITS, RANKS, new_deck, draw_card, hand_value, format_hand,
    build_blackjack_display,
)

from src.games.hangman import (
    HANGMAN_ART, build_hangman_display, calculate_hangman_reward,
)

from src.games.ttt_c4 import (
    build_ttt_display, check_ttt_winner, is_ttt_stalemate,
    build_c4_display, check_c4_winner,
)

from src.games.chess import (
    create_chess_board, build_chess_display, parse_chess_move,
    is_white_piece, is_black_piece, is_valid_chess_move,
)

from src.gambling.slots import eval_slots

from src.gambling.scratchoff import MiniCactpotGame

from src.config import PUZZLE_REWARDS
