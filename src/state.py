import time
import csv
from collections import defaultdict, deque

from src.config import HISTORY_LIMIT, RIDDLES_FILE
from src.persistence import (
    load_channel_prompts, load_economy, load_jackpot, load_bot_roles,
    load_bot_admins, load_godmode_users, load_bot_settings, load_guild_settings,
    load_insurance, load_ragebait, load_mock, load_rigged_slots, load_rigged_flips,
    load_rigged_scratch, load_rigged_steal, load_gambler_streak, load_quote_log, load_chess_games, load_simp, load_curse,
    load_leveling, load_command_perms,
)

# ── Persistent state (loaded from disk on startup) ────────────────────────────

channel_prompts: dict = load_channel_prompts()
economy: dict = load_economy()
slot_jackpot: int = load_jackpot()
bot_roles: set = load_bot_roles()
bot_admins: set = load_bot_admins()
godmode_users: set = load_godmode_users()
bot_settings: dict = load_bot_settings()
guild_settings: dict = load_guild_settings()
insurance: dict = load_insurance()

# Populate lock state from guild_settings
def _load_locks():
    channels = {}
    roles = {}
    for cfg in guild_settings.values():
        for k, v in cfg.get("locked_channels", {}).items():
            channels[int(k)] = int(v)
        for k, v in cfg.get("locked_roles", {}).items():
            roles[int(k)] = int(v)
    return channels, roles

locked_channels: dict
locked_roles: dict
locked_channels, locked_roles = _load_locks()

# ── In-memory game state ──────────────────────────────────────────────────────

active_blackjack_games: dict = {}
active_hangman_games: dict = {}
active_roleplays: dict = {}
roleplay_histories: dict = {}
active_events: dict = {}        # message_id → {amount, rewarded: set}
active_ragebaits: dict = load_ragebait()   # user_id → {remaining: int, history: list[str]}
active_ttt_games: dict = {}     # channel_id → {board, players, marks, current}
active_c4_games: dict = {}      # channel_id → {board, players, marks, current}
active_race_games: dict = {}    # channel_id → {players, names, positions, amount}
active_chess_games: dict = load_chess_games()  # channel_id → {board, players, current, moves, amount}
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
rigged_slots: dict = load_rigged_slots()  # user_id (int) → symbol (str)
rigged_flips: dict = load_rigged_flips()   # user_id → remaining rigged wins
rigged_scratch: dict = load_rigged_scratch()  # user_id → number of symbols to rig (1-4) on 3rd scratch
rigged_steal: dict = load_rigged_steal()   # user_id → remaining rigged steal successes
gambler_streak: dict = load_gambler_streak()
quote_log: list = load_quote_log()

# ── Misc state ────────────────────────────────────────────────────────────────

channel_histories: dict = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
fanfic_thread_ids: set = set()
fanfic_owners: dict = {}        # thread_id → {owner_id, invited_ids: set}
user_last_request: dict = {}
user_last_hangman: dict = {}   # user_id → epoch time of last hangman start
user_last_puzzle: dict = {}    # user_id → epoch time of last puzzle start

# ── Stats ─────────────────────────────────────────────────────────────────────

bot_start_time = time.monotonic()
stats_commands_ran: int = 0
stats_messages_seen: int = 0

# ── Audit log ─────────────────────────────────────────────────────────────────

audit_log: deque = deque(maxlen=20)

# ── Shop effect state ─────────────────────────────────────────────────────────

active_mocks: dict = load_mock()            # user_id → {remaining: int, started_by: int}
active_simps: dict = load_simp()            # user_id → simped_by_user_id
active_curses: dict = load_curse()          # user_id → {cursed_by: int, remaining: int}

# ── Leveling ──────────────────────────────────────────────────────────────────
# uid_str → {xp, level, msg_last_hour, msg_today, cmd_last_hour, cmd_today, voice_last_15, voice_today}
leveling: dict = load_leveling()

# ── Crime animation lock ─────────────────────────────────────────────────────
# user_id → True while a steal or mug animation is in progress
crime_active_users: set = set()

# ── Soundboard rate-limit tracking ───────────────────────────────────────────
# (guild_id, user_id) → list of float timestamps (time.monotonic())
_soundboard_timestamps: dict = {}

# ── Command permission overrides ──────────────────────────────────────────────
# command_name → {"tier": "everyone"|"server_admin"|"bot_admin", "hidden": bool}
command_perms: dict = load_command_perms()
