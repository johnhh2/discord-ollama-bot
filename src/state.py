import time
import csv
from collections import defaultdict, deque

from src.config import HISTORY_LIMIT, RIDDLES_FILE, SLOT_JACKPOT_SEED

# ── Persistent state (populated from DB in on_ready via init_db_state) ────────

channel_prompts: dict = {}
economy: dict = {"users": {}, "last_daily_reset": None, "guild_house": {}}
slot_jackpot: int = SLOT_JACKPOT_SEED
bot_roles: set = set()
bot_admins: set = set()
godmode_users: set = set()
bot_settings: dict = {"vram_text": "16GB"}
guild_settings: dict = {}
insurance: dict = {}
locked_channels: dict = {}
locked_roles: dict = {}
active_ragebaits: dict = {}
active_mocks: dict = {}
active_taxes: dict = {}
active_curses: dict = {}
active_chess_games: dict = {}
rigged_slots: dict = {}
rigged_flips: dict = {}
rigged_scratch: dict = {}
rigged_steal: dict = {}
gambler_streak: dict = {}
quote_log: list = []
leveling: dict = {}
command_perms: dict = {}

# ── In-memory game state ──────────────────────────────────────────────────────

active_blackjack_games: dict = {}
active_hangman_games: dict = {}
active_events: dict = {}        # message_id → {amount, rewarded: set}
active_ttt_games: dict = {}
active_c4_games: dict = {}
active_race_games: dict = {}
active_puzzles: dict = {}       # channel_id → {question, answer, reward, user_id}


def _load_riddles_list() -> list:
    try:
        with open(RIDDLES_FILE, newline="", encoding="utf-8") as f:
            return [
                {"riddle": r["QUESTIONS"].strip(), "answer": r["ANSWERS"].strip().lower()}
                for r in csv.DictReader(f)
                if r["QUESTIONS"].strip() and r["ANSWERS"].strip()
            ]
    except Exception:
        return []


RIDDLES_LIST: list = _load_riddles_list()

# ── Misc state ────────────────────────────────────────────────────────────────

channel_histories: dict = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
# AI thread sessions (ask, story, roleplay, rpg) — keyed by thread_id.
# Value: {kind, owner_id, invited_ids: set, system_prompt: str|None,
#         character_prompt: str|None, history: list, guild_id: int|None}
ai_threads: dict = {}
# Cross-cog rate-limit tracking. user_last_hangman / user_last_puzzle /
# crime_active_users / _soundboard_timestamps used to live here too;
# they were each touched by exactly one cog or module and have been
# moved to that owner (HangmanCog._last_hangman_by_uid,
# UtilityCog._last_puzzle_by_uid, EconomyCog._crime_active,
# events._SOUNDBOARD_TIMESTAMPS).
user_last_request: dict = {}

# ── Stats ─────────────────────────────────────────────────────────────────────

bot_start_time = time.monotonic()
stats_commands_ran: int = 0
stats_messages_seen: int = 0

stats_messages_today: int = 0
stats_commands_today: int = 0
stats_ai_responses_today: int = 0
stats_commands_today_by_cog: dict = {}

# ── Audit log ─────────────────────────────────────────────────────────────────

audit_log: deque = deque(maxlen=20)
