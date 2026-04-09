import os
import sys
import logging
import asyncio
import aiohttp
import discord
import json
import time
import random
import datetime
import re
from zoneinfo import ZoneInfo
import subprocess
from pathlib import Path
from discord.ext import commands, tasks
from discord import ui
from dotenv import load_dotenv
from collections import defaultdict, deque

# Load .env only in dev (not in Docker)
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "dolphin3:8b")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "5.0"))
RULE34_API_KEY = os.getenv("RULE34_API_KEY")
RULE34_USER_ID = os.getenv("RULE34_USER_ID")
RACE_TRACK_LEN = 20

_raw_channels = os.getenv("ACTIVE_CHANNEL_IDS", "")
ACTIVE_CHANNEL_IDS = (
    {int(cid.strip()) for cid in _raw_channels.split(",") if cid.strip()}
    if _raw_channels.strip()
    else set()
)

CHANNEL_PROMPTS_FILE = "data/channel_prompts.json"
ECONOMY_FILE = "data/economy.json"
BOT_ROLES_FILE = "data/bot_roles.json"
BOT_ADMINS_FILE = "data/bot_admins.json"
BOT_SETTINGS_FILE = "data/bot_settings.json"
GUILD_SETTINGS_FILE = "data/guild_settings.json"
INSURANCE_FILE = "data/insurance.json"
MODELS_FILE = "data/models.json"
SLOT_JACKPOT_FILE = "data/slots_jackpot.json"
GODMODE_USERS_FILE = "data/godmode_users.json"
CHESS_GAMES_FILE = "data/chess_games.json"
RAGEBAIT_FILE = "data/ragebait.json"
MOCK_FILE = "data/mock.json"
RIGGED_SLOTS_FILE = "data/rigged_slots.json"
QUOTE_LOG_FILE = "data/quote_log.json"
SAVED_QUOTES_FILE = "data/saved_quotes.json"
SIMP_FILE = "data/simp.json"
CURSE_FILE = "data/curse.json"
LOTTERY_FILE = "data/lottery.json"
GAMBLER_STREAK_FILE = "data/gambler_streak.json"
RESTART_MSG_FILE = "data/restart_msg.json"
EPHEMERAL_MSG_FILE = "data/ephemeral_msgs.json"
FANFIC_HISTORIES_FILE = "data/fanfic_histories.json"
FANFIC_OWNERS_FILE = "data/fanfic_owners.json"
ROLEPLAY_STATE_FILE = "data/roleplay_state.json"

# Slot machine configuration
SLOT_REEL = (
    ["🍒"] * 7 +
    ["🍋"] * 5 +
    ["🔔"] * 4 +
    ["🎰"] * 3 +
    ["7️⃣"] * 1
)
SLOT_JACKPOT_SEED = 5_000
SLOT_JACKPOT_CONTRIB = 0.02
SLOT_HOUSE_CHANCE = 0.05

INITIAL_BOT_ADMIN_ID = 139928946044174336

# Scratchoff lottery configuration
SCRATCHOFF_FILE = "data/scratchoff.json"
SCRATCH_SYMBOLS = ["🍒", "🍋", "🍇", "🍊"]
SCRATCHOFF_MAX_DAILY = 3
SCRATCHOFF_PAYOUTS = {1: 100, 2: 1000, 3: 10000, 4: 100000}

# Soundboard rate-limiting
SOUNDBOARD_WINDOW_SECS = 3.0
SOUNDBOARD_MAX_SOUNDS  = 5


def _load_json(filepath, default):
    os.makedirs("data", exist_ok=True)
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(filepath, data):
    os.makedirs("data", exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)



async def send_ephemeral(ctx: commands.Context, *args, **kwargs) -> discord.Message:
    """Send a message with delete_after=60 and register it for cleanup on restart."""
    kwargs["delete_after"] = 60
    msg = await ctx.send(*args, **kwargs)
    records = _load_json(EPHEMERAL_MSG_FILE, [])
    records.append({"channel_id": msg.channel.id, "message_id": msg.id})
    _save_json(EPHEMERAL_MSG_FILE, records)
    return msg


def load_channel_prompts() -> dict[int, str]:
    if os.path.exists(CHANNEL_PROMPTS_FILE):
        with open(CHANNEL_PROMPTS_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def save_channel_prompts(prompts: dict[int, str]):
    with open(CHANNEL_PROMPTS_FILE, "w") as f:
        json.dump({str(k): v for k, v in prompts.items()}, f, indent=2)


def load_roleplay_state():
    raw = _load_json(ROLEPLAY_STATE_FILE, {"roleplays": {}, "histories": {}})
    roleplays = {}
    for k, v in raw.get("roleplays", {}).items():
        v["participants"] = set(v.get("participants", []))
        roleplays[int(k)] = v
    histories = {int(k): v for k, v in raw.get("histories", {}).items()}
    return roleplays, histories

def save_roleplay_state():
    _save_json(ROLEPLAY_STATE_FILE, {
        "roleplays": {
            str(k): {**v, "participants": list(v.get("participants", set()))}
            for k, v in active_roleplays.items()
        },
        "histories": {str(k): v for k, v in roleplay_histories.items()},
    })


def load_fanfic_histories() -> dict[int, list]:
    raw = _load_json(FANFIC_HISTORIES_FILE, {})
    return {int(k): v for k, v in raw.items()}

def save_fanfic_histories():
    _save_json(FANFIC_HISTORIES_FILE, {str(k): list(v) for k, v in channel_histories.items() if k in fanfic_thread_ids})
    _save_json(FANFIC_OWNERS_FILE, {
        str(k): {"owner_id": v["owner_id"], "invited_ids": list(v["invited_ids"])}
        for k, v in fanfic_owners.items()
    })

def load_fanfic_owners() -> dict[int, dict]:
    raw = _load_json(FANFIC_OWNERS_FILE, {})
    return {int(k): {"owner_id": v["owner_id"], "invited_ids": set(v["invited_ids"])} for k, v in raw.items()}


def load_economy() -> dict:
    data = _load_json(ECONOMY_FILE, {"users": {}, "last_daily_reset": None})
    data.setdefault("last_daily_reset", None)
    data.setdefault("guild_house", {})
    return data


def save_economy():
    _save_json(ECONOMY_FILE, economy)


def load_jackpot() -> int:
    return max(SLOT_JACKPOT_SEED, _load_json(SLOT_JACKPOT_FILE, {}).get("jackpot", SLOT_JACKPOT_SEED))


def save_jackpot(value: int):
    _save_json(SLOT_JACKPOT_FILE, {"jackpot": value})


def load_bot_roles() -> set[int]:
    return set(_load_json(BOT_ROLES_FILE, []))


def save_bot_roles():
    _save_json(BOT_ROLES_FILE, list(bot_roles))


def load_bot_admins() -> set[int]:
    return set(_load_json(BOT_ADMINS_FILE, [INITIAL_BOT_ADMIN_ID]))


def save_bot_admins():
    _save_json(BOT_ADMINS_FILE, list(bot_admins))


def load_bot_settings() -> dict:
    return _load_json(BOT_SETTINGS_FILE, {"vram_text": "16GB"})


def save_bot_settings():
    _save_json(BOT_SETTINGS_FILE, bot_settings)


def load_godmode_users() -> set[int]:
    return set(_load_json(GODMODE_USERS_FILE, []))


def save_godmode_users():
    _save_json(GODMODE_USERS_FILE, list(godmode_users))


def load_chess_games() -> dict:
    # JSON requires string keys; convert back to ints on load
    return {int(k): v for k, v in _load_json(CHESS_GAMES_FILE, {}).items()}


def save_chess_games():
    _save_json(CHESS_GAMES_FILE, active_chess_games)


def load_guild_settings() -> dict:
    return _load_json(GUILD_SETTINGS_FILE, {})


def save_guild_settings():
    _save_json(GUILD_SETTINGS_FILE, guild_settings)


def get_guild_cfg(guild_id: int) -> dict:
    key = str(guild_id)
    if key not in guild_settings:
        guild_settings[key] = {}
    return guild_settings[key]


def load_insurance() -> dict:
    data = _load_json(INSURANCE_FILE, {})
    now = time.time()
    return {k: v for k, v in data.items() if v.get("expires_at", 0) > now}


def save_insurance():
    _save_json(INSURANCE_FILE, insurance)


def load_ragebait() -> dict:
    return _load_json(RAGEBAIT_FILE, {})


def save_ragebait():
    _save_json(RAGEBAIT_FILE, active_ragebaits)


def load_mock() -> dict:
    return _load_json(MOCK_FILE, {})


def save_mock():
    _save_json(MOCK_FILE, active_mocks)


def load_rigged_slots() -> set[int]:
    return set(_load_json(RIGGED_SLOTS_FILE, []))


def save_rigged_slots():
    _save_json(RIGGED_SLOTS_FILE, list(rigged_slots))


def load_gambler_streak() -> dict:
    """Map of str(user_id) -> last date they used all 3 scratchoffs (ISO format)."""
    return _load_json(GAMBLER_STREAK_FILE, {})


def save_gambler_streak():
    _save_json(GAMBLER_STREAK_FILE, gambler_streak)


def load_quote_log() -> list[str]:
    return _load_json(QUOTE_LOG_FILE, [])


def save_quote_log(log: list[str]):
    _save_json(QUOTE_LOG_FILE, log[-10:])


def load_saved_quotes() -> dict:
    return _load_json(SAVED_QUOTES_FILE, {})


def save_saved_quotes(quotes: dict):
    _save_json(SAVED_QUOTES_FILE, quotes)


def load_simp() -> dict:
    return {int(k): v for k, v in _load_json(SIMP_FILE, {}).items()}


def save_simp(simp_data: dict):
    _save_json(SIMP_FILE, simp_data)


def load_curse() -> dict:
    return {int(k): v for k, v in _load_json(CURSE_FILE, {}).items()}


def save_curse(curse_data: dict):
    _save_json(CURSE_FILE, curse_data)


def load_lottery(guild_id: int) -> dict:
    return _load_json(f"data/lottery_{guild_id}.json", {"prize_pool": 0, "players": {}, "last_posted_week": 0})


def save_lottery(guild_id: int, lottery_data: dict):
    _save_json(f"data/lottery_{guild_id}.json", lottery_data)


def get_guild_house_balance(guild_id: int) -> int:
    return economy.get("guild_house", {}).get(str(guild_id), 0)


def add_guild_house(guild_id: int, amount: int):
    economy.setdefault("guild_house", {})
    key = str(guild_id)
    economy["guild_house"][key] = economy["guild_house"].get(key, 0) + amount
    save_economy()


def drain_bot_balance_into_lottery(lottery: dict, guild_id: int) -> int:
    """Transfer this guild's house balance into the lottery prize pool. Returns the amount transferred."""
    economy.setdefault("guild_house", {})
    key = str(guild_id)
    house_balance = economy["guild_house"].get(key, 0)
    if house_balance > 0:
        economy["guild_house"][key] = 0
        save_economy()
        lottery["prize_pool"] = lottery.get("prize_pool", 0) + house_balance
    return house_balance


async def announce_new_lottery(channel: discord.TextChannel, prize_pool: int = 2000, now: datetime.datetime = None):
    """Announce a new lottery week to the specified channel."""
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)

    # Calculate next Saturday 6pm CT (handles CST/CDT automatically)
    ct = ZoneInfo("America/Chicago")
    now_cst = now.astimezone(ct)
    days_until_saturday = (5 - now_cst.weekday()) % 7
    if days_until_saturday == 0:
        days_until_saturday = 7
    next_saturday = now_cst + datetime.timedelta(days=days_until_saturday)
    next_saturday = next_saturday.replace(hour=18, minute=0, second=0, microsecond=0)
    timestamp = int(next_saturday.timestamp())

    embed = discord.Embed(title="🎰 New Lottery Week", color=C_PURPLE)
    embed.description = (
        "A new lottery has started! Buy tickets with `!lottery <n>`\n\n"
        f"**Prize Pool:** {prize_pool:,} 🪙 (+1,000 🪙 per player)\n"
        f"**Ticket Cost:** 10 🪙 for 1 🎟️\n"
        f"**Ends:** <t:{timestamp}:R>"
    )
    await channel.send(embed=embed)


def is_insured(uid: int, against: str) -> bool:
    if str(uid) not in insurance:
        return False
    entry = insurance[str(uid)]
    if entry.get("expires_at", 0) <= time.time():
        del insurance[str(uid)]
        save_insurance()
        return False
    return against in entry.get("protected_from", [])


def get_guild_ask_model(guild_id: int) -> str:
    """Get the ask model for a guild, with fallback to default."""
    cfg = get_guild_cfg(guild_id)
    return cfg.get("ask_model", OLLAMA_MODEL)


def get_guild_roleplay_model(guild_id: int) -> str:
    """Get the roleplay model for a guild, with fallback to default."""
    cfg = get_guild_cfg(guild_id)
    return cfg.get("roleplay_model", OLLAMA_MODEL)


def get_guild_coding_model(guild_id: int) -> str:
    """Get the coding puzzle model for a guild, with fallback to default."""
    cfg = get_guild_cfg(guild_id)
    return cfg.get("coding_model", OLLAMA_MODEL)


async def _wrong_channel_reply(ctx_or_msg, text: str) -> None:
    """Send a ❌ Wrong Channel embed and delete both the trigger and the reply after 10 s."""
    if isinstance(ctx_or_msg, commands.Context):
        message = ctx_or_msg.message
        reply_fn = ctx_or_msg.reply
    else:
        message = ctx_or_msg
        reply_fn = ctx_or_msg.reply
    reply = await reply_fn(embed=emb("❌ Wrong Channel", text, C_RED), mention_author=False)
    asyncio.create_task(_delete_after(message, 10.0))
    asyncio.create_task(_delete_after(reply, 10.0))


async def check_channel(ctx: commands.Context, *config_keys: str, label: str = "These") -> bool:
    """Return True (and send a timed reply) if the current channel is not in any of the configured channel lists.

    Checks each config_key in order, unions all resulting channel IDs.  If the
    union is non-empty and the current channel is not in it, the user gets a
    'wrong channel' reply and True is returned (caller should ``return``).
    Passing multiple keys is used when a command is valid in multiple channel
    types (e.g. puzzle = ai_channels | game_channels).
    A fallback key can be expressed by passing two keys where the second is only
    checked when the first is empty — callers that need that behaviour pass both.
    """
    if not ctx.guild:
        return False
    cfg = get_guild_cfg(ctx.guild.id)
    allowed: set[int] = set()
    for key in config_keys:
        allowed |= set(cfg.get(key, []))
    if allowed and ctx.channel.id not in allowed:
        names = " ".join(f"<#{cid}>" for cid in allowed)
        await _wrong_channel_reply(ctx, f"{label} commands are only allowed in: {names}")
        return True
    return False


async def check_game_channel(ctx: commands.Context, label: str = "Games") -> bool:
    return await check_channel(ctx, "game_channels", label=label)


async def check_ai_channel(ctx: commands.Context) -> bool:
    return await check_channel(ctx, "ai_channels", label="AI")


async def check_puzzle_channel(ctx: commands.Context) -> bool:
    return await check_channel(ctx, "ai_channels", "game_channels", label="Puzzle")


async def check_chess_channel(ctx: commands.Context) -> bool:
    cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
    chess_channels = cfg.get("chess_channels", []) or cfg.get("game_channels", [])
    if chess_channels and ctx.channel.id not in chess_channels:
        names = " ".join(f"<#{cid}>" for cid in chess_channels)
        await _wrong_channel_reply(ctx, f"Chess is only allowed in: {names}")
        return True
    return False


channel_prompts = load_channel_prompts()
economy = load_economy()
slot_jackpot = load_jackpot()
bot_roles: set[int] = load_bot_roles()
bot_admins: set[int] = load_bot_admins()
godmode_users: set[int] = load_godmode_users()
bot_settings: dict = load_bot_settings()
guild_settings: dict = load_guild_settings()
insurance: dict = load_insurance()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ── In-memory game state ──────────────────────────────────────────────────────
active_blackjack_games: dict[int, dict] = {}
active_hangman_games: dict[int, dict] = {}
active_roleplays: dict[int, dict] = {}
roleplay_histories: dict[int, list] = {}
active_events: dict[int, dict] = {}     # message_id → {amount, rewarded: set}
active_ragebaits: dict[int, dict] = load_ragebait() # user_id → {remaining: int, history: list[str]}
active_ttt_games: dict[int, dict] = {}  # channel_id → {board, players, marks, current}
active_c4_games: dict[int, dict] = {}   # channel_id → {board, players, marks, current}
active_race_games: dict[int, dict] = {} # channel_id → {players, names, positions, amount}
active_chess_games: dict[int, dict] = load_chess_games() # channel_id → {board, players, current, moves, amount}
active_puzzles: dict[int, dict] = {}  # channel_id → {question, answer, reward, user_id}
rigged_slots: set[int] = load_rigged_slots() # user_id → will hit jackpot on next spin
gambler_streak: dict = load_gambler_streak() # str(user_id) → last date they used all 3 scratchoffs
quote_log: list[str] = load_quote_log() # last 10 quotes used

# ── Misc state ────────────────────────────────────────────────────────────────
channel_histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
fanfic_thread_ids: set[int] = set()
fanfic_owners: dict[int, dict] = {}  # thread_id → {owner_id, invited_ids: set}
user_last_request: dict[int, float] = {}

# ── Stats ─────────────────────────────────────────────────────────────────────
bot_start_time = time.monotonic()
stats_commands_ran: int = 0
stats_messages_seen: int = 0

# ── Audit log ─────────────────────────────────────────────────────────────────
audit_log: deque = deque(maxlen=20)

# ── Mock state ────────────────────────────────────────────────────────────────
active_mocks: dict[int, dict] = load_mock() # user_id → {remaining: int, started_by: int}

# ── Simp state ────────────────────────────────────────────────────────────────
active_simps: dict[int, int] = load_simp() # user_id → simped_by_user_id

# ── Curse state ───────────────────────────────────────────────────────────────
active_curses: dict[int, dict] = load_curse() # user_id → {cursed_by: int, remaining: int}

# ── Soundboard rate-limit tracking ───────────────────────────────────────────
# (guild_id, user_id) → list of float timestamps (time.monotonic())
_soundboard_timestamps: dict[tuple[int, int], list[float]] = {}

# ── Lottery state ─────────────────────────────────────────────────────────────
# Lotteries are per-guild, loaded on demand from data/lottery_{guild_id}.json


# ─────────────────────────────────────────────────────────────────────────────
# Economy helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_user(uid: int):
    key = str(uid)
    if key not in economy["users"]:
        economy["users"][key] = {"balance": 0, "last_daily": 0.0}
        save_economy()


def get_balance(uid: int) -> int:
    _ensure_user(uid)
    return economy["users"][str(uid)]["balance"]


def add_balance(uid: int, n: int):
    _ensure_user(uid)
    economy["users"][str(uid)]["balance"] += n
    save_economy()


def deduct_balance(uid: int, n: int) -> bool:
    _ensure_user(uid)
    key = str(uid)
    if economy["users"][key]["balance"] < n:
        return False
    economy["users"][key]["balance"] -= n
    save_economy()
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_admin(ctx: commands.Context) -> bool:
    return ctx.author.id in bot_admins


def is_server_admin(ctx: commands.Context) -> bool:
    return ctx.guild is not None and ctx.author.guild_permissions.administrator


def can_manage_settings(ctx: commands.Context) -> bool:
    return is_admin(ctx) or is_server_admin(ctx)


def check_rate_limit(user_id: int) -> bool:
    now = time.monotonic()
    last = user_last_request.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    user_last_request[user_id] = now
    return False


def log_bot_permission_error(ctx: commands.Context, error_msg: str):
    """Log a bot permission error to the audit log."""
    audit_log.append({
        "time": time.time(),
        "user": f"{ctx.author.display_name} ({ctx.author.id})",
        "command": ctx.message.content[:100],
        "error": f"Bot Permission Error: {error_msg}",
    })


def _log_audit(user: str, command: str, error: str):
    """Log an error to the audit log without a ctx object."""
    audit_log.append({
        "time": time.time(),
        "user": user,
        "command": command,
        "error": error,
    })


def get_system_prompt(channel_id: int) -> str:
    return channel_prompts.get(channel_id, SYSTEM_PROMPT)


def mocking_font(text: str) -> str:
    """Convert text to mocking alternating case: LiKe ThIs."""
    result = []
    uppercase = False
    for char in text:
        if char.isalpha():
            result.append(char.upper() if uppercase else char.lower())
            uppercase = not uppercase
        else:
            result.append(char)
    return "".join(result)


def curse_font(text: str) -> str:
    """Convert text to cursed alternating case but reversed: tHiS iS cUrSeD."""
    result = []
    uppercase = True  # Start with uppercase for curse effect
    for char in text:
        if char.isalpha():
            result.append(char.upper() if uppercase else char.lower())
            uppercase = not uppercase
        else:
            result.append(char)
    return "".join(result)


def get_memory_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0


def format_uptime() -> str:
    seconds = int(time.monotonic() - bot_start_time)
    days, r = divmod(seconds, 86400)
    hours, r = divmod(r, 3600)
    minutes = r // 60
    return f"{days}d {hours}h {minutes}m"


def get_version() -> str:
    try:
        commit_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        commit_count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return f"{commit_count} ({commit_hash})"
    except Exception:
        return "unknown"


async def check_ollama_connected() -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{OLLAMA_BASE_URL}/api/tags",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


# ── Blackjack helpers ─────────────────────────────────────────────────────────

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


def new_deck() -> list[dict]:
    deck = [{"rank": r, "suit": s} for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def draw_card(deck: list[dict]) -> dict:
    return deck.pop()


def hand_value(hand: list[dict]) -> int:
    total = 0
    aces = 0
    for card in hand:
        r = card["rank"]
        if r in ("J", "Q", "K"):
            total += 10
        elif r == "A":
            aces += 1
            total += 11
        else:
            total += int(r)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def format_hand(hand: list[dict], hide_second: bool = False) -> str:
    if hide_second and len(hand) >= 2:
        return f"{hand[0]['rank']}{hand[0]['suit']}  🂠"
    return "  ".join(f"{c['rank']}{c['suit']}" for c in hand)


def build_blackjack_display(
    player: list[dict],
    dealer: list[dict],
    pval: int,
    hide_dealer: bool = False,
    dval: int = None,
) -> str:
    dealer_str = format_hand(dealer, hide_second=hide_dealer)
    player_str = format_hand(player)
    dealer_label = "Dealer" if hide_dealer or dval is None else f"Dealer ({dval})"
    return f"**{dealer_label}:** {dealer_str}\n**You ({pval}):** {player_str}"


# ── Hangman helpers ───────────────────────────────────────────────────────────

_hangman_words_path = os.path.join(os.path.dirname(__file__), "hangman_words.txt")
with open(_hangman_words_path) as _f:
    HANGMAN_WORDS = [w.strip() for w in _f if w.strip()]

HANGMAN_ART = [
    "```\n  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========```",
]


def build_hangman_display(game: dict) -> str:
    word = game["word"]
    guessed = game["guessed_letters"]
    wrong = game["wrong_guesses"]
    blanks = " ".join(c if c in guessed else "_" for c in word)
    guessed_str = ", ".join(sorted(guessed)) if guessed else "none"
    lives_left = 6 - wrong
    return (
        f"{HANGMAN_ART[wrong]}\n"
        f"Word: `{blanks}`\n"
        f"Guessed: {guessed_str}\n"
        f"Lives left: {lives_left}"
    )


# ── Embed colors ──────────────────────────────────────────────────────────────

C_GREEN  = 0x2ecc71  # win, success, economy
C_RED    = 0xe74c3c  # loss, error
C_GOLD   = 0xf1c40f  # gambling neutral, admin, cooldown
C_ORANGE = 0xe67e22  # games in-progress
C_BLUE   = 0x3498db  # blackjack in-progress, info
C_PURPLE = 0x9b59b6  # shop
C_GREY   = 0x95a5a6  # utility, neutral


def emb(title: str, description: str, color: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


# ─────────────────────────────────────────────────────────────────────────────
# Shared command helpers
# ─────────────────────────────────────────────────────────────────────────────

async def parse_amount(ctx: commands.Context, value: str, min_val: int = 1,
                       error_msg: str = "Please provide a positive whole number amount.") -> int | None:
    """Parse a string as a positive integer >= min_val.  Sends error_msg (if non-empty) and returns None on failure."""
    try:
        amount = int(value)
        assert amount >= min_val
        return amount
    except (ValueError, AssertionError):
        if error_msg:
            await ctx.send(error_msg)
        return None


# AI feature costs (coins).  0 = free.
FEATURE_COSTS: dict[str, int] = {
    "ask": 10,
    "fanfic": 20,
    "continue": 10,
    "roleplay": 50,
    "rpg": 50,
}

_FEATURE_LABELS: dict[str, str] = {
    "ask": "Asking",
    "fanfic": "Fan fiction",
    "continue": "Continuing",
    "roleplay": "Starting a roleplay",
    "rpg": "Starting an RPG adventure",
}


async def enforce_cost(ctx: commands.Context, feature: str) -> bool:
    """Deduct the coin cost for *feature*.  Returns True if the user can proceed.
    Godmode users are never charged.  Sends an 'Insufficient Funds' embed and
    returns False when the user can't afford it."""
    uid = ctx.author.id
    cost = 0 if uid in godmode_users else FEATURE_COSTS.get(feature, 0)
    if cost == 0:
        return True
    if not deduct_balance(uid, cost):
        label = _FEATURE_LABELS.get(feature, feature.title())
        await ctx.send(embed=emb(
            "💸 Insufficient Funds",
            f"{label} costs **{cost} 🪙**. Balance: {get_balance(uid)} 🪙",
            C_RED,
        ))
        return False
    return True


async def insufficient_funds(ctx_or_send, uid: int, *, label: str = "") -> None:
    """Send a standard Insufficient Funds embed.  *ctx_or_send* may be a
    ``commands.Context`` or any callable that accepts an ``embed=`` kwarg."""
    desc = f"{label + ' ' if label else ''}Balance: {get_balance(uid)} 🪙"
    e = emb("💸 Insufficient Funds", desc, C_RED)
    if callable(ctx_or_send) and not isinstance(ctx_or_send, commands.Context):
        await ctx_or_send(embed=e)
    else:
        await ctx_or_send.send(embed=e)


def resolve_role(guild: discord.Guild, token: str) -> discord.Role | None:
    """Resolve a role from a mention (<@&ID>) or plain name string."""
    token = token.strip()
    if token.startswith("<@&") and token.endswith(">"):
        try:
            role_id = int(token[3:-1])
            return guild.get_role(role_id)
        except ValueError:
            return None
    return discord.utils.get(guild.roles, name=token)


async def fetch_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """Return a guild member by ID, falling back to an API fetch if not cached."""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None
    return member


async def toggle_member_role(member: discord.Member, role: discord.Role,
                             add: bool, reason: str = "") -> bool:
    """Add or remove *role* from *member*.  Returns True on success, False on Discord error.
    No-ops silently if the member already has / doesn't have the role."""
    try:
        if add:
            if role not in member.roles:
                await member.add_roles(role, reason=reason)
        else:
            if role in member.roles:
                await member.remove_roles(role, reason=reason)
        return True
    except Exception:
        return False


async def shop_charge(ctx: commands.Context, uid: int, cost: int,
                      cost_label: str | None = None) -> bool:
    """Check and deduct *cost* coins for a shop action.  Godmode users are free.
    Returns True if the user can proceed; sends an Insufficient Funds embed and
    returns False otherwise."""
    if uid in godmode_users or cost == 0:
        return True
    if not deduct_balance(uid, cost):
        label_str = f"This costs **{cost_label or f'{cost:,}'} 🪙**. "
        await ctx.send(embed=emb(
            "💸 Insufficient Funds",
            f"{label_str}Balance: {get_balance(uid)} 🪙",
            C_RED,
        ))
        return False
    return True


def _render_race(game: dict) -> str:
    """Render the race board with each player's lane."""
    lines = []
    for uid in game["players"]:
        pos = game["positions"][uid]
        name = game["names"][uid]
        track = "▓" * pos + "🏇" + "░" * (RACE_TRACK_LEN - pos)
        lines.append(f"`{track}` **{name}**")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Gambler role helpers
# ─────────────────────────────────────────────────────────────────────────────

async def get_or_create_gamblers_role(guild: discord.Guild) -> discord.Role | None:
    """Return the 'Gamblers' role, creating it if it doesn't exist."""
    role = discord.utils.get(guild.roles, name="Gamblers")
    if role is None:
        try:
            role = await guild.create_role(name="Gamblers", reason="Auto-created for gambler role tracking")
        except Exception:
            return None
    return role


async def maybe_assign_gambler_role(guild: discord.Guild, member: discord.Member, channel: discord.abc.Messageable):
    """Assign the Gamblers role if the user used all 3 scratchoffs 2 days in a row."""
    cfg = get_guild_cfg(guild.id)
    if not cfg.get("gambler_role_enabled", False):
        return

    uid_key = str(member.id)
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    last_full_day = gambler_streak.get(uid_key)
    if last_full_day == yesterday:
        role = await get_or_create_gamblers_role(guild)
        if role and role not in member.roles:
            if await toggle_member_role(member, role, True, reason="Used all 3 scratchoffs 2 days in a row"):
                await channel.send(
                    f"🎲 {member.mention} You've been automatically added to the **Gamblers** role for using all 3 scratchoffs 2 days in a row! "
                    f"You'll be pinged whenever a progressive jackpot is won. "
                    f"Use `!gambler-role off` to opt out."
                )


# ─────────────────────────────────────────────────────────────────────────────
# Async infrastructure
# ─────────────────────────────────────────────────────────────────────────────

# Global semaphore — only one Ollama request runs at a time to avoid GPU overload.
ollama_semaphore = asyncio.Semaphore(1)


async def keep_typing(channel: discord.abc.Messageable):
    try:
        while True:
            await channel.trigger_typing()
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass


async def stream_ollama(
    session: aiohttp.ClientSession,
    messages: list[dict],
    placeholder: discord.Message,
    model: str = None,
    guild_id: int = None,
) -> str:
    if not bot_settings.get("ai_enabled", True):
        await placeholder.edit(content="", embed=emb("🤖 AI Offline", "Passive AI responses are currently disabled.", C_RED))
        return ""
    if model:
        used_model = model
    elif guild_id:
        used_model = get_guild_ask_model(guild_id)
    else:
        used_model = OLLAMA_MODEL
    payload = {"model": used_model, "messages": messages, "stream": True}

    full_response = ""
    last_edit = 0.0
    EDIT_INTERVAL = 0.8

    if ollama_semaphore.locked():
        try:
            await placeholder.edit(content="⏳ Another AI request is running. You're next...")
        except Exception:
            pass

    async with ollama_semaphore:
        async with session.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for raw_line in resp.content:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = data.get("message", {}).get("content", "")
                full_response += token
                now = time.monotonic()
                if now - last_edit >= EDIT_INTERVAL and full_response:
                    display = full_response[-1997:] if len(full_response) > 1997 else full_response
                    try:
                        await placeholder.edit(content=display + "▌")
                        last_edit = now
                    except discord.HTTPException:
                        pass
                if data.get("done"):
                    break

    return full_response


async def finalize(placeholder: discord.Message, channel: discord.abc.Messageable, text: str):
    chunks = [text[i:i + 2000] for i in range(0, max(len(text), 1), 2000)]
    await placeholder.edit(content=chunks[0])
    for chunk in chunks[1:]:
        await channel.send(chunk)


ASK_SYSTEM_PROMPT = (
    "You are a knowledgeable and helpful assistant. "
    "Answer questions clearly, accurately, and concisely. "
    "Use markdown formatting where it improves readability (e.g. bullet points, code blocks). "
    "If you are uncertain about something, say so. "
    "Do not make up information."
)

FANFIC_SYSTEM_PROMPT = (
    "You are a bold, creative fiction writer specializing in steamy, adult romantic fan fiction. "
    "Your stories are imaginative, dramatic, and elaborately detailed — never timid or vague. "
    "When given a topic, subject, characters, or scenario, write an original, self-contained short story "
    "(roughly 400–700 words) that is sensual, emotionally charged, and engaging from start to finish. "
    "Use vivid descriptive language, charged dialogue, and strong narrative momentum. "
    "Build tension before the payoff. Give characters distinct voices and desires. "
    "The story should feel complete — with a beginning that hooks the reader, a middle that escalates, "
    "and a satisfying, steamy conclusion. "
    "Do not summarize or describe what you are about to write — just write the story directly. "
    "Do not add disclaimers, caveats, or author's notes."
)


def _norm_puzzle_answer(s: str) -> str:
    """Normalize a puzzle answer for comparison: lowercase, collapse whitespace."""
    return " ".join(s.lower().split())


async def _delete_after(message: discord.Message, delay: float = 5.0):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass


async def _edit_board(channel: discord.abc.Messageable, game: dict, embed: discord.Embed):
    """Edit the persistent board message in-place; falls back to a new send if deleted."""
    try:
        msg = await channel.fetch_message(game["board_msg_id"])
        await msg.edit(embed=embed)
    except (discord.NotFound, discord.HTTPException):
        await channel.send(embed=embed)


async def _execute_ollama_stream(channel, reply_to, messages, history, guild_id=None, model=None, placeholder=None):
    if placeholder is None:
        placeholder = await reply_to.reply("...")
    typing_task = asyncio.create_task(keep_typing(channel))
    author = f"{reply_to.author.display_name} ({reply_to.author.id})"
    command = reply_to.content[:100]
    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(session, messages, placeholder, guild_id=guild_id, model=model)
        history.append({"role": "assistant", "content": full_response})
        await finalize(placeholder, channel, full_response)
    except aiohttp.ClientError as e:
        history.pop()
        _log_audit(author, command, f"Ollama offline: {e}")
        await placeholder.edit(content="", embed=emb("", "The AI is currently offline", C_RED))
    except Exception as e:
        history.pop()
        _log_audit(author, command, f"{type(e).__name__}: {e}")
        await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
    finally:
        typing_task.cancel()


async def respond(
    channel: discord.abc.Messageable,
    user_id: int,
    content: str,
    reply_to: discord.Message,
    system_prompt: str = None,
    guild_id: int = None,
    author_name: str = None,
):
    channel_id = channel.id
    history = channel_histories[channel_id]
    system_prompt = system_prompt or get_system_prompt(channel_id)

    formatted_content = f"{author_name}: {content}" if author_name else content
    history.append({"role": "user", "content": formatted_content})
    messages = [{"role": "system", "content": system_prompt}] + list(history)

    placeholder = await channel.send("...") if isinstance(channel, discord.Thread) else None
    await _execute_ollama_stream(channel, reply_to, messages, history, guild_id=guild_id, placeholder=placeholder)


async def respond_roleplay(
    channel: discord.abc.Messageable,
    user_id: int,
    content: str,
    reply_to: discord.Message,
    author_name: str = None,
):
    rp = active_roleplays[user_id]
    history_key = rp.get("history_owner", user_id)
    history = roleplay_histories.setdefault(history_key, [])

    # Use custom system prompt for RPG adventures, standard roleplay prompt otherwise
    if rp.get("is_rpg"):
        system_prompt = rp.get("system_prompt")
    else:
        system_prompt = (
            f"You are roleplaying as the following character and must stay in character "
            f"for every response, no matter what: {rp['character_prompt']}. "
            f"Never break character or acknowledge that you are an AI."
        )

    formatted_content = f"{author_name}: {content}" if author_name else content
    history.append({"role": "user", "content": formatted_content})
    messages = [{"role": "system", "content": system_prompt}] + history

    guild_id = rp.get("guild_id")
    model = get_guild_roleplay_model(guild_id) if guild_id else OLLAMA_MODEL
    placeholder = await channel.send("...") if isinstance(channel, discord.Thread) else None
    await _execute_ollama_stream(channel, reply_to, messages, history, model=model, placeholder=placeholder)
    save_roleplay_state()


async def _run_race(channel, cid: int, race_msg: discord.Message):
    """Animate and run a race until there's a winner."""
    import random

    game = active_race_games[cid]

    while cid in active_race_games:
        await asyncio.sleep(1.5)
        if cid not in active_race_games:
            break

        # Advance each player
        for uid in game["players"]:
            game["positions"][uid] = min(
                game["positions"][uid] + random.randint(1, 3),
                RACE_TRACK_LEN,
            )

        # Check for winners
        winners = [uid for uid in game["players"] if game["positions"][uid] >= RACE_TRACK_LEN]
        board = _render_race(game)

        if winners:
            del active_race_games[cid]
            amount = game["amount"]
            total_pot = amount * len(game["players"])
            share = total_pot // len(winners) if winners else 0

            if len(winners) == 1:
                winner_name = game["names"][winners[0]]
                if share > 0:
                    add_balance(winners[0], share)
                result = f"{board}\n\n🏆 **{winner_name}** wins" + (f" **{share} 🪙**!" if share else "!")
            else:
                for w in winners:
                    if share > 0:
                        add_balance(w, share)
                names = ", ".join(f"**{game['names'][w]}**" for w in winners)
                result = f"{board}\n\n🤝 Tie! {names} each get **{share} 🪙**"

            await race_msg.edit(embed=emb("🏁 Race Finished!", result, C_GREEN))
            return

        # Update board
        await race_msg.edit(embed=emb("🏇 Race in Progress", board, C_ORANGE))


async def _blackjack_stand(message: discord.Message, uid: int, game: dict):
    dealer = game["dealer_hand"]
    player = game["player_hand"]
    deck = game["deck"]

    while hand_value(dealer) <= 16:
        dealer.append(draw_card(deck))

    pval = hand_value(player)
    dval = hand_value(dealer)
    amount = game["amount"]
    del active_blackjack_games[uid]

    display = build_blackjack_display(player, dealer, pval, hide_dealer=False, dval=dval)

    if dval > 21 or pval > dval:
        add_balance(uid, amount * 2)
        color, result = C_GREEN, f"✅ You win **{amount} 🪙**! Balance: {get_balance(uid)} 🪙"
    elif pval == dval:
        add_balance(uid, amount)
        color, result = C_GOLD, f"🤝 Push! Bet returned. Balance: {get_balance(uid)} 🪙"
    else:
        color, result = C_RED, f"❌ Dealer wins. You lose **{amount} 🪙**. Balance: {get_balance(uid)} 🪙"

    await message.channel.send(embed=emb("🃏 Blackjack", display + f"\n\n{result}", color))


# ─────────────────────────────────────────────────────────────────────────────
# Global checks
# ─────────────────────────────────────────────────────────────────────────────

@bot.check
async def global_command_channel_check(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return True
    if ctx.command and ctx.command.name in ("settings", "clear"):
        return True  # always allow !settings and !clear in any channel

    cfg = get_guild_cfg(ctx.guild.id)

    # Allow searchquote if bypass is enabled
    if ctx.command and ctx.command.name == "searchquote":
        if cfg.get("quote_bypass_restrictions", False):
            return True

    # !quote (save/display) always allowed in any channel
    if ctx.command and ctx.command.name == "quote":
        return True

    # Check blacklist first (deny)
    command_blacklist = cfg.get("command_blacklist", [])
    if ctx.channel.id in command_blacklist:
        return False

    # Check whitelist (allow only if specified)
    command_whitelist = cfg.get("command_whitelist", [])
    if command_whitelist and ctx.channel.id not in command_whitelist:
        return False

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Events
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        logging.debug(f"[debug] {error}")
        return
    if isinstance(error, commands.CheckFailure):
        cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
        command_whitelist = cfg.get("command_whitelist", [])
        if command_whitelist:
            names = " ".join(f"<#{cid}>" for cid in command_whitelist)
            msg = f"Commands are only allowed in: {names}"
        else:
            msg = "Commands are not allowed in this channel."
        await _wrong_channel_reply(ctx, msg)
        return
    audit_log.append({
        "time": time.time(),
        "user": f"{ctx.author.display_name} ({ctx.author.id})",
        "command": ctx.message.content[:100],
        "error": f"{type(error).__name__}: {error}",
    })
    raise error


@bot.event
async def on_command_completion(ctx: commands.Context):
    global stats_commands_ran
    stats_commands_ran += 1


async def _roast_soundboard_spam(guild_id: int, user_id: int):
    """Generate a roast for soundboard spam using the ragebait system."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    member = await fetch_member(guild, user_id)
    if member is None:
        return

    roast_system = (
        "You are an expert at crafting witty, cutting roasts. Your goal is to roast someone "
        "for spamming soundboard sounds in a voice channel. "
        "Rules: be specific to the target by referring to them by name, be witty and sarcastic rather than just mean, "
        "make fun of them for the spam/spam in general, keep it under 150 characters, "
        "and make it feel natural — like something a friend would say. "
        "Output only the roast with no preamble, explanation, or quotation marks."
    )
    prompt = (
        f"Write a witty roast for {member.display_name} for spamming soundboard sounds in voice chat. "
        "Be sarcastic and funny. Do not use @ symbols."
    )

    try:
        # Create a fake channel/message context for the streaming function
        voice_channel = member.voice.channel if member.voice else None
        if voice_channel is None:
            return

        # Find the first AI channel in the guild to send the roast to
        cfg = get_guild_cfg(guild_id)
        ai_channels = cfg.get("ai_channels", [])
        text_channel = None
        if ai_channels:
            for ch_id in ai_channels:
                ch = guild.get_channel(ch_id)
                if ch and ch.permissions_for(guild.me).send_messages:
                    text_channel = ch
                    break
        if text_channel is None:
            return

        placeholder = await text_channel.send("...")
        typing_task = asyncio.create_task(keep_typing(text_channel))
        try:
            async with aiohttp.ClientSession() as session:
                full_response = await stream_ollama(session, [
                    {"role": "system", "content": roast_system},
                    {"role": "user", "content": prompt},
                ], placeholder)
            await finalize(placeholder, text_channel, f"{member.mention} {full_response}")
        except Exception as e:
            await placeholder.edit(content=f"⚠️ {e}")
        finally:
            typing_task.cancel()
    except Exception:
        pass


async def _handle_soundboard_ratelimit(guild_id: int, user_id: int):
    """Check if user exceeded soundboard rate limit; kick if so."""
    now = time.monotonic()
    key = (guild_id, user_id)
    timestamps = _soundboard_timestamps.setdefault(key, [])
    cutoff = now - SOUNDBOARD_WINDOW_SECS
    _soundboard_timestamps[key] = [t for t in timestamps if t >= cutoff]
    timestamps = _soundboard_timestamps[key]
    timestamps.append(now)
    if len(timestamps) <= SOUNDBOARD_MAX_SOUNDS:
        return
    # Threshold exceeded — clear so the same burst doesn't re-trigger
    _soundboard_timestamps[key] = []
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    member = await fetch_member(guild, user_id)
    if member is None or member.voice is None:
        return

    # Generate roast
    asyncio.create_task(_roast_soundboard_spam(guild_id, user_id))

    # Kick from voice channel
    try:
        await member.move_to(None)  # kick from voice channel
    except (discord.Forbidden, Exception):
        return


@bot.event
async def on_socket_raw_receive(msg):
    """Handle raw Discord gateway events; intercept VOICE_CHANNEL_EFFECT_SEND for soundboard rate-limiting."""
    try:
        data = json.loads(msg.decode("utf-8") if isinstance(msg, bytes) else msg)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if data.get("t") != "VOICE_CHANNEL_EFFECT_SEND":
        return
    d = data.get("d", {})
    if "sound_id" not in d:   # emoji reactions have no sound_id
        return
    try:
        guild_id = int(d["guild_id"])
        user_id  = int(d["user_id"])
    except (KeyError, ValueError, TypeError):
        return
    cfg = get_guild_cfg(guild_id)
    if user_id not in cfg.get("soundboard_ratelimit", []):
        return
    await _handle_soundboard_ratelimit(guild_id, user_id)


async def _auto_daily(message: discord.Message):
    """Award daily coins on first interaction of the day. Sends a short message if awarded."""
    uid = message.author.id
    _ensure_user(uid)
    today = datetime.date.today().isoformat()
    user_data = economy["users"][str(uid)]
    if user_data.get("daily_date") == today:
        return
    is_new = user_data.get("last_daily", 0.0) == 0.0
    add_balance(uid, 200)
    user_data["daily_date"] = today
    if is_new:
        user_data["last_daily"] = time.time()  # marks as no longer a new user
    save_economy()
    greeting = f"Welcome, **{message.author.display_name}**! 🎉 Here are your first" if is_new else "Daily coins ready!"
    await message.channel.send(embed=emb(
        "🪙 Daily Reward",
        f"{greeting} **200 🪙** added. Balance: {get_balance(uid)} 🪙",
        C_GREEN,
    ))


async def _passive_ragebait(message: discord.Message, history: list[str]):
    context = "\n".join(history)
    ragebait_system = (
        "You are an expert at crafting ragebait — messages specifically engineered to provoke "
        "an emotional reaction. Your goal is to write something that will genuinely irritate, "
        "annoy, or get under the skin of the target. "
        "Rules: be specific to the target by referring to them by name (no @ symbols), be witty and cutting rather than just insulting, "
        "use irony or condescension where effective, keep it under 200 characters, "
        "and make it feel natural — like something a person would actually say. "
        "Output only the ragebait message with no preamble, explanation, or quotation marks."
    )
    prompt = (
        f"Write a ragebait reply aimed at {message.author.display_name} based on what they just said. "
        f"Their recent messages for context:\n{context}\n"
        "Make it personal, pointed, and reactive to what they actually wrote. Do not use @ symbols."
    )
    placeholder = await message.reply("...")
    typing_task = asyncio.create_task(keep_typing(message.channel))
    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(session, [
                {"role": "system", "content": ragebait_system},
                {"role": "user", "content": prompt},
            ], placeholder)
        await finalize(placeholder, message.channel, f"{message.author.mention} {full_response}")
    except Exception as e:
        await placeholder.edit(content=f"⚠️ {e}")
    finally:
        typing_task.cancel()


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    uid = message.author.id
    content_lower = message.content.strip().lower()

    global stats_messages_seen, active_simps, active_curses
    stats_messages_seen += 1

    # Passive ragebait: track targeted users and fire at 50% chance
    if uid in active_ragebaits and not message.content.startswith("!"):
        # Only proceed if AI is online
        if await check_ollama_connected():
            rage = active_ragebaits[uid]
            rage["history"].append(f"[{message.author.display_name}]: {message.content[:200]}")
            rage["remaining"] -= 1
            if rage["remaining"] <= 0:
                del active_ragebaits[uid]
            save_ragebait()

            asyncio.create_task(_passive_ragebait(message, list(rage["history"])))

    # Mock: track mocked users and repeat their messages in mocking font
    if uid in active_mocks and not message.content.startswith("!"):
        mock = active_mocks[uid]
        mocked = mocking_font(message.content)
        await message.channel.send(mocked)
        mock["remaining"] -= 1
        if mock["remaining"] <= 0:
            del active_mocks[uid]
        save_mock()

    # Simp/Concubine tax: deduct coins from users who have a tax on them
    if uid in active_simps and not message.content.startswith("!"):
        simp_data = active_simps[uid]
        tax_type = simp_data["type"]

        # Check if concubine has expired (24h)
        if tax_type == "concubine" and "activated_at" in simp_data:
            time_elapsed = time.time() - simp_data["activated_at"]
            if time_elapsed > 86400:  # 24 hours in seconds
                del active_simps[uid]
                save_simp(active_simps)
            else:
                # Apply tax
                simp_master_id = simp_data["master"]
                simp_tax = 10
                if deduct_balance(uid, simp_tax):
                    add_balance(simp_master_id, simp_tax)
                    await message.channel.send(f"You paid a **{simp_tax} 🪙** Concubine tax to <@{simp_master_id}>")
        else:
            # Regular simp (permanent)
            simp_master_id = simp_data["master"]
            simp_tax = 10
            if deduct_balance(uid, simp_tax):
                add_balance(simp_master_id, simp_tax)
                await message.channel.send(f"You paid a **{simp_tax} 🪙** Simp tax to <@{simp_master_id}>")

    # Curse: corrupt cursed users' messages
    if uid in active_curses and not message.content.startswith("!"):
        curse = active_curses[uid]
        cursed = curse_font(message.content)
        await message.channel.send(cursed)
        curse["remaining"] -= 1
        if curse["remaining"] <= 0:
            del active_curses[uid]
        save_curse(active_curses)

    # Auto-award daily on any bot interaction (skip blacklisted channels)
    _is_dm = isinstance(message.channel, discord.DMChannel)
    _blacklisted = (
        not _is_dm
        and message.guild
        and message.channel.id in get_guild_cfg(message.guild.id).get("command_blacklist", [])
    )
    if not _blacklisted and (
        message.content.startswith("!")
        or bot.user in message.mentions
        or _is_dm
        or message.channel.id in active_hangman_games
        or uid in active_blackjack_games
    ):
        await _auto_daily(message)

    # Intercept hit / stand for active blackjack (with or without ! prefix)
    if content_lower in ("!hit", "!stand", "hit", "stand") and uid in active_blackjack_games and active_blackjack_games[uid].get("channel_id") == message.channel.id:
        game = active_blackjack_games[uid]
        if content_lower in ("!hit", "hit"):
            card = draw_card(game["deck"])
            game["player_hand"].append(card)
            pval = hand_value(game["player_hand"])
            display = build_blackjack_display(
                game["player_hand"], game["dealer_hand"], pval, hide_dealer=True
            )
            if pval > 21:
                del active_blackjack_games[uid]
                await message.channel.send(embed=emb(
                    "💥 Bust!",
                    display + f"\n\nYou lose **{game['amount']} 🪙**. Balance: {get_balance(uid)} 🪙",
                    C_RED,
                ))
            elif pval == 21:
                await _blackjack_stand(message, uid, game)
            else:
                await message.channel.send(embed=emb(
                    "🃏 Blackjack", display + "\n\n`!hit` to draw or `!stand` to hold.", C_BLUE
                ))
        else:
            await _blackjack_stand(message, uid, game)
        return

    # Intercept puzzle answers (must run before channel/AI guards)
    cid = message.channel.id
    if cid in active_puzzles and not message.content.startswith("!"):
        puzzle = active_puzzles[cid]
        invited = puzzle.get("invited_ids")
        if not invited or uid in invited:
            guess = message.content.strip()
            expected = puzzle["answer"]
            if _norm_puzzle_answer(guess) == _norm_puzzle_answer(expected):
                reward = puzzle["reward"]
                del active_puzzles[cid]
                add_balance(uid, reward)
                await message.channel.send(embed=emb(
                    "✅ Correct!",
                    f"{message.author.mention} got it!\n**Answer:** `{expected}`\n+**{reward} 🪙** (Balance: {get_balance(uid)} 🪙)",
                    C_GREEN,
                ))
                return

    # Intercept free-text hangman guesses (no prefix needed when game is active)
    # Only single-letter guesses via free-text; full words require !guess command
    cid = message.channel.id
    if cid in active_hangman_games and not message.content.startswith("!"):
        guess = message.content.lower().strip()
        if guess and guess.isalpha() and len(guess) == 1:
            asyncio.create_task(_delete_after(message))
            await _process_hangman_guess(message.channel, uid, cid, guess, message.author.display_name)
            return

    # AI enabled guard
    if not bot_settings.get("ai_enabled", True):
        await bot.process_commands(message)
        return

    # Channel guard
    if ACTIVE_CHANNEL_IDS and message.channel.id not in ACTIVE_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    # Only respond to mentions if the message starts with the mention
    is_mentioned = bot.user in message.mentions and message.content.strip().startswith(f"<@{bot.user.id}>")
    in_roleplay = uid in active_roleplays and active_roleplays[uid].get("channel_id") == message.channel.id

    # Ragebait and mock take precedence over normal mentions
    if uid in active_ragebaits or uid in active_mocks:
        await bot.process_commands(message)
        return

    # Check channel restrictions for mentions (AI channels and blacklist)
    if is_mentioned and not is_dm and message.guild:
        cfg = get_guild_cfg(message.guild.id)
        ai_channels = cfg.get("ai_channels", [])
        command_blacklist = cfg.get("command_blacklist", [])

        # Determine if channel is allowed for AI
        channel_allowed = True
        if ai_channels:
            # If AI channels configured, must be in one of them
            channel_allowed = message.channel.id in ai_channels
        elif message.channel.id in command_blacklist:
            # If no AI channels but channel is blacklisted, not allowed
            channel_allowed = False

        if not channel_allowed:
            if ai_channels:
                names = " ".join(f"<#{cid}>" for cid in ai_channels)
            else:
                # Show where AI IS allowed (inverse of blacklist)
                all_channels = [ch.id for ch in message.guild.text_channels if ch.id not in command_blacklist]
                names = " ".join(f"<#{cid}>" for cid in all_channels) if all_channels else "no channels"
            await _wrong_channel_reply(message, f"AI commands are only allowed in: {names}")
            await bot.process_commands(message)
            return

    if not (is_dm or is_mentioned or in_roleplay):
        await bot.process_commands(message)
        return

    # Skip bare commands during roleplay (let process_commands handle them)
    if in_roleplay and not is_mentioned and not is_dm and message.content.startswith("!"):
        await bot.process_commands(message)
        return

    content = message.content.replace(f"<@{bot.user.id}>", "").strip()
    if not content:
        await message.reply("Yes?")
        await bot.process_commands(message)
        return

    if check_rate_limit(uid):
        await message.reply("⚠️ Slow down! Please wait a moment before sending another message.")
        await bot.process_commands(message)
        return

    if uid in active_roleplays:
        await respond_roleplay(message.channel, uid, content, message, author_name=message.author.display_name)
    else:
        # Gate fanfic threads to invited participants only
        fo = fanfic_owners.get(message.channel.id)
        if fo and uid not in fo["invited_ids"]:
            await bot.process_commands(message)
            return
        guild_id = message.guild.id if message.guild else None
        await respond(message.channel, uid, content, message, guild_id=guild_id, author_name=message.author.display_name)

    await bot.process_commands(message)


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Utility
# ─────────────────────────────────────────────────────────────────────────────


@bot.command(name="gambler-role", aliases=["gamblerole", "gamblers"])
async def cmd_gambler_role(ctx: commands.Context, toggle: str = None):
    if not ctx.guild:
        return
    cfg = get_guild_cfg(ctx.guild.id)
    if not cfg.get("gambler_role_enabled", False):
        await ctx.send(embed=emb("🎲 Gambler Role", "The gambler role feature is not enabled on this server.", C_GREY))
        return

    if toggle is None or toggle.lower() not in ("on", "off"):
        has_role = discord.utils.get(ctx.author.roles, name="Gamblers") is not None
        status = "✅ you have it" if has_role else "❌ you don't have it"
        await ctx.send(embed=emb("🎲 Gambler Role", f"Gamblers role: {status}\nUse `!gambler-role on` or `!gambler-role off` to opt in/out.", C_GOLD))
        return

    role = await get_or_create_gamblers_role(ctx.guild)
    if role is None:
        await ctx.send(embed=emb("❌ Error", "Could not find or create the Gamblers role.", C_RED))
        return

    adding = toggle.lower() == "on"
    already = role in ctx.author.roles
    if adding and already:
        await ctx.send(embed=emb("🎲 Gambler Role", "You already have the Gamblers role.", C_GREY))
    elif not adding and not already:
        await ctx.send(embed=emb("🎲 Gambler Role", "You don't have the Gamblers role.", C_GREY))
    else:
        reason = "User opted in via !gambler-role" if adding else "User opted out via !gambler-role"
        if await toggle_member_role(ctx.author, role, adding, reason=reason):
            msg = "✅ You've been added to the **Gamblers** role. You'll be pinged when a progressive jackpot is won!" if adding else "✅ You've been removed from the **Gamblers** role."
            await ctx.send(embed=emb("🎲 Gambler Role", msg, C_GREEN))
        else:
            await ctx.send(embed=emb("❌ Error", f"Failed to {'add' if adding else 'remove'} the role.", C_RED))


@bot.command(name="help", aliases=["h"])
async def cmd_help(ctx: commands.Context):
    help_embed = discord.Embed(title="📖 Commands", color=0x3498db)
    help_embed.add_field(name="💰 Economy", inline=False, value=(
        "`!daily` — Claim 200 🪙 (24h cooldown)\n"
        "`!balance [@user]` — Check balance\n"
        "`!pay @user <amount>` — Send coins to another user\n"
        "`!leaderboard` — Top 10 richest users"
    ))
    help_embed.add_field(name="🎮 Games / Gambling", inline=False, value=(
        "`!games` — View all games and gambling commands"
    ))
    help_embed.add_field(name="🤖 AI", inline=False, value=(
        "`!ai` — View AI connection status and command info\n"
        "`!ask <question>` — Ask the AI a question\n"
        "`!fanfic <prompt>` — Generate a steamy fan fiction story (costs 20 🪙)\n"
        "`!continue` — Generate the next chapter in a fanfic thread (costs 10 🪙)\n"
        "`!tldr` — Summarize the last AI response in a fanfic or roleplay thread\n"
        "`!roleplay <character prompt> [@user1 @user2]` — Start a roleplay (costs 50 🪙, invite others with mentions)\n"
        "`!rpg [@user1 @user2]` — Start an interactive RPG adventure (costs 50 🪙, invite others with mentions)"
    ))
    help_embed.add_field(name="🛒 Shop", inline=False, value=(
        "`!shop` — Browse items\n"
    ))

    # Only show rule34 if enabled in guild
    if ctx.guild:
        cfg = get_guild_cfg(ctx.guild.id)
        r34_enabled = cfg.get("rule34_enabled", True)
    else:
        r34_enabled = True

    if r34_enabled:
        help_embed.add_field(name="🔞 NSFW", inline=False, value=(
            "`!rule34 [tags]` — Random image from rule34 (alias: `!r34`)"
        ))

    help_embed.add_field(name="🎉 Fun", inline=False, value=(
        "`!dog` — Random dog picture\n"
        "`!cat` — Random cat picture\n"
        "`!quote` — Save a quoted message (reply) or display a random saved quote\n"
        "`!searchquote [#channel] [@user]` — Find spicy/volatile messages to quote"
    ))
    utility_val = (
        "`!stats` — Show bot statistics\n"
        "`!stop` — Stop roleplay / forfeit active game"
    )
    if ctx.guild and get_guild_cfg(ctx.guild.id).get("gambler_role_enabled", False):
        utility_val += "\n`!gambler-role on|off` — Opt in/out of the Gamblers role"
    help_embed.add_field(name="🔧 Utility", inline=False, value=utility_val)
    await send_ephemeral(ctx, embed=help_embed)


@bot.command(name="stats", aliases=["stat"])
async def cmd_stats(ctx: commands.Context):
    elapsed = time.monotonic() - bot_start_time
    msg_rate = stats_messages_seen / (elapsed / 60) if elapsed > 0 else 0
    text_channels = sum(len(g.text_channels) for g in bot.guilds)
    voice_channels = sum(len(g.voice_channels) for g in bot.guilds)
    ai_connected = await check_ollama_connected()
    vram_text = bot_settings.get("vram_text", "16GB")

    embed = discord.Embed(title="📊 Bot Stats", color=C_BLUE)
    indent = "⠀ "  # Invisible character + space for indentation that Discord preserves
    embed.add_field(name="🤖 Bot", value=f"{indent}{bot.user}\n{bot.user.id}", inline=True)
    embed.add_field(name="⚙️ Shard", value=f"{indent}#0 / 1", inline=True)
    embed.add_field(name="💬 Commands Ran", value=f"{indent}{stats_commands_ran} Commands", inline=True)
    embed.add_field(name="📨 Messages", value=f"{indent}{stats_messages_seen} ({msg_rate:.2f}/min)", inline=True)
    embed.add_field(name="🧠 Memory", value=f"{indent}{get_memory_mb():.2f} MB", inline=True)
    embed.add_field(name="⏱️ Uptime", value=f"{indent}{format_uptime()}", inline=True)
    embed.add_field(name="🌐 Presence", value=(
        f"{indent}{len(bot.guilds)} Servers\n"
        f"{indent}{text_channels} Text Channels\n"
        f"{indent}{voice_channels} Voice Channels"
    ), inline=True)
    ai_enabled = bot_settings.get("ai_enabled", True)
    ai_status = "Online" if ai_connected else "Offline"
    ai_status_emoji = "🟢" if (ai_connected and ai_enabled) else "🔴"
    passive_status = "Enabled" if ai_enabled else "**Disabled**"
    ask_model = get_guild_ask_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
    roleplay_model = get_guild_roleplay_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
    coding_model = get_guild_coding_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
    embed.add_field(name=f"{ai_status_emoji} AI Status", value=(
        f"{indent}Status: {ai_status} · Passive: {passive_status}\n"
        f"{indent}Ask model: `{ask_model}`\n"
        f"{indent}Roleplay model: `{roleplay_model}`\n"
        f"{indent}Coding model: `{coding_model}`\n"
        f"{indent}vRAM: {vram_text}"
    ), inline=True)
    await send_ephemeral(ctx, embed=embed)


@bot.command(name="ai")
async def cmd_ai(ctx: commands.Context, state: str = None):
    # Bot-admin subcommands: on/online/off/offline
    if state is not None:
        if not is_admin(ctx):
            await ctx.send(embed=emb("❌ No Permission", "Only bot admins can use this command.", C_RED))
            return
        normalized = state.lower()
        if normalized in ("on", "online"):
            bot_settings["ai_enabled"] = True
            save_bot_settings()
            await ctx.send(embed=emb("🤖 AI Enabled", "Passive AI responses are now **online**.", C_GREEN))
        elif normalized in ("off", "offline"):
            bot_settings["ai_enabled"] = False
            save_bot_settings()
            await ctx.send(embed=emb("🤖 AI Disabled", "Passive AI responses are now **offline**.", C_RED))
        else:
            await ctx.send(embed=emb("❌ Invalid Option", "Use `!ai on`, `!ai online`, `!ai off`, or `!ai offline`.", C_RED))
        return

    ai_connected = await check_ollama_connected()
    ask_model = get_guild_ask_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
    roleplay_model = get_guild_roleplay_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL
    coding_model = get_guild_coding_model(ctx.guild.id) if ctx.guild else OLLAMA_MODEL

    ai_enabled = bot_settings.get("ai_enabled", True)
    ai_status = "Online" if ai_connected else "Offline"
    ai_status_emoji = "🟢" if (ai_connected and ai_enabled) else "🔴"
    passive_status = "Enabled" if ai_enabled else "**Disabled**"
    embed_color = C_BLUE if ai_enabled else C_RED

    embed = discord.Embed(title="🤖 AI Commands", color=embed_color)

    embed.add_field(
        name=f"{ai_status_emoji} Connection",
        value=f"Status: **{ai_status}** · Passive responses: {passive_status}",
        inline=False
    )

    embed.add_field(
        name="💬 !ask",
        value=(
            f"Ask the AI a question\n"
            f"Cost: **10 🪙**\n"
            f"Model: `{ask_model}`\n"
            f"Usage: `!ask <question>`"
        ),
        inline=False
    )

    embed.add_field(
        name="📖 !fanfic",
        value=(
            f"Generate a steamy fan fiction story on any topic\n"
            f"Cost: **20 🪙** · `!continue` for next chapter (10 🪙) · `!tldr` to summarize\n"
            f"Model: `{ask_model}`\n"
            f"Usage: `!fanfic <prompt>`"
        ),
        inline=False
    )

    embed.add_field(
        name="🎭 !roleplay",
        value=(
            f"Start an AI roleplay session\n"
            f"Cost: **50 🪙** · `!tldr` to summarize the last response\n"
            f"Model: `{roleplay_model}`\n"
            f"Usage: `!roleplay <character> [@user1 @user2 ...]`"
        ),
        inline=False
    )

    embed.add_field(
        name="🗺️ !rpg",
        value=(
            f"Start an interactive text adventure game\n"
            f"Cost: **50 🪙**\n"
            f"Model: `{roleplay_model}`\n"
            f"Usage: `!rpg [@user1 @user2 ...]`\n"
        ),
        inline=False
    )

    embed.add_field(
        name="🧩 !puzzle coding / riddle",
        value=(
            f"AI-generated puzzles\n"
            f"`!puzzle coding [easy|medium|hard|extreme] [@user …]` — figure out the code output · **10–50 🪙**\n"
            f"`!puzzle riddle [@user …]` — one-word riddle · **{PUZZLE_RIDDLE_REWARD} 🪙**\n"
            f"Model: `{coding_model}`\n"
            f"Only the creator can answer by default; mention users to invite them."
        ),
        inline=False
    )

    await send_ephemeral(ctx, embed=embed)


@bot.command(name="game", aliases=["games"])
async def cmd_game(ctx: commands.Context):
    embed = discord.Embed(title="🎮 Games & Gambling", color=C_BLUE)

    embed.add_field(
        name="💰 Gambling",
        value=(
            "`!flip <amount>` — 50/50 coinflip\n"
            "`!slots <amount>` — 3-reel slot machine with progressive jackpot\n"
            "`!scratchoff` — Daily lottery (3 attempts/day)\n"
            "`!blackjack <amount>` — Interactive blackjack (type `hit` / `stand`)"
        ),
        inline=False
    )

    embed.add_field(
        name="🎯 Competitive",
        value=(
            "`!hangman [@user1 @user2]` — Start hangman\n"
            "`!race @user1 [@user2 ...] [amount]` — Race against others (optional bet)\n"
            "`!ttt @user [amount]` — Tic-Tac-Toe (use `!m <1-9>`)\n"
            "`!c4 @user [amount]` — Connect 4 (use `!m <1-7>`)\n"
            "`!chess @user [amount]` — Correspondence chess (use `!move <e2e4>`)\n"
        ),
        inline=False
    )

    embed.add_field(
        name="🧩 Puzzles",
        value=(
            "`!puzzle coding [easy|medium|hard|extreme] [@user …]` — AI-generated coding puzzle\n"
            "Reward: **10–50 🪙** depending on difficulty.\n"
            f"`!puzzle riddle [@user …]` — one-word riddle · Reward: **{PUZZLE_RIDDLE_REWARD} 🪙**\n"
            "Only the creator can answer by default; mention users to invite them."
        ),
        inline=False
    )

    embed.add_field(
        name="🏁 Utility",
        value=(
            "`!stop` — Forfeit/stop active game"
        ),
        inline=False
    )

    await send_ephemeral(ctx, embed=embed)


PUZZLE_REWARDS = {
    "easy":   10,
    "medium": 20,
    "hard":   35,
    "extreme": 50,
}

PUZZLE_RIDDLE_REWARD = 25

PUZZLE_RIDDLE_PROMPT = (
    "You are a riddle generator. Your ONLY output must be a single raw JSON object — no markdown, no code fences, no prose before or after.\n"
    "\n"
    "Generate a creative riddle that satisfies ALL of these rules:\n"
    "\n"
    "WHAT MAKES A GOOD RIDDLE:\n"
    "  • The riddle should describe the answer indirectly — through what it does, how it behaves, or how it feels — NOT by literally listing its physical properties\n"
    "  • The answer should feel surprising and satisfying in hindsight: 'oh, of course!' — not 'well obviously, it said exactly what it is'\n"
    "  • Use unexpected angles, personification, or contradiction to obscure the answer\n"
    "\n"
    "HARD RULES:\n"
    "  • Do NOT generate math problems, arithmetic, number puzzles, trivia questions, or factual quiz questions\n"
    "  • Do NOT use these banned overused answers: echo, mirror, shadow, silence, time, fire, wind, darkness, light, water, breath, death, balloon\n"
    "  • Do NOT write a riddle that reads like a checklist of the answer's traits (shape + property + action = answer). That is not a riddle, it is a description.\n"
    "  • Every statement in the riddle must be UNIVERSALLY TRUE of the answer — no exceptions. Do not invent false constraints (e.g. 'I have no lock' for a door) to make the answer harder to guess. If a clue is only sometimes true, or sometimes false, remove it.\n"
    "  • A good riddle makes the solver think of something UNRELATED before the answer clicks. If someone could guess the answer from the second sentence alone, rewrite it.\n"
    "  • The answer must be a single common English word (no phrases, no numbers, no abbreviations)\n"
    "  • The answer must be unambiguous — there should be only one reasonable word that fits\n"
    "\n"
    "Output EXACTLY this JSON and nothing else:\n"
    "  {\"riddle\": \"<the riddle text>\", \"answer\": \"<single lowercase word>\"}\n"
    "\n"
    "Output ONLY the JSON object. Any text outside the JSON will break the parser."
)

PUZZLE_CODING_PROMPT = (
    "You are a coding puzzle generator. Your ONLY output must be a single raw JSON object — no markdown, no code fences, no prose before or after.\n"
    "\n"
    "STEP 1 — Write a self-contained code snippet that satisfies ALL of these:\n"
    "  • Uses only the standard library (no third-party imports)\n"
    "  • No input(), no random, no time-dependent values — output must be fully deterministic\n"
    "  • No unhandled exceptions of ANY kind — no AttributeError, TypeError, ZeroDivisionError, NameError, IndexError, KeyError, RecursionError, or any other exception that would terminate the program without being caught\n"
    "  • No infinite loops or unbounded recursion\n"
    "  • Must produce exactly ONE line of stdout output — the entire output must fit on a single line with no newline characters\n"
    "  • The puzzle's difficulty should come from surprising but VALID behavior — not from errors\n"
    "\n"
    "STEP 2 — Simulate a Python/JS/C interpreter in your head. Execute every line in order:\n"
    "  a) Track the value of every variable after each assignment\n"
    "  b) For every function call, trace what it returns\n"
    "  c) For every exception that could be raised — even inside try/except blocks — verify it is caught and handled\n"
    "  d) List only the lines that call print() (or printf/console.log). Write down exactly what each prints.\n"
    "  e) Ask: 'Is there any line that could raise an UNCAUGHT exception?' If yes → go back to STEP 1 and rewrite.\n"
    "  f) Ask: 'Is the stdout list from step (d) non-empty?' If empty → go back to STEP 1 and rewrite.\n"
    "  g) Ask: 'Does the stdout list from step (d) contain more than one line of output?' If yes → go back to STEP 1 and rewrite the snippet so it only prints once.\n"
    "\n"
    "STEP 3 — Output EXACTLY this JSON and nothing else:\n"
    "  {\"language\": \"<Python|JavaScript|C>\", "
    "\"code\": \"<snippet as a plain string — no backticks, no markdown, no code fences>\", "
    "\"answer\": \"<exact stdout>\"}\n"
    "\n"
    "CRITICAL: The 'code' field must be a valid JSON string value.\n"
    "  • Do NOT wrap it in backticks or markdown fences (no ```python, no ``` at all)\n"
    "  • Embed newlines as \\n and tabs as \\t\n"
    "  • Any double-quote character inside the code MUST be escaped as \\\". Example: s = \\\"hello\\\" not s = \"hello\"\n"
    "  • Prefer single quotes for string literals in the code where the language allows it (Python, JS) to avoid escaping\n"
    "\n"
    "Rules for the answer field:\n"
    "  • Copy character-for-character from your stdout list in STEP 2d\n"
    "  • Multiple printed lines are joined with a literal \\n in the JSON string\n"
    "  • No trailing newline (Python's print() newline is not part of the output string)\n"
    "  • For C: use standard Linux printf/puts behavior\n"
    "\n"
    "Output ONLY the JSON object. Any text outside the JSON will break the parser."
)

PUZZLE_DIFFICULTY_GUIDANCE = {
    "easy":   "Use Python or JavaScript only. Use a trivial snippet (e.g. basic arithmetic, string concat, simple loop). The output should be obvious to a beginner.",
    "medium": "Use Python or JavaScript only. Use a moderately tricky snippet involving type coercion, simple recursion, or list operations.",
    "hard":   "Use Python, JavaScript, or C. Use a tricky snippet involving closures, scoping, reference semantics, or unexpected operator behavior.",
    "extreme": "Use Python, JavaScript, or C. This must be brutally hard. The difficulty MUST come from actual algorithmic complexity or non-trivial computation — NOT from simple floating point quirks, basic type theory, or single-line edge cases. Required: the snippet must involve at least one of (a) a non-trivial algorithm (e.g. recursive descent, dynamic programming, bitwise computation, manual numeric base conversion, custom sort/reduce), (b) complex multi-step string manipulation or construction (e.g. encoding, interleaving, repeated transformations), or (c) a computation that requires tracing several steps of state mutation through data structures. The solver must actually work through the logic — not just recall a language quirk. The code must run to completion and print exactly one line.",
}


@bot.command(name="puzzle")
async def cmd_puzzle(ctx: commands.Context, *args):
    if await check_puzzle_channel(ctx):
        return

    # Parse args: subcommand, optional difficulty, optional @mentions (in any order after subcommand)
    subcommand = None
    difficulty = None
    invited_ids: set[int] = {ctx.author.id}

    pos_args = []
    for arg in args:
        # Mentions come through as <@123> or <@!123>
        if arg.startswith("<@") and arg.endswith(">"):
            uid_str = arg.strip("<@!>")
            if uid_str.isdigit():
                invited_ids.add(int(uid_str))
        else:
            pos_args.append(arg)

    if pos_args:
        subcommand = pos_args[0]
    if len(pos_args) > 1:
        difficulty = pos_args[1]

    if subcommand is None:
        embed = discord.Embed(title="🧩 Puzzle Commands", color=C_BLUE)
        embed.add_field(
            name="!puzzle coding [difficulty] [@user …]",
            value=(
                "AI generates a code snippet — figure out its output!\n"
                "**Difficulties:** `easy` (10 🪙) · `medium` (20 🪙) · `hard` (35 🪙) · `extreme` (50 🪙)\n"
                "Default difficulty: `medium`\n"
                "Only you can answer by default. Mention users to invite them too.\n"
                "Example: `!puzzle coding hard @Alice @Bob`"
            ),
            inline=False,
        )
        embed.add_field(
            name="!puzzle riddle [@user …]",
            value=(
                "AI generates a classic riddle — answer in one word!\n"
                f"Reward: **{PUZZLE_RIDDLE_REWARD} 🪙**\n"
                "Only you can answer by default. Mention users to invite them too.\n"
                "Example: `!puzzle riddle @Alice`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed)
        return

    if subcommand.lower() not in ("coding", "riddle"):
        await ctx.send(f"Unknown puzzle type `{subcommand}`. Try `!puzzle coding` or `!puzzle riddle`.")
        return

    uid = ctx.author.id
    if any(p["user_id"] == uid for p in active_puzzles.values()):
        await ctx.send(embed=emb("⚠️ Puzzle Active", "You already have a puzzle running! Solve it or use `!stop` to cancel.", C_GOLD))
        return

    cid = ctx.channel.id
    if cid in active_puzzles:
        await ctx.send(embed=emb("⚠️ Puzzle Active", "A puzzle is already running in this channel! Solve it first.", C_GOLD))
        return

    import re as _re

    # ── Riddle branch ──────────────────────────────────────────────────────────
    if subcommand.lower() == "riddle":
        reward = PUZZLE_RIDDLE_REWARD
        messages = [
            {"role": "system", "content": PUZZLE_RIDDLE_PROMPT},
            {"role": "user", "content": "Generate a riddle. Output the JSON object only."},
        ]
        guild_id = ctx.guild.id if ctx.guild else None
        coding_model = get_guild_coding_model(guild_id) if guild_id else OLLAMA_MODEL
        thinking_msg = await ctx.send(embed=emb("🧩 Generating riddle...", f"Reward: **{reward} 🪙**", C_BLUE))

        if not bot_settings.get("ai_enabled", True):
            await thinking_msg.edit(embed=emb("🤖 AI Offline", "Passive AI responses are currently disabled.", C_RED))
            return

        active_puzzles[cid] = {"generating": True, "user_id": uid, "reward": reward, "invited_ids": invited_ids}

        try:
            async with aiohttp.ClientSession() as session:
                if ollama_semaphore.locked():
                    await thinking_msg.edit(embed=emb("⏳ Queued", "Another AI request is running. Your riddle will generate next...", C_BLUE))
                async with ollama_semaphore:
                    if cid not in active_puzzles:
                        await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
                        return
                    await thinking_msg.edit(embed=emb("🧩 Generating riddle...", f"Reward: **{reward} 🪙**", C_BLUE))
                    typing_task = asyncio.create_task(keep_typing(ctx.channel))
                    try:
                        payload = {"model": coding_model, "messages": messages, "stream": False}
                        async with session.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload) as resp:
                            resp.raise_for_status()
                            data = await resp.json()
                            raw = data.get("message", {}).get("content", "")
                    finally:
                        typing_task.cancel()
        except aiohttp.ClientError as e:
            active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Ollama offline: {e}")
            await thinking_msg.edit(embed=emb("❌ AI Offline", "Could not connect to the AI.", C_RED))
            return

        if cid not in active_puzzles:
            await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
            return

        json_match = _re.search(r'\{.*\}', raw, _re.DOTALL)
        if not json_match:
            active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response (no JSON): {raw[:200]}")
            await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
            return
        try:
            puzzle_data = json.loads(json_match.group())
            riddle_text = str(puzzle_data["riddle"])
            answer = str(puzzle_data["answer"]).lower().strip()
        except (json.JSONDecodeError, KeyError) as e:
            active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response ({type(e).__name__}): {raw[:200]}")
            await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
            return

        if " " in answer or not answer.isalpha():
            active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI generated non-single-word answer: {answer[:200]}")
            await thinking_msg.edit(embed=emb("❌ Bad Riddle", "The AI generated a multi-word answer. Try again.", C_RED))
            return

        active_puzzles[cid] = {
            "answer": answer,
            "reward": reward,
            "user_id": uid,
            "invited_ids": invited_ids,
        }

        guests = invited_ids - {uid}
        if guests:
            invite_line = "\nInvited: " + " ".join(f"<@{i}>" for i in guests)
            footer = f"Only {ctx.author.display_name} and invited users can answer · Use !stop to cancel"
        else:
            invite_line = ""
            footer = f"Only {ctx.author.display_name} can answer · Use !stop to cancel"
        embed = discord.Embed(
            title="🧩 Riddle",
            description=f"{riddle_text}\n\nType the **one-word answer** to win **{reward} 🪙**!{invite_line}",
            color=C_GOLD,
        )
        embed.set_footer(text=footer)
        try:
            await ctx.send(embed=embed)
            await thinking_msg.delete()
        except discord.HTTPException as e:
            active_puzzles.pop(cid, None)
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Discord error sending riddle: {e}")
            await thinking_msg.edit(embed=emb("❌ Discord Error", "Failed to send the riddle. Please try again.", C_RED))
        return

    # ── Coding branch ──────────────────────────────────────────────────────────
    # Resolve difficulty
    difficulty = (difficulty or "medium").lower()
    if difficulty not in PUZZLE_REWARDS:
        await ctx.send(f"Unknown difficulty `{difficulty}`. Choose: {', '.join(PUZZLE_REWARDS)}")
        return

    reward = PUZZLE_REWARDS[difficulty]
    guidance = PUZZLE_DIFFICULTY_GUIDANCE[difficulty]
    system_prompt = PUZZLE_CODING_PROMPT + f"\n\nDIFFICULTY: {difficulty}. {guidance}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Generate a {difficulty} coding output puzzle. "
            "Follow STEP 1, STEP 2, and STEP 3 from the instructions. "
            "Mentally trace execution before writing the answer field. "
            "Output the JSON object only."
        )},
    ]

    guild_id = ctx.guild.id if ctx.guild else None
    coding_model = get_guild_coding_model(guild_id) if guild_id else OLLAMA_MODEL
    thinking_msg = await ctx.send(embed=emb("🧩 Generating puzzle...", f"Difficulty: **{difficulty}** · Reward: **{reward} 🪙**", C_BLUE))

    if not bot_settings.get("ai_enabled", True):
        await thinking_msg.edit(embed=emb("🤖 AI Offline", "Passive AI responses are currently disabled.", C_RED))
        return

    # Register immediately so !stop can cancel during generation
    active_puzzles[cid] = {"generating": True, "user_id": uid, "reward": reward, "invited_ids": invited_ids}

    try:
        async with aiohttp.ClientSession() as session:
            if ollama_semaphore.locked():
                await thinking_msg.edit(embed=emb("⏳ Queued", "Another AI request is running. Your puzzle will generate next...", C_BLUE))
            async with ollama_semaphore:
                if cid not in active_puzzles:
                    # Cancelled while waiting in queue
                    await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
                    return
                await thinking_msg.edit(embed=emb("🧩 Generating puzzle...", f"Difficulty: **{difficulty}** · Reward: **{reward} 🪙**", C_BLUE))
                typing_task = asyncio.create_task(keep_typing(ctx.channel))
                try:
                    payload = {"model": coding_model, "messages": messages, "stream": False}
                    async with session.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                        raw = data.get("message", {}).get("content", "")
                finally:
                    typing_task.cancel()
    except aiohttp.ClientError as e:
        active_puzzles.pop(cid, None)
        _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Ollama offline: {e}")
        await thinking_msg.edit(embed=emb("❌ AI Offline", "Could not connect to the AI.", C_RED))
        return

    if cid not in active_puzzles:
        # Cancelled after generation completed but before we parsed
        await thinking_msg.edit(embed=emb("🚫 Cancelled", "Puzzle generation was cancelled.", C_RED))
        return

    # Parse JSON from the response
    def _extract_puzzle_fields(text: str):
        """Try json.loads first, then fall back to per-field regex extraction."""
        json_match = _re.search(r'\{.*\}', text, _re.DOTALL)
        if not json_match:
            return None
        blob = json_match.group()
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            pass
        # Fallback: extract each field individually via regex.
        # language and answer are simple quoted strings; code is everything between
        # "code": " and the last closing quote before "answer" or end of object.
        lang_m = _re.search(r'"language"\s*:\s*"([^"]+)"', blob)
        ans_m  = _re.search(r'"answer"\s*:\s*"([^"]*)"', blob)
        # code spans from after `"code": "` to just before `", "answer"` or `"}`
        code_m = _re.search(r'"code"\s*:\s*"(.*?)"\s*(?:,\s*"answer"|,\s*"language"|\})', blob, _re.DOTALL)
        if lang_m and ans_m and code_m:
            return {
                "language": lang_m.group(1),
                "code":     code_m.group(1),
                "answer":   ans_m.group(1),
            }
        return None

    puzzle_data = _extract_puzzle_fields(raw)
    if puzzle_data is None:
        active_puzzles.pop(cid, None)
        _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response (no JSON): {raw[:200]}")
        await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
        return
    try:
        code_raw = puzzle_data["code"]
        # Strip markdown code fences if the model ignored the prompt instructions
        code_raw = _re.sub(r'^```[a-zA-Z]*\n?', '', code_raw.strip())
        code_raw = _re.sub(r'\n?```$', '', code_raw)
        code_snippet = code_raw.replace("\\n", "\n").replace("\\t", "\t")
        answer = str(puzzle_data["answer"])
        language = puzzle_data.get("language", "Unknown")
    except KeyError as e:
        active_puzzles.pop(cid, None)
        _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI malformed response ({type(e).__name__}): {raw[:200]}")
        await thinking_msg.edit(embed=emb("❌ Parse Error", "The AI returned an unexpected format. Try again.", C_RED))
        return

    if "\n" in answer:
        active_puzzles.pop(cid, None)
        _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"AI generated multi-line answer: {answer[:200]}")
        await thinking_msg.edit(embed=emb("❌ Bad Puzzle", "The AI generated a multi-line answer. Try again.", C_RED))
        return

    active_puzzles[cid] = {
        "answer": answer,
        "code_snippet": code_snippet,
        "reward": reward,
        "user_id": uid,
        "invited_ids": invited_ids,
    }

    guests = invited_ids - {uid}
    if guests:
        invite_line = "\nInvited: " + " ".join(f"<@{i}>" for i in guests)
        footer = f"Only {ctx.author.display_name} and invited users can answer · Use !stop to cancel"
    else:
        invite_line = ""
        footer = f"Only {ctx.author.display_name} can answer · Use !stop to cancel"
    embed = discord.Embed(
        title=f"🧩 Coding Puzzle — {difficulty.capitalize()} · {language}",
        description=f"What will the output of this code be?\n```{language.lower()}\n{code_snippet}\n```\nType the **exact output** to win **{reward} 🪙**!{invite_line}",
        color=C_GOLD,
    )
    embed.set_footer(text=footer)
    try:
        await ctx.send(embed=embed)
        await thinking_msg.delete()
    except discord.HTTPException as e:
        active_puzzles.pop(cid, None)
        _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Discord error sending puzzle: {e}")
        await thinking_msg.edit(embed=emb("❌ Discord Error", "Failed to send the puzzle. Please try again.", C_RED))


@bot.command(name="adminhelp", aliases=["helpadmin"])
async def cmd_adminhelp(ctx: commands.Context):
    if not can_manage_settings(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    admin_embed = discord.Embed(title="⚙️ Admin Commands", color=C_GOLD)
    admin_embed.add_field(name="🔧 Server Settings", inline=False, value=(
        "`!settings` — View current server settings\n"
        "`!settings ai-channels #ch... / clear` — Restrict AI commands to channels\n"
        "`!settings cmd-whitelist #ch... / clear` — Allow commands only in channels\n"
        "`!settings cmd-blacklist #ch... / clear` — Disallow commands in channels\n"
        "`!settings chess-channels #ch... / clear` — Restrict chess to channels\n"
        "`!settings shop <item> on|off` — Toggle shop items\n"
        "`!settings quote bypass on|off` — Allow searchquote in any channel (bypass restrictions)\n"
        "`!settings rule34 on|off / channels add|remove|list / ban <tag> / unban <tag> / banned` — rule34 config\n"
        "`!settings soundboard-ratelimit add|remove @user|<userid> / list` — Soundboard rate-limit list"
    ))
    admin_embed.add_field(name="🔍 Moderation", inline=False, value=(
        "`!audit` — Last 5 failed command attempts\n"
        "`!clearbot [n]` — Delete last n bot messages (default 50)\n"
        "`!clearall <n>` — Delete last n messages (any author)\n"
        "`!saved` — Show saved data (admin-only)"
    ))
    if is_admin(ctx):
        admin_embed.add_field(name="🪙 Economy", inline=False, value=(
            "`!admingive @user <amount>` — Add or remove coins from a user\n"
            "`!event <amount> [hours]` — Start a reaction event\n"
            "`!adminragebait @user [n]` — Force ragebait on user (default 5 messages)"
        ))
        admin_embed.add_field(name="🤖 AI", inline=False, value=(
            "`!model [name]` — View or change the AI model\n"
            "`!roleplaymodel [name]` — View or change the roleplay model\n"
            "`!codingmodel [name]` — View or change the coding puzzle model"
        ))
        admin_embed.add_field(name="⚙️ Config", inline=False, value=(
            "`!setprompt <prompt>` — Set a custom system prompt for this channel\n"
            "`!clearprompt` — Reset this channel's prompt to default\n"
            "`!godmode [user]` — Toggle free costs on/off (for yourself or a user)\n"
            "`!vramtext [text]` — View or set the vRAM display text in !stats"
        ))
        admin_embed.add_field(name="📢 Bot Control", inline=False, value=(
            "`!say <text>` — Make the bot repeat text in channel\n"
            "`!botinvitelink` — Display bot invite link\n"
            "`!invitelink` — Display server invite link\n"
            "`!restart` — Restart the bot process"
        ))
    await send_ephemeral(ctx, embed=admin_embed)


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Economy
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="daily")
async def cmd_daily(ctx: commands.Context):
    uid = ctx.author.id
    _ensure_user(uid)
    today = datetime.date.today().isoformat()
    user_data = economy["users"][str(uid)]
    if user_data.get("daily_date") == today:
        now = datetime.datetime.now()
        next_reset = datetime.datetime.combine(
            now.date() if now.hour < 5 else now.date() + datetime.timedelta(days=1),
            datetime.time(5, 0),
        )
        remaining = int((next_reset - now).total_seconds())
        hours, rem = divmod(remaining, 3600)
        minutes = rem // 60
        await ctx.send(embed=emb("⏳ Already Claimed", f"Resets at **5am** — come back in **{hours}h {minutes}m**.", C_GOLD))
        return
    add_balance(uid, 200)
    user_data["daily_date"] = today
    user_data["last_daily"] = time.time()
    save_economy()
    await ctx.send(embed=emb("🪙 Daily Reward", f"+200 🪙 claimed! Balance: **{get_balance(uid)} 🪙**", C_GREEN))


@bot.command(name="balance", aliases=["bal", "b", "!", "$"])
async def cmd_balance(ctx: commands.Context, target: discord.Member = None):
    target = target or ctx.author
    if bot.user and target.id == bot.user.id and ctx.guild:
        bal = get_guild_house_balance(ctx.guild.id)
        await ctx.send(embed=emb("🏦 House Pot", f"**{ctx.guild.name}**: {bal} 🪙", C_GOLD))
    else:
        bal = get_balance(target.id)
        await ctx.send(embed=emb("💰 Balance", f"**{target.display_name}**: {bal} 🪙", C_GREEN))


@bot.command(name="leaderboard", aliases=["leaderboards", "lb"])
async def cmd_leaderboard(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("Leaderboard is only available in servers.")
        return
    sorted_users = sorted(
        ((k, v) for k, v in economy["users"].items() if v["balance"] > 0),
        key=lambda x: x[1]["balance"], reverse=True
    )[:10]
    if not sorted_users:
        await ctx.send(embed=emb("🪙 Leaderboard", "No users yet.", C_GREEN))
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid_str, data) in enumerate(sorted_users):
        uid_int = int(uid_str)
        name = None
        member = await fetch_member(ctx.guild, uid_int)
        if member:
            name = member.display_name
        # Try global user lookup
        if name is None:
            try:
                user = await bot.fetch_user(uid_int)
                name = user.display_name
            except (discord.NotFound, discord.HTTPException):
                pass
        # Fallback name
        if name is None:
            name = f"User {uid_str}"
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} **{name}** — {data['balance']} 🪙")
    await ctx.send(embed=emb("🪙 Leaderboard", "\n".join(lines), C_GREEN))


@bot.command(name="pay", aliases=["give", "gift", "donate"])
async def cmd_pay(ctx: commands.Context, recipient: discord.Member = None, amount: str = None):
    if recipient is None or amount is None:
        await ctx.send("Usage: `!pay @user <amount>`")
        return
    if recipient.id == ctx.author.id:
        await ctx.send("You can't pay yourself.")
        return
    amount = await parse_amount(ctx, amount)
    if amount is None:
        return
    if not await shop_charge(ctx, ctx.author.id, amount):
        return
    if bot.user and recipient.id == bot.user.id and ctx.guild:
        add_guild_house(ctx.guild.id, amount)
        await ctx.send(embed=emb(
            "💸 Payment Sent",
            f"**{ctx.author.display_name}** paid **{amount} 🪙** to the house pot.\n"
            f"Your balance: **{get_balance(ctx.author.id)} 🪙**",
            C_GREEN,
        ))
        return
    add_balance(recipient.id, amount)
    await ctx.send(embed=emb(
        "💸 Payment Sent",
        f"**{ctx.author.display_name}** paid **{recipient.display_name}** {amount} 🪙\n"
        f"Your balance: **{get_balance(ctx.author.id)} 🪙**",
        C_GREEN,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Gambling
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="flip", aliases=["coinflip"])
async def cmd_flip(ctx: commands.Context, amount: str = None):
    if await check_game_channel(ctx, "Gambling"):
        return
    uid = ctx.author.id
    if amount is None:
        await ctx.send("Usage: `!flip <amount>`")
        return
    amount = await parse_amount(ctx, amount)
    if amount is None:
        return
    if not await shop_charge(ctx, uid, amount):
        return
    win = random.random() < 0.5
    if win:
        add_balance(uid, amount * 2)
        await ctx.send(embed=emb("🪙 Heads!", f"You won **{amount} 🪙**! Balance: {get_balance(uid)} 🪙", C_GREEN))
    else:
        await ctx.send(embed=emb("🪙 Tails!", f"You lost **{amount} 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))


# Mini Cactpot payout table
CACTPOT_PAYOUTS = {
    6: 10000, 7: 36, 8: 720, 9: 360, 10: 80, 11: 252, 12: 108, 13: 72, 14: 54, 15: 180,
    16: 72, 17: 180, 18: 119, 19: 36, 20: 306, 21: 1080, 22: 144, 23: 1800, 24: 3600
}

class MiniCactpotGame:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.grid = list(range(1, 10))
        random.shuffle(self.grid)
        self.revealed = set()
        # Reveal one random cell initially
        self.revealed.add(random.randint(0, 8))
        self.selections = []
        self.selected_line = None

    def get_grid_display(self):
        """Return a 3x3 grid display with numbers or letters A-I"""
        letters = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]
        lines = []
        for row in range(3):
            row_str = ""
            for col in range(3):
                idx = row * 3 + col
                if idx in self.revealed:
                    row_str += str(self.grid[idx]).rjust(2) + " "
                else:
                    row_str += f" {letters[idx]} "
            lines.append(row_str)
        return "\n".join(lines)

    def get_line_sum(self, line_type: str, line_idx: int) -> int:
        """Get sum of a line. Types: row, col, diag1, diag2"""
        cells = []
        if line_type == "row":
            cells = [line_idx * 3, line_idx * 3 + 1, line_idx * 3 + 2]
        elif line_type == "col":
            cells = [line_idx, line_idx + 3, line_idx + 6]
        elif line_type == "diag1":
            cells = [0, 4, 8]
        elif line_type == "diag2":
            cells = [2, 4, 6]
        return sum(self.grid[i] for i in cells)

    def calculate_payout(self, line_type: str, line_idx: int) -> int:
        total = self.get_line_sum(line_type, line_idx)
        return CACTPOT_PAYOUTS.get(total, 0)

def do_daily_reset():
    """Reset all users' daily reward and scratchoff counts at 5am."""
    today = datetime.date.today().isoformat()
    for user in economy["users"].values():
        user["daily_date"] = None
        user["scratch_used"] = 0
        user["scratch_date"] = today
    economy["last_daily_reset"] = today
    save_economy()
    logging.info(f"[DAILY] Reset daily reward and scratchoff counts for {today}")


# Store active games
@bot.command(name="scratchoff", aliases=["scratch"])
async def cmd_scratchoff(ctx: commands.Context):
    if await check_game_channel(ctx, "Gambling"):
        return
    uid = ctx.author.id
    _ensure_user(uid)

    # Check daily limit
    today = datetime.date.today().isoformat()
    user = economy["users"][str(uid)]
    if user.get("scratch_date") != today:
        user["scratch_date"] = today
        user["scratch_used"] = 0

    if user["scratch_used"] >= 3:
        save_economy()
        await ctx.send(embed=emb("🎰 Daily Limit", "You've used your **3** daily scratchoffs.\nCome back tomorrow!", C_GOLD))
        return

    user["scratch_used"] += 1
    save_economy()

    # Track full-day scratchoff streak for Gamblers role
    if user["scratch_used"] >= 3 and ctx.guild:
        gambler_streak[str(uid)] = today
        save_gambler_streak()
        await maybe_assign_gambler_role(ctx.guild, ctx.author, ctx.channel)

    # Generate daily goal seeded by uid + date (consistent if called twice)
    seed_str = f"{uid}{today}"
    seed_val = hash(seed_str) % (2**31)
    random.seed(seed_val)
    goal = random.choices(SCRATCH_SYMBOLS, k=4)

    # Reset seed and generate player's card
    random.seed()
    card = random.choices(SCRATCH_SYMBOLS, k=4)

    # Count how many card symbols match the goal at each position
    matches = sum(c == g for c, g in zip(card, goal))

    # Determine payout
    payout = 0
    match_text = ""
    if matches == 0:
        match_text = "❌ No matches."
    elif matches == 1:
        payout = 100
        match_text = "⭐ 1 Match! You won 100 🪙!"
    elif matches == 2:
        payout = 1000
        match_text = "🎉 2 Matches! You won 1,000 🪙!"
    elif matches == 3:
        payout = 10000
        match_text = "🏆 3 Matches! You won 10,000 🪙!"
    elif matches == 4:
        payout = 100000
        match_text = "💎 4 Matches! You won 100,000 🪙!"

    add_balance(uid, payout)

    # First-time message
    first_time = not user.get("scratchoff_seen_rewards", False)
    if first_time:
        user["scratchoff_seen_rewards"] = True
        save_economy()

    goal_str = " ".join(goal)
    card_str = " ".join(card)
    attempts_left = 3 - user["scratch_used"]

    embed = discord.Embed(title="🎫 Scratchoff", color=C_GREEN if payout > 0 else C_RED)
    embed.description = f"Daily Goal: {goal_str}\nYour Card:  {card_str}\n\n{match_text}\n\nAttempts left: {attempts_left}/3"

    if first_time:
        embed.add_field(name="📊 Payout Info", value="Use `!scratchoffrewards` to see all payouts!", inline=False)

    await ctx.send(embed=embed)

@bot.command(name="scratchoffrewards", aliases=["scratchrewards", "scratchoffreward", "scratchreward"])
async def cmd_scratchoff_rewards(ctx: commands.Context):
    embed = discord.Embed(title="🎫 Scratchoff Payouts", color=C_PURPLE)
    embed.description = "**Scratchoff** — Match symbols to your daily goal"

    table = "```\nMatches  Payout\n─────────────────\n"
    payouts = [
        ("0", "0 🪙"),
        ("1", "100 🪙"),
        ("2", "1,000 🪙"),
        ("3", "10,000 🪙"),
        ("4", "100,000 🪙"),
    ]

    for matches, payout in payouts:
        table += f"{matches}        {payout}\n"

    table += "─────────────────```"

    embed.add_field(name="Limit", value="**3 per day**", inline=False)
    await ctx.send(embed=embed)

def eval_slots(reels: list[str], bet: int) -> tuple[str, int]:
    """Returns (result_label, multiplier). Caller applies multiplier to bet."""
    a, b, c = reels
    cherry = "🍒"

    # Priority: evaluate highest payout first
    if a == b == c:
        sym = a
        if sym == "7️⃣":
            # Jackpot requires minimum bet of 25
            if bet < 25:
                return ("nothing", 0)
            return ("jackpot", 75)
        if sym == "🎰":
            return ("3bar", 15)
        if sym == "🔔":
            return ("3bell", 7)
        if sym == "🍋":
            return ("3lemon", 4)
        if sym == cherry:
            return ("3cherry", 3)

    # Cherry retention (only checked when no 3-of-a-kind)
    cherry_count = reels.count(cherry)
    if cherry_count >= 2:
        return ("2cherry", 2)
    if cherry_count == 1:
        return ("1cherry", 1)

    return ("nothing", 0)


@bot.command(name="slots", aliases=["slot"])
async def cmd_slots(ctx: commands.Context, amount: str = None):
    if await check_game_channel(ctx, "Gambling"):
        return
    global slot_jackpot
    uid = ctx.author.id
    _ensure_user(uid)

    # Track first-time usage
    user = economy["users"][str(uid)]
    first_time_slots = not user.get("slots_seen_rewards", False)
    if first_time_slots:
        user["slots_seen_rewards"] = True
        save_economy()

    if amount is None:
        embed = discord.Embed(title="🎰 Slots", color=C_GOLD)
        embed.description = "**Usage:** `!slots <amount>` — Minimum bet: **25 🪙**"
        embed.add_field(name="Jackpot", value=(
            "**7️⃣7️⃣7️⃣** (Jackpot) — 75x + Progressive Jackpot\n"
            "The Progressive Jackpot bonus scales to 4x at bet 1000 🪙 or above)*"
        ), inline=False)
        embed.add_field(name="Three of a Kind", value=(
            "**🎰🎰🎰** (3 Slots) — 15x\n"
            "**🔔🔔🔔** (3 Bells) — 7x\n"
            "**🍋🍋🍋** (3 Lemons) — 4x\n"
            "**🍒🍒🍒** (3 Cherries) — 3x"
        ), inline=False)
        embed.add_field(name="Cherry Bonuses", value=(
            "🍒 **Two Cherries** — 2x\n"
            "🍒 **One Cherry** — 1x (Money Back)"
        ), inline=False)
        embed.add_field(name="Other", value=(
            "❌ **No Match** — 0x (Lose bet)\n\n"
            f"**Progressive Jackpot:** Grows by 2% of every bet!\n"
            f"**Current Jackpot: {slot_jackpot:,} 🪙**"
        ), inline=False)
        await send_ephemeral(ctx, embed=embed)
        return

    amount = await parse_amount(ctx, amount, error_msg="")  # slots sends its own embed below
    if amount is None:
        await ctx.send(embed=emb("❌ Invalid Bet", "Please provide a positive amount.", C_RED))
        return

    if amount < 25:
        await ctx.send(embed=emb("❌ Minimum Bet", f"Minimum bet is **25 🪙**.", C_RED))
        return

    if not await shop_charge(ctx, uid, amount):
        return

    # Jackpot contribution (2% of every bet, rounded up)
    contrib = max(1, int(amount * SLOT_JACKPOT_CONTRIB))
    slot_jackpot += contrib
    save_jackpot(slot_jackpot)

    # Spin (or use rigged result)
    if uid in rigged_slots:
        rigged_slots.discard(uid)
        save_rigged_slots()
        reels = ["7️⃣", "7️⃣", "7️⃣"]
    else:
        if random.random() < SLOT_HOUSE_CHANCE: # 5% back to house
            symbol_types = list(dict.fromkeys(SLOT_REEL))  # unique symbols, preserving order
            reels = random.sample(symbol_types, 3)
        else: # normal
            reels = [random.choice(SLOT_REEL) for _ in range(3)]
    display = " | ".join(reels)
    label, mult = eval_slots(reels, amount)

    # Progressive jackpot: hit 3 sevens
    if label == "jackpot":
        # Calculate bonus multiplier: 1x at bet 25, scaling to 4x at bet 1000+
        bet_bonus = min(4.0, 1.0 + max(0, amount - 25) / 975.0 * 3.0)
        prize = int(slot_jackpot * bet_bonus)
        slot_jackpot = SLOT_JACKPOT_SEED
        save_jackpot(slot_jackpot)
        add_balance(uid, prize)
        desc = (f"{display}\n\n🏆 **You hit the Progressive Jackpot!**\n"
                f"**Won: {prize:,} 🪙** (Bet: {amount} 🪙 • Multiplier: {bet_bonus:.2f}x) | Balance: {get_balance(uid):,} 🪙\n"
                f"*(Jackpot reset to {SLOT_JACKPOT_SEED:,} 🪙)*")
        if first_time_slots:
            desc += "\n\n📊 Use `!slotsrewards` to see all payouts!"
        msg = await ctx.send(embed=emb("🎰 PROGRESSIVE JACKPOT!", desc, C_GOLD))
        try:
            await msg.pin()
        except Exception:
            pass
        # Ping Gamblers role if enabled
        if ctx.guild:
            cfg = get_guild_cfg(ctx.guild.id)
            if cfg.get("gambler_role_enabled", False):
                role = discord.utils.get(ctx.guild.roles, name="Gamblers")
                if role:
                    await ctx.send(f"{role.mention} 🎰 A progressive jackpot was just won!")
        return

    # Money Back (cherry retention)
    if label == "1cherry":
        add_balance(uid, amount)
        desc = (f"{display}\n\n🍒 **One Cherry — Money Back!**\n"
                f"Got **{amount} 🪙** back | Balance: {get_balance(uid):,} 🪙\n"
                f"Progressive Jackpot: **{slot_jackpot:,} 🪙**")
        if first_time_slots:
            desc += "\n\n📊 Use `!slotsrewards` to see all payouts!"
        await ctx.send(embed=emb("🎰 Money Back!", desc, C_GOLD))
        return

    if mult == 0:
        desc = (f"{display}\n\nYou lost **{amount} 🪙**. Balance: {get_balance(uid):,} 🪙\n"
                f"Progressive Jackpot: **{slot_jackpot:,} 🪙**")
        if first_time_slots:
            desc += "\n\n📊 Use `!slotsrewards` to see all payouts!"
        await ctx.send(embed=emb("🎰 No Win", desc, C_RED))
        return

    winnings = amount * mult
    add_balance(uid, winnings)

    result_labels = {
        "jackpot": f"7️⃣7️⃣7️⃣ — **{mult}x** (min bet 25, bonus scales to 4x at bet 1000+)",
        "3bar":    f"🎰🎰🎰 — **{mult}x**",
        "3bell":   f"🔔🔔🔔 — **{mult}x**",
        "3lemon":  f"🍋🍋🍋 — **{mult}x**",
        "3cherry": f"🍒🍒🍒 — **{mult}x**",
        "2cherry": f"Two Cherries — **{mult}x**",
    }
    desc_line = result_labels.get(label, f"**{mult}x**")

    desc = (f"{display}\n\n{desc_line}\n"
            f"Won **{winnings} 🪙** | Balance: {get_balance(uid):,} 🪙\n"
            f"Progressive Jackpot: **{slot_jackpot:,} 🪙**")
    if first_time_slots:
        desc += "\n\n📊 Use `!slotsrewards` to see all payouts!"
    await ctx.send(embed=emb("🎰 Winner!", desc, C_GREEN))


@bot.command(name="slotsrewards", aliases=["slotrewards", "slotreward"])
async def cmd_slots_rewards(ctx: commands.Context):
    embed = discord.Embed(title="🎰 Slots Payouts", color=C_PURPLE)
    embed.description = "**Spin 3 reels and match symbols for payouts!**\n\n"

    embed.add_field(name="Three of a Kind", value=
        "🌟 **7️⃣7️⃣7️⃣** (Jackpot) — 75x\n"
        "   *(Min bet 25 🪙, bonus scales to 4x at bet 1000+)*\n"
        "🌟 **🎰🎰🎰** (3 Slots) — 15x\n"
        "🌟 **🔔🔔🔔** (3 Bells) — 7x\n"
        "🌟 **🍋🍋🍋** (3 Lemons) — 4x\n"
        "🌟 **🍒🍒🍒** (3 Cherries) — 3x",
        inline=False)

    embed.add_field(name="Cherry Bonuses", value=
        "🍒 **Two Cherries** — 2x\n"
        "🍒 **One Cherry** — 1x (Money Back)",
        inline=False)

    jackpot = load_jackpot()
    embed.add_field(name="Other", value=
        "❌ **No Match** — 0x (Lose bet)\n\n"
        f"**Progressive Jackpot:** Grows by 2% of every bet!\n"
        f"**Current Jackpot: {jackpot:,} 🪙**",
        inline=False)

    await send_ephemeral(ctx, embed=embed)


@bot.command(name="rig", hidden=True)
async def cmd_rig(ctx: commands.Context, target_input: str = None):
    """Hidden admin-only command: rig the next slots spin to hit 7 7 7."""
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "Only bot admins can use this command.", C_RED))
        return

    # Determine target user
    uid = None
    target_name = "you"

    if ctx.message.mentions:
        # Priority: use mention if present
        target = ctx.message.mentions[0]
        uid = target.id
        target_name = target.display_name
    elif target_input:
        # Try to parse as user ID
        try:
            uid = int(target_input)
            target_name = f"user {uid}"
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Input", f"Could not parse `{target_input}` as a user ID.", C_RED))
            return
    else:
        # Default to command author
        uid = ctx.author.id
        target_name = "you"

    rigged_slots.add(uid)
    save_rigged_slots()
    await ctx.send(embed=emb(
        "🎰 Slots Rigged",
        f"{target_name.capitalize()}'s next `!slots` spin will hit the **7️⃣7️⃣7️⃣ jackpot**!",
        C_GOLD,
    ))


# ─────────────────────────────────────────────────────────────────────────
# Tic-Tac-Toe & Connect 4 Helpers
# ─────────────────────────────────────────────────────────────────────────

def build_ttt_display(game: dict) -> str:
    """Build a tic-tac-toe board display from game state."""
    NUM_EMOJIS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣"]
    board = game["board"]
    row1 = (board[0] or NUM_EMOJIS[0]) + (board[1] or NUM_EMOJIS[1]) + (board[2] or NUM_EMOJIS[2])
    row2 = (board[3] or NUM_EMOJIS[3]) + (board[4] or NUM_EMOJIS[4]) + (board[5] or NUM_EMOJIS[5])
    row3 = (board[6] or NUM_EMOJIS[6]) + (board[7] or NUM_EMOJIS[7]) + (board[8] or NUM_EMOJIS[8])
    return f"{row1}\n{row2}\n{row3}"


def build_c4_display(game: dict) -> str:
    """Build a connect 4 board display from game state."""
    COL_EMOJIS = "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣"
    board = game["board"]
    display = COL_EMOJIS + "\n"
    for row in board:
        display += "".join(cell or "⚫" for cell in row) + "\n"
    return display.strip()


def check_ttt_winner(board: list) -> str | None:
    """Check if there's a winner in tic-tac-toe. Return winning mark or None."""
    LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for a, b, c in LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


def is_ttt_stalemate(board: list) -> bool:
    """Return True if neither player can possibly win — forced draw."""
    LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    marks = {c for c in board if c is not None}
    if len(marks) < 2:
        return False
    for mark in marks:
        opponent = (marks - {mark}).pop()
        for line in LINES:
            if not any(board[i] == opponent for i in line):
                return False  # this mark can still win via this line
    return True


def check_c4_winner(board: list) -> str | None:
    """Check if there's a winner in connect 4. Return winning mark or None."""
    # Check horizontal
    for r in range(6):
        for c in range(4):
            if board[r][c] and board[r][c] == board[r][c+1] == board[r][c+2] == board[r][c+3]:
                return board[r][c]
    # Check vertical
    for r in range(3):
        for c in range(7):
            if board[r][c] and board[r][c] == board[r+1][c] == board[r+2][c] == board[r+3][c]:
                return board[r][c]
    # Check diagonal (↗)
    for r in range(3):
        for c in range(4):
            if board[r][c] and board[r][c] == board[r+1][c+1] == board[r+2][c+2] == board[r+3][c+3]:
                return board[r][c]
    # Check diagonal (↖)
    for r in range(3, 6):
        for c in range(4):
            if board[r][c] and board[r][c] == board[r-1][c+1] == board[r-2][c+2] == board[r-3][c+3]:
                return board[r][c]
    return None


def create_chess_board() -> list:
    """Create a standard chess board with pieces in starting position.
    Using Unicode chess symbols. Index 0 = rank 8 (black's side), Index 7 = rank 1 (white's side)."""
    return [
        ['♜', '♞', '♝', '♛', '♚', '♝', '♞', '♜'],  # Black pieces (rank 8)
        ['♟'] * 8,                                    # Black pawns (rank 7)
        [None] * 8,                                   # Rank 6
        [None] * 8,                                   # Rank 5
        [None] * 8,                                   # Rank 4
        [None] * 8,                                   # Rank 3
        ['♙'] * 8,                                    # White pawns (rank 2)
        ['♖', '♘', '♗', '♕', '♔', '♗', '♘', '♖'],  # White pieces (rank 1)
    ]


def build_chess_display(board: list, is_black_perspective: bool = False) -> str:
    """Build a chess board display from game state. Shows the board from the current player's perspective."""
    FILE_LABELS = ["a", "b", "c", "d", "e", "f", "g", "h"]
    RANK_LABELS = ["8", "7", "6", "5", "4", "3", "2", "1"]

    # Build file label line with dots on both sides
    files_to_show = list(reversed(FILE_LABELS)) if is_black_perspective else FILE_LABELS
    file_labels_str = " . ".join(files_to_show)
    file_line = f"... {file_labels_str} .\n"
    display = file_line

    # Determine which rows to iterate based on perspective
    if is_black_perspective:
        board_to_display = list(reversed(board))
        rank_labels_order = list(reversed(RANK_LABELS))
    else:
        board_to_display = board
        rank_labels_order = RANK_LABELS

    for rank_idx, row in enumerate(board_to_display):
        rank_num = rank_labels_order[rank_idx]

        line = f"{rank_num} "

        # Reverse row order for black perspective
        row_to_display = list(reversed(row)) if is_black_perspective else row

        for piece in row_to_display:
            if piece:
                line += piece + " "
            else:
                line += ".... "

        line += f"{rank_num}"
        display += line + "\n"

    display += file_line
    return display


def parse_chess_move(move_str: str) -> tuple[int, int, int, int] | None:
    """Parse chess move in algebraic notation (e.g., 'e2e4', 'e2 e4').
    Returns (from_row, from_col, to_row, to_col) or None if invalid."""
    move_str = move_str.lower().strip().replace(" ", "")

    if len(move_str) == 4:  # e2e4 format
        try:
            from_col = ord(move_str[0]) - ord('a')
            from_row = 8 - int(move_str[1])
            to_col = ord(move_str[2]) - ord('a')
            to_row = 8 - int(move_str[3])

            if all(0 <= x <= 7 for x in [from_row, from_col, to_row, to_col]):
                return (from_row, from_col, to_row, to_col)
        except (ValueError, IndexError):
            pass

    return None


def is_white_piece(piece: str | None) -> bool:
    """Check if piece is white (lowercase unicode symbols)."""
    if not piece:
        return False
    # White pieces: ♔♕♖♗♘♙
    return piece in '♔♕♖♗♘♙'


def is_black_piece(piece: str | None) -> bool:
    """Check if piece is black (uppercase unicode symbols)."""
    if not piece:
        return False
    # Black pieces: ♚♛♜♝♞♟
    return piece in '♚♛♜♝♞♟'


def is_valid_chess_move(board: list, from_r: int, from_c: int, to_r: int, to_c: int, is_white: bool) -> bool:
    """Basic chess move validation."""
    if not (0 <= from_r <= 7 and 0 <= from_c <= 7 and 0 <= to_r <= 7 and 0 <= to_c <= 7):
        return False

    piece = board[from_r][from_c]
    target = board[to_r][to_c]

    # Can't move empty square
    if not piece:
        return False

    # Check piece ownership
    if is_white and not is_white_piece(piece):
        return False
    if not is_white and not is_black_piece(piece):
        return False

    # Can't capture own pieces
    if target and ((is_white and is_white_piece(target)) or (not is_white and is_black_piece(target))):
        return False

    return True


def hangman_pot_msg(word: str, player_count: int) -> str:
    """Return a human-readable 'you would've won/split X' string for hangman game-over."""
    total = calculate_hangman_reward(word)
    per = total // player_count
    if player_count == 1:
        return f"💰 You would've won **{total} 🪙**"
    return f"💰 You would've split **{total} 🪙** ({per} each)"


def calculate_hangman_reward(word: str) -> int:
    """Calculate hangman reward based on word difficulty.

    Formula (AI-derived):
    - Base: 10 coins
    - Length Bonus: (word_length - 3) × 6
    - Unique Letters Bonus: unique_count × 3
    - Rare Letters Bonus: rare_count × 15

    Examples:
    - 5-letter average word (APPLE): ~25 coins
    - 10-letter hard word with rare letters: ~150 coins
    """
    ULTRA_RARE_LETTERS = {'q', 'x', 'z'}
    RARE_LETTERS = {'y', 'j', 'k', 'w', 'v'}

    word_lower = word.lower()
    base = 10
    length_bonus = max(0, (len(word) - 3)) * 6
    unique_count = len(set(word_lower))
    unique_bonus = unique_count * 3
    rare_count = sum(1 for c in word_lower if c in RARE_LETTERS)
    ultra_rare_count = sum(1 for c in word_lower if c in ULTRA_RARE_LETTERS)
    rare_bonus = (rare_count * 25) + (ultra_rare_count * 50)

    total = base + length_bonus + unique_bonus + rare_bonus
    return total


@bot.command(name="blackjack")
async def cmd_blackjack(ctx: commands.Context, amount: str = None):
    if await check_game_channel(ctx, "Gambling"):
        return
    uid = ctx.author.id
    if amount is None:
        await ctx.send("Usage: `!blackjack <amount>`")
        return
    amount = await parse_amount(ctx, amount)
    if amount is None:
        return
    if uid in active_blackjack_games:
        await ctx.send(embed=emb("🃏 Already Playing", "Just type `hit` or `stand`.", C_GOLD))
        return
    if not await shop_charge(ctx, uid, amount):
        return
    deck = new_deck()
    player = [draw_card(deck), draw_card(deck)]
    dealer = [draw_card(deck), draw_card(deck)]
    pval = hand_value(player)
    dval = hand_value(dealer)

    active_blackjack_games[uid] = {
        "amount": amount,
        "player_hand": player,
        "dealer_hand": dealer,
        "deck": deck,
        "channel_id": ctx.channel.id,
    }

    display = build_blackjack_display(player, dealer, pval, hide_dealer=True)

    # Natural blackjack
    if pval == 21:
        del active_blackjack_games[uid]
        full_display = build_blackjack_display(player, dealer, pval, hide_dealer=False, dval=dval)
        if dval == 21:
            add_balance(uid, amount)
            await ctx.send(embed=emb("🃏 Blackjack — Push", full_display + "\n\nBoth have Blackjack! Bet returned.", C_GOLD))
        else:
            winnings = int(amount * 2.5)
            add_balance(uid, winnings)
            await ctx.send(embed=emb("🃏 Blackjack!", full_display + f"\n\nYou win **{winnings} 🪙**! Balance: {get_balance(uid)} 🪙", C_GREEN))
        return

    await ctx.send(embed=emb("🃏 Blackjack", display + "\n\nType `hit` to draw a card or `stand` to hold.", C_BLUE))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Games
# ─────────────────────────────────────────────────────────────────────────────

def _distribute_hangman_rewards(cid: int, game: dict) -> str:
    """Distributes win rewards, deletes the game, and returns the reward message."""
    word = game["word"]
    total_reward = calculate_hangman_reward(word)
    active_players = list(game["active_players"])
    per_player = total_reward // len(active_players)
    remainder = total_reward % len(active_players)
    del active_hangman_games[cid]
    if len(active_players) == 1:
        msg = f"The word was `{word}`!\n\n"
    else:
        msg = f"The word was `{word}`!\n\n**Total: {total_reward} 🪙** split among {len(active_players)} players\n"
    names = game.get("player_names", {})
    for i, pid in enumerate(active_players):
        bonus = 1 if i < remainder else 0
        reward = per_player + bonus
        add_balance(pid, reward)
        name = names.get(pid, f"<@{pid}>")
        msg += f"**{name}**: +{reward} 🪙 | Balance: {get_balance(pid)} 🪙\n"
    return msg.strip()


async def _process_hangman_guess(channel: discord.abc.Messageable, author_id: int, cid: int, guess: str, author_name: str):
    """Shared hangman guess logic used by both `!guess`/`!g` command and free-text intercept."""
    game = active_hangman_games[cid]

    if author_id not in game["invited_players"]:
        return

    if not guess.isalpha():
        return  # silently ignore non-alpha free-text; cmd_guess shows an error

    name = author_name
    game["player_names"][author_id] = author_name

    # Track this player as active
    game["active_players"].add(author_id)

    # Full word guess
    if len(guess) > 1:
        if guess == game["word"]:
            game["last_move"] = f"{name} guessed the word! 🎉"
            game["guessed_letters"].update(game["word"])  # reveal full word for display
            reward_msg = _distribute_hangman_rewards(cid, game)
            await _edit_board(channel, game, emb("🎉 Correct!", build_hangman_display(game) + "\n\n" + reward_msg + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
        elif guess in game["guessed_words"]:
            game["last_move"] = f"{name} guessed `{guess}` ❌ (already tried)"
            await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_ORANGE))
        else:
            game["guessed_words"].add(guess)
            game["wrong_guesses"] += 1
            if game["wrong_guesses"] >= 6:
                word = game["word"]
                game["last_move"] = f"{name} guessed `{guess}` — Game over! The word was `{word}`"
                pot_msg = hangman_pot_msg(word, len(game["active_players"]))
                await _edit_board(channel, game, emb("💀 Game Over", build_hangman_display(game) + f"\n\nThe word was `{word}`.\n\n{pot_msg}\n\n**Last move:** {game['last_move']}", C_RED))
                del active_hangman_games[cid]
            else:
                game["last_move"] = f"{name} guessed `{guess}` ❌"
                await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_RED))
        return

    # Single letter guess
    if guess in game["guessed_letters"]:
        game["last_move"] = f"{name} guessed `{guess}` (already tried)"
        await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_ORANGE))
        return
    game["guessed_letters"].add(guess)
    if guess in game["word"]:
        if all(c in game["guessed_letters"] for c in game["word"]):
            game["last_move"] = f"{name} guessed `{guess}` ✅ — word complete! 🎉"
            reward_msg = _distribute_hangman_rewards(cid, game)
            await _edit_board(channel, game, emb("🎉 You Got It!", build_hangman_display(game) + "\n\n" + reward_msg + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
        else:
            game["last_move"] = f"{name} guessed `{guess}` ✅"
            await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_GREEN))
    else:
        game["wrong_guesses"] += 1
        if game["wrong_guesses"] >= 6:
            word = game["word"]
            game["last_move"] = f"{name} guessed `{guess}` — Game over! The word was `{word}`"
            pot_msg = hangman_pot_msg(word, len(game["active_players"]))
            await _edit_board(channel, game, emb("💀 Game Over", build_hangman_display(game) + f"\n\nThe word was `{word}`.\n\n{pot_msg}\n\n**Last move:** {game['last_move']}", C_RED))
            del active_hangman_games[cid]
        else:
            game["last_move"] = f"{name} guessed `{guess}` ❌"
            await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_ORANGE))


@bot.command(name="hangman", aliases=["hang", "hm"])
async def cmd_hangman(ctx: commands.Context, *args):
    if await check_game_channel(ctx):
        return
    cid = ctx.channel.id
    if cid in active_hangman_games:
        await ctx.send(embed=emb("🔤 Already Playing", "Just type your guess directly!", C_ORANGE))
        return
    word = random.choice(HANGMAN_WORDS)
    active_hangman_games[cid] = {
        "word": word,
        "guessed_letters": set(),
        "guessed_words": set(),  # Track full word guesses to prevent repeats
        "wrong_guesses": 0,
        "user_id": ctx.author.id,
        "active_players": {ctx.author.id},  # Track who's actively guessing (for rewards)
        "invited_players": {ctx.author.id},  # Only these users may guess
        "player_names": {ctx.author.id: ctx.author.display_name},
        "board_msg_id": None,
        "last_move": "Game started!",
    }
    # Invite flow for mentioned users
    invited_users = [m for m in ctx.message.mentions if m.id != ctx.author.id]
    if invited_users:
        confirmed = await _wait_for_confirmations(ctx, invited_users, title="📨 Hangman Invite")
        active_hangman_games[cid]["invited_players"].update(confirmed)
    game = active_hangman_games[cid]
    board_msg = await ctx.send(embed=emb("🔤 Hangman", build_hangman_display(game) + "\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** Game started!", C_ORANGE))
    game["board_msg_id"] = board_msg.id


@bot.command(name="guess", aliases=["g"])
async def cmd_guess(ctx: commands.Context, *, guess: str = None):
    cid = ctx.channel.id
    asyncio.create_task(_delete_after(ctx.message))
    if cid not in active_hangman_games:
        err = await ctx.send(embed=emb("🔤 No Game", "No active hangman game. Start one with `!hangman`.", C_ORANGE))
        asyncio.create_task(_delete_after(err))
        return
    if guess is None:
        err = await ctx.send(embed=emb("🔤 Hangman", "Usage: `!guess <letter or word>`", C_ORANGE))
        asyncio.create_task(_delete_after(err))
        return
    await _process_hangman_guess(ctx.channel, ctx.author.id, cid, guess.lower().strip(), ctx.author.display_name)


async def _send_game_board(ctx: commands.Context, game: dict, title: str,
                           board_text: str, player1_desc: str, player2_desc: str,
                           controls: str, amount: int) -> None:
    """Send the initial PVP board message and store its ID in game['board_msg_id']."""
    wager_info = f"\nWager: {amount} 🪙 each" if amount > 0 else ""
    desc = (
        f"{board_text}\n\n"
        f"{player1_desc} vs {player2_desc}{wager_info}\n"
        f"{ctx.author.mention}'s turn. {controls}\n\n"
        f"**Last move:** {game['last_move']}"
    )
    msg = await ctx.send(embed=emb(title, desc, C_BLUE))
    game["board_msg_id"] = msg.id


async def _setup_pvp_game(ctx, opponent, amount, invite_title):
    """Validates opponent, deducts wagers, waits for confirmation.
    Returns True if game should proceed; False if an error was already sent."""
    uid = ctx.author.id
    if opponent is None:
        await ctx.send(f"Usage: `!{ctx.invoked_with} @user [amount]`")
        return False
    if opponent.id == uid:
        await ctx.send(embed=emb("❌ Can't Invite Yourself", "Pick a different opponent.", C_RED))
        return False
    if amount < 0:
        await ctx.send("Amount must be positive.")
        return False
    if amount > 0:
        if not deduct_balance(uid, amount):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"You need {amount} 🪙. Balance: {get_balance(uid)} 🪙", C_RED))
            return False
        if not deduct_balance(opponent.id, amount):
            add_balance(uid, amount)  # refund challenger
            await ctx.send(embed=emb("💸 Insufficient Funds", f"{opponent.display_name} needs {amount} 🪙. Balance: {get_balance(opponent.id)} 🪙", C_RED))
            return False
    wager_text = f" for {amount} 🪙" if amount > 0 else ""
    confirmed = await _wait_for_confirmations(ctx, [opponent], title=f"{invite_title}{wager_text}")
    if not confirmed:
        if amount > 0:
            add_balance(uid, amount)
            add_balance(opponent.id, amount)
            msg = f"{opponent.display_name} didn't accept. Coins refunded ({amount} 🪙 each)."
        else:
            msg = f"{opponent.display_name} didn't accept."
        await ctx.send(embed=emb("❌ Invite Declined", msg, C_RED))
        return False
    return True


@bot.command(name="ttt")
async def cmd_ttt(ctx: commands.Context, opponent: discord.User = None, amount: int = 0):
    if await check_game_channel(ctx):
        return
    cid = ctx.channel.id
    uid = ctx.author.id
    if cid in active_ttt_games or cid in active_c4_games:
        await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
        return
    if not await _setup_pvp_game(ctx, opponent, amount, "📨 Tic-Tac-Toe Invite"):
        return
    active_ttt_games[cid] = {
        "board": [None]*9,
        "players": [uid, opponent.id],
        "marks": {uid: "❌", opponent.id: "⭕"},
        "current": uid,
        "amount": amount,
        "board_msg_id": None,
        "last_move": f"{ctx.author.display_name}'s turn",
    }
    await _send_game_board(ctx, active_ttt_games[cid], "🎮 Tic-Tac-Toe",
                           build_ttt_display(active_ttt_games[cid]),
                           f"{ctx.author.mention} (❌)", f"{opponent.mention} (⭕)",
                           "Use `!m <1-9>`", amount)


@bot.command(name="c4")
async def cmd_c4(ctx: commands.Context, opponent: discord.User = None, amount: int = 0):
    if await check_game_channel(ctx):
        return
    cid = ctx.channel.id
    uid = ctx.author.id
    if cid in active_ttt_games or cid in active_c4_games:
        await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
        return
    if not await _setup_pvp_game(ctx, opponent, amount, "📨 Connect 4 Invite"):
        return
    active_c4_games[cid] = {
        "board": [[None]*7 for _ in range(6)],
        "players": [uid, opponent.id],
        "marks": {uid: "🔴", opponent.id: "🟡"},
        "current": uid,
        "amount": amount,
        "board_msg_id": None,
        "last_move": f"{ctx.author.display_name}'s turn",
    }
    await _send_game_board(ctx, active_c4_games[cid], "🟡 Connect 4",
                           build_c4_display(active_c4_games[cid]),
                           f"{ctx.author.mention} (🔴)", f"{opponent.mention} (🟡)",
                           "Use `!m <1-7>`", amount)


@bot.command(name="chess")
async def cmd_chess(ctx: commands.Context, *args):
    # Special admin preview commands
    if args and args[0].lower() == "preview":
        if not is_admin(ctx):
            await ctx.send(embed=emb("❌ No Permission", "", C_RED))
            return
        preview_board = create_chess_board()
        await ctx.send(embed=emb("♟️ Chess Board Preview (White)", build_chess_display(preview_board, is_black_perspective=False), C_BLUE))
        return

    if args and args[0].lower() == "blackpreview":
        if not is_admin(ctx):
            await ctx.send(embed=emb("❌ No Permission", "", C_RED))
            return
        preview_board = create_chess_board()
        await ctx.send(embed=emb("♟️ Chess Board Preview (Black)", build_chess_display(preview_board, is_black_perspective=True), C_BLUE))
        return

    # Parse opponent and amount from args
    opponent = None
    amount = 0
    if ctx.message.mentions:
        opponent = ctx.message.mentions[0]
    if args:
        try:
            amount = int(args[-1])
        except (ValueError, IndexError):
            pass

    if await check_chess_channel(ctx):
        return

    cid = ctx.channel.id
    uid = ctx.author.id

    if cid in active_ttt_games or cid in active_c4_games or cid in active_chess_games:
        await ctx.send(embed=emb("❌ Game Active", "A game is already active in this channel.", C_RED))
        return

    if not await _setup_pvp_game(ctx, opponent, amount, "♟️ Chess Invite"):
        return

    active_chess_games[cid] = {
        "board": create_chess_board(),
        "players": [uid, opponent.id],  # [white, black]
        "current": uid,  # white moves first
        "moves": [],
        "amount": amount,
        "board_msg_id": None,
        "last_move": f"{ctx.author.display_name}'s turn (White)",
    }
    await _send_game_board(ctx, active_chess_games[cid], "♟️ Chess",
                           build_chess_display(active_chess_games[cid]["board"], is_black_perspective=False),
                           f"{ctx.author.mention} (White ♙)", f"{opponent.mention} (Black ♟)",
                           "Use `!move <e2e4>`", amount)
    save_chess_games()


@bot.command(name="move")
async def cmd_move_chess(ctx: commands.Context, *args):
    cid = ctx.channel.id
    uid = ctx.author.id

    asyncio.create_task(_delete_after(ctx.message))

    if cid not in active_chess_games:
        err = await ctx.send("No active chess game in this channel. Start one with `!chess @user [amount]`")
        asyncio.create_task(_delete_after(err))
        return

    game = active_chess_games[cid]
    if uid != game["current"]:
        opponent_id = game["current"]
        opponent = ctx.guild.get_member(opponent_id) if ctx.guild else None
        err = await ctx.send(embed=emb("⏳ Not Your Turn", f"Waiting for {opponent.mention if opponent else 'opponent'}.", C_GOLD))
        asyncio.create_task(_delete_after(err))
        return

    if not args:
        err = await ctx.send("Usage: `!move <e2e4>` or `!move e2 e4` (from square to square in algebraic notation)")
        asyncio.create_task(_delete_after(err))
        return

    move = " ".join(args)

    parsed = parse_chess_move(move)
    if not parsed:
        err = await ctx.send("Invalid move format. Use algebraic notation like `e2e4`")
        asyncio.create_task(_delete_after(err))
        return

    from_r, from_c, to_r, to_c = parsed
    is_white = uid == game["players"][0]

    if not is_valid_chess_move(game["board"], from_r, from_c, to_r, to_c, is_white):
        err = await ctx.send("Invalid move. The piece can't move there or it's not your piece.")
        asyncio.create_task(_delete_after(err))
        return

    # Make the move
    board = game["board"]
    piece = board[from_r][from_c]
    board[to_r][to_c] = piece
    board[from_r][from_c] = None

    move_notation = f"{chr(ord('a') + from_c)}{8 - from_r}{chr(ord('a') + to_c)}{8 - to_r}"
    game["moves"].append(move_notation)

    # Switch turns
    game["current"] = game["players"][1] if uid == game["players"][0] else game["players"][0]
    next_player = ctx.guild.get_member(game["current"]) if ctx.guild else None

    game["last_move"] = f"{ctx.author.display_name} played {move_notation}"
    save_chess_games()

    # Display from the next player's perspective
    is_black_perspective = game["current"] == game["players"][1]  # True if it's black's turn next
    await _edit_board(ctx.channel, game, emb("♟️ Chess", build_chess_display(board, is_black_perspective) + f"\n\n{next_player.mention if next_player else 'Next player'}'s turn. Use `!move <e2e4>`\n\n**Last move:** {game['last_move']}", C_BLUE))


@bot.command(name="m",)
async def cmd_move(ctx: commands.Context, pos: int = None):
    cid = ctx.channel.id
    uid = ctx.author.id
    name = ctx.author.display_name

    if cid in active_ttt_games:
        game = active_ttt_games[cid]
        asyncio.create_task(_delete_after(ctx.message))
        if uid != game["current"]:
            err = await ctx.send(embed=emb("⏳ Not Your Turn", f"Waiting for {ctx.guild.get_member(game['current']).mention if ctx.guild else 'opponent'}.", C_GOLD))
            asyncio.create_task(_delete_after(err))
            return
        if pos is None or not 1 <= pos <= 9:
            err = await ctx.send("Use `!m <1-9>` to place your mark.")
            asyncio.create_task(_delete_after(err))
            return
        idx = pos - 1
        if game["board"][idx] is not None:
            err = await ctx.send(embed=emb("❌ Taken", "That square is already taken.", C_RED))
            asyncio.create_task(_delete_after(err))
            return
        game["board"][idx] = game["marks"][uid]
        winner = check_ttt_winner(game["board"])
        if winner:
            winner_uid = [p for p in game["players"] if game["marks"][p] == winner][0]
            amount = game.get("amount", 0)
            winnings = amount * 2
            if winnings > 0:
                add_balance(winner_uid, winnings)
            winner_name = ctx.guild.get_member(winner_uid).display_name if ctx.guild else str(winner_uid)
            game["last_move"] = f"{name} played position {pos} — {winner_name} wins!" + (f" **+{winnings} 🪙**" if winnings > 0 else "")
            winner_mention = ctx.guild.get_member(winner_uid).mention if ctx.guild else str(winner_uid)
            await _edit_board(ctx.channel, game, emb("🎉 Tic-Tac-Toe Won!", build_ttt_display(game) + f"\n\n{winner_mention} wins!" + (f" **+{winnings} 🪙**" if winnings > 0 else "") + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
            del active_ttt_games[cid]
        elif all(c is not None for c in game["board"]) or is_ttt_stalemate(game["board"]):
            amount = game.get("amount", 0)
            if amount > 0:
                for player_uid in game["players"]:
                    add_balance(player_uid, amount)
            game["last_move"] = f"{name} played position {pos} — It's a draw!"
            draw_text = f"\n\nIt's a draw!" + (f" Each player gets {amount} 🪙 back." if amount > 0 else "")
            await _edit_board(ctx.channel, game, emb("🤝 Tic-Tac-Toe Draw", build_ttt_display(game) + draw_text + f"\n\n**Last move:** {game['last_move']}", C_GOLD))
            del active_ttt_games[cid]
        else:
            players = game["players"]
            game["current"] = players[1] if uid == players[0] else players[0]
            next_player = ctx.guild.get_member(game["current"]) if ctx.guild else None
            game["last_move"] = f"{name} played position {pos}"
            await _edit_board(ctx.channel, game, emb("🎮 Tic-Tac-Toe", build_ttt_display(game) + f"\n\n{next_player.mention if next_player else 'Next player'}'s turn. Use `!m <1-9>`\n\n**Last move:** {game['last_move']}", C_BLUE))

    elif cid in active_c4_games:
        game = active_c4_games[cid]
        asyncio.create_task(_delete_after(ctx.message))
        if uid != game["current"]:
            err = await ctx.send(embed=emb("⏳ Not Your Turn", f"Waiting for {ctx.guild.get_member(game['current']).mention if ctx.guild else 'opponent'}.", C_GOLD))
            asyncio.create_task(_delete_after(err))
            return
        if pos is None or not 1 <= pos <= 7:
            err = await ctx.send("Use `!m <1-7>` to drop a piece.")
            asyncio.create_task(_delete_after(err))
            return
        col = pos - 1
        row = next((r for r in range(5, -1, -1) if game["board"][r][col] is None), None)
        if row is None:
            err = await ctx.send(embed=emb("❌ Column Full", "That column is full.", C_RED))
            asyncio.create_task(_delete_after(err))
            return
        game["board"][row][col] = game["marks"][uid]
        winner = check_c4_winner(game["board"])
        if winner:
            winner_uid = [p for p in game["players"] if game["marks"][p] == winner][0]
            amount = game.get("amount", 0)
            winnings = amount * 2
            if winnings > 0:
                add_balance(winner_uid, winnings)
            winner_name = ctx.guild.get_member(winner_uid).display_name if ctx.guild else str(winner_uid)
            game["last_move"] = f"{name} dropped in column {pos} — {winner_name} wins!" + (f" **+{winnings} 🪙**" if winnings > 0 else "")
            winner_mention = ctx.guild.get_member(winner_uid).mention if ctx.guild else str(winner_uid)
            await _edit_board(ctx.channel, game, emb("🎉 Connect 4 Won!", build_c4_display(game) + f"\n\n{winner_mention} wins!" + (f" **+{winnings} 🪙**" if winnings > 0 else "") + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
            del active_c4_games[cid]
        elif all(game["board"][r][c] is not None for r in range(6) for c in range(7)):
            amount = game.get("amount", 0)
            if amount > 0:
                for player_uid in game["players"]:
                    add_balance(player_uid, amount)
            game["last_move"] = f"{name} dropped in column {pos} — It's a draw!"
            draw_text = f"\n\nIt's a draw!" + (f" Each player gets {amount} 🪙 back." if amount > 0 else "")
            await _edit_board(ctx.channel, game, emb("🤝 Connect 4 Draw", build_c4_display(game) + draw_text + f"\n\n**Last move:** {game['last_move']}", C_GOLD))
            del active_c4_games[cid]
        else:
            players = game["players"]
            game["current"] = players[1] if uid == players[0] else players[0]
            next_player = ctx.guild.get_member(game["current"]) if ctx.guild else None
            game["last_move"] = f"{name} dropped in column {pos}"
            await _edit_board(ctx.channel, game, emb("🟡 Connect 4", build_c4_display(game) + f"\n\n{next_player.mention if next_player else 'Next player'}'s turn. Use `!m <1-7>`\n\n**Last move:** {game['last_move']}", C_BLUE))

    else:
        err = await ctx.send(embed=emb("❌ No Game", "No active tic-tac-toe or connect 4 game in this channel.", C_GREY))
        asyncio.create_task(_delete_after(err))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — AI
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="ask")
async def cmd_ask(ctx: commands.Context, *, question: str = None):
    if await check_ai_channel(ctx):
        return
    if question is None:
        await ctx.send("Usage: `!ask <question>`")
        return
    if check_rate_limit(ctx.author.id):
        await ctx.send("⚠️ Slow down! Please wait a moment before sending another message.")
        return

    # Check cost
    if not await enforce_cost(ctx, "ask"):
        return

    # Gather last 10 channel messages (excluding the !ask command itself)
    history_lines = []
    async for msg in ctx.channel.history(limit=11):
        if msg.id == ctx.message.id:
            continue
        if len(history_lines) >= 10:
            break
        history_lines.append(f"[{msg.author.display_name}]: {msg.content[:200]}")
    history_lines.reverse()

    if history_lines:
        context_block = "\n".join(history_lines)
        system_prompt = (
            ASK_SYSTEM_PROMPT
            + "\n\nThe following is recent channel conversation. "
            "Only use it as context if it is directly relevant to the question — ignore it otherwise:\n"
            f"<context>\n{context_block}\n</context>"
        )
    else:
        system_prompt = ASK_SYSTEM_PROMPT

    guild_id = ctx.guild.id if ctx.guild else None
    if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
        thread = await ctx.message.create_thread(name=f"ask: {question[:80]}")
        await respond(thread, ctx.author.id, question, ctx.message, system_prompt=system_prompt, guild_id=guild_id, author_name=ctx.author.display_name)
    else:
        await respond(ctx.channel, ctx.author.id, question, ctx.message, system_prompt=system_prompt, guild_id=guild_id, author_name=ctx.author.display_name)


@bot.command(name="fanfic")
async def cmd_fanfic(ctx: commands.Context, *, prompt: str = None):
    if await check_ai_channel(ctx):
        return
    if prompt is None:
        await ctx.send("Usage: `!fanfic <prompt> [@user1 @user2 ...]` — e.g. `!fanfic Batman and Superman stuck in an elevator`")
        return
    if check_rate_limit(ctx.author.id):
        await ctx.send("⚠️ Slow down! Please wait a moment before sending another message.")
        return

    uid = ctx.author.id
    invited_users = [m for m in ctx.message.mentions if m.id != uid]
    clean_prompt = prompt
    for m in ctx.message.mentions:
        clean_prompt = clean_prompt.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
    clean_prompt = clean_prompt.strip()
    if not clean_prompt:
        await ctx.send("Usage: `!fanfic <prompt> [@user1 @user2 ...]` — e.g. `!fanfic Batman and Superman stuck in an elevator`")
        return

    if not await enforce_cost(ctx, "fanfic"):
        return

    guild_id = ctx.guild.id if ctx.guild else None
    if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
        thread = await ctx.message.create_thread(name=f"Fanfic: {clean_prompt[:75]}")
        fanfic_thread_ids.add(thread.id)

        confirmed_ids: set[int] = set()
        if invited_users:
            confirmed_ids = await _wait_for_confirmations(ctx, invited_users, title="📨 Fanfic Invite")
            for inv_uid in confirmed_ids:
                member = ctx.guild.get_member(inv_uid)
                if member:
                    try:
                        await thread.add_user(member)
                    except Exception:
                        pass
        fanfic_owners[thread.id] = {"owner_id": uid, "invited_ids": {uid} | confirmed_ids}

        await respond(thread, uid, clean_prompt, ctx.message, system_prompt=FANFIC_SYSTEM_PROMPT, guild_id=guild_id)
        save_fanfic_histories()
        await thread.send(embed=emb("📖 Continue?", "Use `!continue` for the next chapter · `!tldr` to summarize · `!invite @user` to add a co-author.", C_BLUE))
    else:
        fanfic_owners[ctx.channel.id] = {"owner_id": uid, "invited_ids": {uid}}
        await respond(ctx.channel, uid, clean_prompt, ctx.message, system_prompt=FANFIC_SYSTEM_PROMPT, guild_id=guild_id)


@bot.command(name="continue")
async def cmd_continue(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.Thread):
        await ctx.send(embed=emb("❌ Threads Only", "`!continue` only works inside a fanfic thread.", C_RED))
        return
    if check_rate_limit(ctx.author.id):
        await ctx.send("⚠️ Slow down! Please wait a moment before sending another message.")
        return

    uid = ctx.author.id
    guild_id = ctx.guild.id if ctx.guild else None
    history = channel_histories[ctx.channel.id]

    if not history:
        await ctx.send(embed=emb("❌ Nothing to Continue", "No fanfic found in this thread.", C_RED))
        return

    if not await enforce_cost(ctx, "continue"):
        return

    await respond(ctx.channel, uid, "Continue the story.", ctx.message, system_prompt=FANFIC_SYSTEM_PROMPT, guild_id=guild_id)
    save_fanfic_histories()


@bot.command(name="tldr")
async def cmd_tldr(ctx: commands.Context):
    if not isinstance(ctx.channel, discord.Thread):
        await ctx.send(embed=emb("❌ Threads Only", "`!tldr` only works inside a fanfic or roleplay thread.", C_RED))
        return

    uid = ctx.author.id
    guild_id = ctx.guild.id if ctx.guild else None
    last_text = None

    # Roleplay/RPG thread
    if uid in active_roleplays and active_roleplays[uid].get("channel_id") == ctx.channel.id:
        history_key = active_roleplays[uid].get("history_owner", uid)
        history = roleplay_histories.get(history_key, [])
        for entry in reversed(history):
            if entry["role"] == "assistant":
                last_text = entry["content"]
                break

    # Fanfic thread
    if last_text is None:
        history = channel_histories[ctx.channel.id]
        for entry in reversed(history):
            if entry["role"] == "assistant":
                last_text = entry["content"]
                break

    if not last_text:
        await ctx.send(embed=emb("❌ Nothing to Summarize", "No AI response found in this thread yet.", C_RED))
        return

    tldr_prompt = [
        {"role": "system", "content": "You are a concise summarizer. Summarize the following story excerpt in 2-3 sentences, capturing the key events and mood. Do not editorialize or add commentary — just summarize."},
        {"role": "user", "content": f"Summarize this:\n\n{last_text}"},
    ]

    placeholder = await ctx.channel.send("📝 Summarizing...")
    typing_task = asyncio.create_task(keep_typing(ctx.channel))
    try:
        async with aiohttp.ClientSession() as session:
            model = get_guild_ask_model(guild_id) if guild_id else OLLAMA_MODEL
            summary = await stream_ollama(session, tldr_prompt, placeholder, guild_id=guild_id, model=model)
        await finalize(placeholder, ctx.channel, f"**TL;DR:** {summary}")
    except aiohttp.ClientError:
        await placeholder.edit(content="", embed=emb("", "The AI is currently offline.", C_RED))
    except Exception as e:
        await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
    finally:
        typing_task.cancel()


async def _wait_for_confirmations(
    ctx: commands.Context,
    invited_users: list,
    title: str = "📨 Game Invite",
    timeout: float = 60.0,
) -> set:
    """Wait for invited users to react with ✅ within timeout. Returns set of confirmed user IDs."""
    if not invited_users:
        return set()
    invited_ids = {u.id for u in invited_users}
    mentions = " ".join(u.mention for u in invited_users)
    invite_msg = await ctx.send(embed=emb(
        title,
        f"{mentions}\n{ctx.author.mention} is inviting you. React ✅ within 60 seconds to join!",
        C_BLUE,
    ))
    await invite_msg.add_reaction("✅")

    def check(reaction, user):
        return (
            reaction.message.id == invite_msg.id
            and str(reaction.emoji) == "✅"
            and user.id in invited_ids
        )

    confirmed_ids: set = set()
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            _, user = await bot.wait_for("reaction_add", check=check, timeout=remaining)
            confirmed_ids.add(user.id)
            if confirmed_ids == invited_ids:
                break
        except asyncio.TimeoutError:
            break
    try:
        await invite_msg.delete()
    except Exception:
        pass
    return confirmed_ids


@bot.command(name="roleplay")
async def cmd_roleplay(ctx: commands.Context, *, character_prompt: str = None):
    if await check_ai_channel(ctx):
        return
    uid = ctx.author.id
    if character_prompt is None:
        await ctx.send("Usage: `!roleplay <character prompt> [@user1 @user2 ...]`")
        return

    # Parse mentions and clean prompt
    invited_users = [m for m in ctx.message.mentions if m.id != uid]
    clean_prompt = character_prompt
    for m in ctx.message.mentions:
        clean_prompt = clean_prompt.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
    clean_prompt = clean_prompt.strip()
    if not clean_prompt:
        await ctx.send("Usage: `!roleplay <character prompt> [@user1 @user2 ...]`")
        return

    if not await enforce_cost(ctx, "roleplay"):
        return

    # Create a thread to contain the roleplay
    guild_id = ctx.guild.id if ctx.guild else None
    if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
        thread = await ctx.message.create_thread(name=f"roleplay: {clean_prompt[:70]}")
        rp_channel_id = thread.id
    else:
        thread = None
        rp_channel_id = ctx.channel.id

    # Register host with participants set
    active_roleplays[uid] = {
        "character_prompt": clean_prompt,
        "channel_id": rp_channel_id,
        "guild_id": guild_id,
        "participants": {uid},
    }
    roleplay_histories[uid] = []
    save_roleplay_state()

    # Invite flow and confirmation
    if invited_users:
        confirmed_ids = await _wait_for_confirmations(ctx, invited_users, title="📨 Roleplay Invite")
        for inv_uid in confirmed_ids:
            if inv_uid not in active_roleplays:
                active_roleplays[inv_uid] = {
                    "character_prompt": clean_prompt,
                    "channel_id": rp_channel_id,
                    "guild_id": guild_id,
                    "history_owner": uid,
                }
        active_roleplays[uid]["participants"].update(confirmed_ids)

        # Show confirmation of who joined
        if confirmed_ids:
            confirmed_names = []
            for cid in confirmed_ids:
                member = ctx.guild.get_member(cid) if ctx.guild else None
                if member:
                    confirmed_names.append(member.display_name)
            joined_text = ", ".join(confirmed_names) if confirmed_names else f"{len(confirmed_ids)} user(s)"
            await ctx.send(embed=emb("✅ Joined", f"{joined_text} joined the roleplay!", C_GREEN))

    preview = clean_prompt[:100] + ("..." if len(clean_prompt) > 100 else "")
    dest = thread or ctx.channel
    await dest.send(embed=emb(
        "🎭 Roleplay Started",
        f"Responding as: *{preview}*\nType freely — no @mention needed. Use `!stop` to end.",
        C_BLUE,
    ))


@bot.command(name="rpg")
async def cmd_rpg(ctx: commands.Context):
    if await check_ai_channel(ctx):
        return
    uid = ctx.author.id

    # Parse mentions for multiplayer
    invited_users = [m for m in ctx.message.mentions if m.id != uid]

    if not await enforce_cost(ctx, "rpg"):
        return

    # Register host with participants set
    rpg_system_prompt = (
        "Purpose:\n"
        "To create an immersive, text-based role-playing game.\n"
        "To guide the player through a narrative driven by their choices.\n\n"
        "Function:\n"
        "Out-of-Game Communication: Respond to the player as \"GAL,\" which stands for \"Game AI Liaison.\" This helps distinguish between in-game and out-of-game communication.\n"
        "In-Game Communication: When interacting with NPCs, respond in character, maintaining their personality, motivations, and knowledge of the world. Simulate a natural conversation, responding to the player's input and driving the narrative forward.\n"
        "Worldbuilding: Construct a detailed and consistent game world, including lore, locations, and NPCs. There should be an engaging overarching main story that guides the player through the world.\n"
        "Character Development: Assist the player in creating and developing their character, providing opportunities for growth and customization.\n"
        "Narrative Progression: Present choices and challenges, advancing the story based on the player's decisions.\n"
        "Rule Enforcement: Adhere to the established rules and guidelines to maintain consistency.\n"
        "Sheet Management: Maintain and update character sheets, party sheets, and quest logs, and present them to the player upon request.\n"
        "Player Engagement: Incorporate elements such as puzzles, riddles, and mini-games to keep the player interested and challenged.\n"
        "Reward System: Implement a system of rewards, such as experience points, treasure, or special abilities, to motivate players and encourage exploration.\n\n"
        "Starting the Game:\n"
        "Must start with character creation.\n"
        "Genre Selection: Ask the player to choose the genre of the game (e.g., Fantasy, Sci-Fi, Historical).\n"
        "Character Naming: Ask the player to name their character.\n"
        "Character Details: Guide the player through a step-by-step process of creating their character, including:\n"
        "- Race: Selecting a race for the character, which will determine their abilities, limitations, and physical appearance.\n"
        "- Class: Choosing a class for the character, which will define their role, skills, and abilities.\n"
        "- Attributes: Assigning attribute scores (Strength, Dexterity, Constitution, Intelligence, Wisdom, and Charisma). Ask if the player would prefer to have scores chosen for them or to choose from a buy system.\n"
        "- Backstory: Developing a brief backstory for the character, which can be used to inform their motivations, relationships, and overall personality.\n"
        "- Starting Spells or Skills: List out potential starting spells or skills and let the player decide what they begin with.\n\n"
        "Game Sheets:\n"
        "Rule Sheet: A comprehensive document outlining the core rules and mechanics of the game.\n"
        "Character Sheet: Includes Character Name, Race, Class, Level, Experience (shown as Current XP/XP Needed), Ability Scores, and Inventory.\n"
        "Party Sheet: Lists all party members with Name, Gender, Race, Class, Level, Experience, and Inventory.\n"
        "Inventory Sheet: Lists Currently Equipped Items and all other items in inventory.\n"
        "Spell Sheet: Shows spell slots available and a list of spells/cantrips the character can cast.\n"
        "Skill Sheet: A list of skills and abilities the character possesses.\n"
        "Quest Sheets:\n"
        "- Main Quest: The overarching storyline, updated as the story progresses.\n"
        "- Current Mission: The specific task or goal the player is currently focused on (could be a sub-task, side quest, or current activity).\n"
        "- Current Location: The player's current location within the game world.\n"
        "Lore Sheets:\n"
        "- Lore Sheet - Characters: A compendium of significant NPCs encountered, including party members and pivotal characters, updated as the player interacts with new individuals.\n"
        "- Lore Sheet - World: An evolving catalog of locations visited or heard of, including geographical features, landmarks, and historical/cultural significance.\n"
        "- Lore Sheet - Races: An exhaustive enumeration of all known races within the game's universe, including unique characteristics, customs, and societal structures.\n\n"
        "Rule Adherence:\n"
        "At any time, the player may ask to see one of the Game Sheets, Quest Sheets or Lore Sheets. Search, update, and show the player the current updated sheet.\n"
        "Reference the Rule Sheet to ensure consistency in gameplay and world-building.\n"
        "Use the rules to guide decisions and resolve conflicts.\n"
        "Be prepared to adapt and modify the rules as needed to accommodate the evolving narrative.\n\n"
        "RULE SHEET:\n\n"
        "Core Rules:\n"
        "1. Character Creation: Six primary attributes (Strength, Dexterity, Constitution, Intelligence, Wisdom, Charisma) determine the character's abilities and limitations. Characters also have a race and class defining abilities and roleplaying potential. Characters begin at level 1 and gain XP through quests, defeating enemies, and overcoming challenges.\n"
        "2. Skill Progression: Characters have skills (Stealth, Perception, Persuasion, etc.) used to perform actions and overcome challenges. Skill checks use a d20 + skill modifier vs. a GM-set Difficulty Class (DC). Skill proficiency increases with experience and practice.\n"
        "3. Immersive Conversations: Conversations between players and NPCs are role-played, with the GM acting as NPCs. The GM responds directly to the player's input without repeating player statements.\n"
        "4. Player Agency: Players have significant control over their character's actions and decisions. Choices have consequences, both positive and negative.\n"
        "5. Open-Ended Prompts: The GM uses open-ended prompts to guide the narrative and provide opportunities for player choice. These are used to initiate new actions or scenarios, not during NPC conversations.\n"
        "6. Game Setting: The world is grounded in the specific genre chosen, rich and detailed with a variety of cultures, civilizations, and landscapes.\n"
        "7. Challenges and Consequences: The game presents challenges (combat, puzzles, moral dilemmas). Failure may result in negative consequences such as character death or loss of resources.\n"
        "8. Character Limitations: Characters have finite resources (health points, spell slots, inventory space) and must make strategic decisions about resource usage.\n"
        "9. Dice Rolls: Dice rolls determine outcomes of actions, attacks, skill checks, and ability checks. The GM handles all dice rolls internally and announces the result.\n"
        "10. Internal Dice Rolls: All dice rolls are handled internally by the GM using a random number generator. Players do not have direct control over outcomes.\n"
        "11. Inventory and Resources: Players have a limited inventory and must manage resources carefully. New items can be acquired through quests, exploration, and purchases.\n"
        "12. Health and Damage: Characters have health that decreases when taking damage. When health reaches zero, they are incapacitated or killed. Health recovers through rest, potions, or magical abilities. Different damage types (physical, magical, poison) affect characters differently.\n"
        "13. Mature Themes: The game may contain mature themes such as violence, death, and morally ambiguous choices.\n"
        "14. Day/Night Cycle: The game has a day/night cycle affecting gameplay and NPC behavior. Certain actions may be more difficult or dangerous at night.\n"
        "15. World Detailing: The GM provides detailed descriptions of settings, characters, and events. Players can explore the world and uncover secrets.\n"
        "16. NPC Reactions: NPCs react to the player's actions and choices, influenced by their personality, motivations, and the current situation. Players can build relationships with NPCs.\n"
        "17. Multiple Quest Lines: The game features multiple quest lines (main and side quests). Players can choose which quests to pursue. Completing quests rewards XP, treasure, and reputation.\n"
        "18. Consistent NPCs: NPCs have consistent personalities, motivations, and backstories. The GM tracks NPC information for a cohesive world. NPCs may change behavior based on player actions. Different types of relationships can develop (friendly to antagonistic to romantic), each developed organically.\n"
        "19. Character Leveling: As players gain XP, characters level up, granting new abilities, spells, and features.\n"
        "20. Diverse NPCs: The world is populated with a diverse cast of NPCs with unique names, personalities, motivations, and backstories.\n"
        "21. Combat System: Combat is turn-based with characters acting in initiative order. Attacks use a d20 + attack modifier vs. target's armor class. Damage is calculated based on weapon and armor class.\n"
        "22. Magic System: Spellcasters have a limited number of spell slots. Spell effects vary by spell level and caster ability.\n"
        "23. Skill Challenges: Skill challenges resolve non-combat situations (persuasion, stealth, investigation, crafting) using a d20 + skill modifier vs. a difficulty target.\n"
        "24. Main Story and Side Quests: There is a Main Overarching Story as the backbone of the adventure. Each party member that joins should have their own personal story that can be completed with the player."
    )

    # Create a thread to contain the RPG session
    guild_id = ctx.guild.id if ctx.guild else None
    if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
        thread = await ctx.message.create_thread(name=f"rpg: {ctx.author.display_name}'s adventure")
        rpg_channel_id = thread.id
    else:
        thread = None
        rpg_channel_id = ctx.channel.id

    active_roleplays[uid] = {
        "character_prompt": "RPG Adventure",
        "channel_id": rpg_channel_id,
        "guild_id": guild_id,
        "participants": {uid},
        "is_rpg": True,
        "system_prompt": rpg_system_prompt,
    }
    roleplay_histories[uid] = []
    save_roleplay_state()

    # Invite flow and confirmation
    if invited_users:
        confirmed_ids = await _wait_for_confirmations(ctx, invited_users, title="📨 RPG Adventure Invite")
        for inv_uid in confirmed_ids:
            if inv_uid not in active_roleplays:
                active_roleplays[inv_uid] = {
                    "character_prompt": "RPG Adventure",
                    "channel_id": rpg_channel_id,
                    "guild_id": guild_id,
                    "history_owner": uid,
                    "is_rpg": True,
                    "system_prompt": rpg_system_prompt,
                }
        active_roleplays[uid]["participants"].update(confirmed_ids)

        # Show confirmation of who joined
        if confirmed_ids:
            confirmed_names = []
            for cid in confirmed_ids:
                member = ctx.guild.get_member(cid) if ctx.guild else None
                if member:
                    confirmed_names.append(member.display_name)
            joined_text = ", ".join(confirmed_names) if confirmed_names else f"{len(confirmed_ids)} user(s)"
            await ctx.send(embed=emb("✅ Joined", f"{joined_text} joined the adventure!", C_GREEN))

    # Send initial AI message asking for character configuration
    dest = thread or ctx.channel
    placeholder = await dest.send("🗺️ Starting your adventure...")
    typing_task = asyncio.create_task(keep_typing(dest))

    try:
        async with aiohttp.ClientSession() as session:
            model = get_guild_roleplay_model(guild_id) if guild_id else OLLAMA_MODEL
            messages = [{"role": "system", "content": rpg_system_prompt}]
            full_response = await stream_ollama(session, messages, placeholder, model=model)

        # Add to history with a synthetic user turn so the conversation structure is valid
        roleplay_histories[uid].append({"role": "user", "content": "Begin the adventure."})
        roleplay_histories[uid].append({"role": "assistant", "content": full_response})
        save_roleplay_state()
        await finalize(placeholder, dest, full_response)
    except aiohttp.ClientError as e:
        _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Ollama offline: {e}")
        await placeholder.edit(content="", embed=emb("", "The AI is currently offline", C_RED))
    except Exception as e:
        _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"{type(e).__name__}: {e}")
        await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
    finally:
        typing_task.cancel()


@bot.command(name="race")
async def cmd_race(ctx: commands.Context, *args):
    if await check_game_channel(ctx):
        return
    cid = ctx.channel.id
    uid = ctx.author.id

    if cid in active_ttt_games or cid in active_c4_games or cid in active_race_games:
        await ctx.send(embed=emb("❌ Game Active", "Finish the current game first.", C_RED))
        return

    invited_users = [m for m in ctx.message.mentions if m.id != uid]
    if not invited_users:
        await ctx.send("Usage: `!race @user1 [@user2 ...] [amount]`")
        return

    # Parse optional amount (any numeric arg that isn't a mention)
    amount = 0
    for a in args:
        if not a.startswith("<@"):
            try:
                amount = int(a)
                if amount <= 0:
                    await ctx.send(embed=emb("❌ Invalid Amount", "Amount must be positive.", C_RED))
                    return
            except ValueError:
                pass

    all_players = [uid] + [u.id for u in invited_users]

    # Deduct bets from all players upfront
    paid = []
    if amount > 0:
        for player_uid in all_players:
            if not deduct_balance(player_uid, amount):
                for refund_uid in paid:
                    add_balance(refund_uid, amount)
                member = ctx.guild.get_member(player_uid) if ctx.guild else None
                name = member.display_name if member else str(player_uid)
                await ctx.send(embed=emb("💸 Insufficient Funds", f"**{name}** can't cover the **{amount} 🪙** bet.", C_RED))
                return
            paid.append(player_uid)

    # Skip confirmation if no bet
    if amount == 0:
        confirmed_ids = set(u.id for u in invited_users)
    else:
        confirmed_ids = await _wait_for_confirmations(ctx, invited_users, title="🏇 Race Invite")

    # Refund anyone who didn't confirm
    declined = set(u.id for u in invited_users) - confirmed_ids
    if amount > 0:
        for d_uid in declined:
            add_balance(d_uid, amount)

    if not confirmed_ids:
        if amount > 0:
            add_balance(uid, amount)
            msg = f"Race cancelled — no one accepted the invite. Coins refunded ({amount} 🪙)."
        else:
            msg = "Race cancelled — no one accepted the invite."
        await ctx.send(embed=emb("❌ No One Joined", msg, C_RED))
        return

    # Build final player list (host + confirmed)
    final_players = [uid] + list(confirmed_ids)

    # Build names map using known member objects where available
    names = {}
    # Host is always ctx.author
    names[uid] = ctx.author.display_name
    # Confirmed players come from invited_users
    for player_uid in confirmed_ids:
        # Find the member from invited_users
        member = next((u for u in invited_users if u.id == player_uid), None)
        if member:
            names[player_uid] = member.display_name
        else:
            # Fallback to lookup (shouldn't happen)
            member = ctx.guild.get_member(player_uid) if ctx.guild else None
            names[player_uid] = member.display_name if member else str(player_uid)

    active_race_games[cid] = {
        "players": final_players,
        "names": names,
        "positions": {p: 0 for p in final_players},
        "amount": amount,
    }

    board = _render_race(active_race_games[cid])
    race_msg = await ctx.send(embed=emb("🏇 Race Starting!", board, C_ORANGE))

    asyncio.create_task(_run_race(ctx.channel, cid, race_msg))


@bot.command(name="invite")
async def cmd_invite_activity(ctx: commands.Context):
    uid = ctx.author.id
    cid = ctx.channel.id

    invited_users = [m for m in ctx.message.mentions if m.id != uid]
    if not invited_users:
        await ctx.send(embed=emb("❌ Usage", "Usage: `!invite @user1 [@user2 ...]`", C_RED))
        return

    # Determine activity type for this channel
    is_rp_host = (
        uid in active_roleplays
        and "history_owner" not in active_roleplays[uid]
        and active_roleplays[uid].get("channel_id") == cid
    )
    is_fanfic_host = cid in fanfic_thread_ids and fanfic_owners.get(cid, {}).get("owner_id") == uid
    is_puzzle_host = cid in active_puzzles and active_puzzles[cid].get("user_id") == uid

    if not (is_rp_host or is_fanfic_host or is_puzzle_host):
        await ctx.send(embed=emb(
            "❌ No Active Activity",
            "You must be the host of an active roleplay, RPG, fanfic, or puzzle in this channel to invite others.",
            C_RED,
        ))
        return

    if is_rp_host:
        activity_label = "RPG" if active_roleplays[uid].get("is_rpg") else "Roleplay"
    elif is_fanfic_host:
        activity_label = "Fanfic"
    else:
        activity_label = "Puzzle"

    confirmed_ids = await _wait_for_confirmations(ctx, invited_users, title=f"📨 {activity_label} Invite")
    if not confirmed_ids:
        await ctx.send(embed=emb("📨 No Response", "No one accepted the invite.", C_BLUE))
        return

    joined_names = []
    skipped_names = []
    for inv_uid in confirmed_ids:
        member = ctx.guild.get_member(inv_uid) if ctx.guild else None
        # Check if user is already in an activity in this channel/thread
        already_in_rp = inv_uid in active_roleplays and active_roleplays[inv_uid].get("channel_id") == cid
        already_in_fanfic = cid in fanfic_owners and inv_uid in fanfic_owners[cid]["invited_ids"]
        already_in_puzzle = cid in active_puzzles and inv_uid in active_puzzles[cid].get("invited_ids", set())
        if already_in_rp or already_in_fanfic or already_in_puzzle:
            if member:
                skipped_names.append(member.display_name)
            continue
        if is_rp_host:
            rp = active_roleplays[uid]
            active_roleplays[inv_uid] = {
                "character_prompt": rp["character_prompt"],
                "channel_id": rp["channel_id"],
                "guild_id": rp.get("guild_id"),
                "history_owner": uid,
                **({"is_rpg": True, "system_prompt": rp["system_prompt"]} if rp.get("is_rpg") else {}),
            }
            active_roleplays[uid]["participants"].add(inv_uid)
            save_roleplay_state()
            # Add the participant to the thread so they can see and send messages
            if member and isinstance(ctx.channel, discord.Thread):
                try:
                    await ctx.channel.add_user(member)
                except Exception:
                    pass
        elif is_fanfic_host:
            fanfic_owners[cid]["invited_ids"].add(inv_uid)
            if member:
                try:
                    await ctx.channel.add_user(member)
                except Exception:
                    pass
            save_fanfic_histories()
        elif is_puzzle_host:
            active_puzzles[cid].setdefault("invited_ids", set()).add(inv_uid)

        if member:
            joined_names.append(member.display_name)

    if skipped_names:
        await ctx.send(embed=emb(
            "⚠️ Already Active",
            f"{', '.join(skipped_names)} already {'has' if len(skipped_names) == 1 else 'have'} an active activity in this channel.",
            C_GOLD,
        ))
    if joined_names:
        await ctx.send(embed=emb("✅ Joined", f"{', '.join(joined_names)} joined the {activity_label.lower()}!", C_GREEN))
    elif not skipped_names:
        await ctx.send(embed=emb("📨 No Response", "No one accepted the invite.", C_BLUE))


@bot.command(name="stop", aliases=["quit", "forfeit", "q"])
async def cmd_stop(ctx: commands.Context):
    uid = ctx.author.id
    cid = ctx.channel.id
    stopped = []

    if cid in active_puzzles and (active_puzzles[cid]["user_id"] == uid or is_admin(ctx)):
        puzzle = active_puzzles.pop(cid)
        if puzzle.get("generating"):
            stopped.append("🧩 Puzzle (cancelled during generation)")
        else:
            stopped.append(f"🧩 Puzzle (answer was `{puzzle['answer']}`)")

    if uid in active_roleplays:
        rp = active_roleplays[uid]
        history_owner = rp.get("history_owner", uid)
        if "history_owner" in rp:
            # Participant leaving the group
            del active_roleplays[uid]
            if history_owner in active_roleplays:
                active_roleplays[history_owner]["participants"].discard(uid)
            save_roleplay_state()
            stopped.append("🎭 Roleplay (left group)")
        else:
            # Host stopping — remove all participants and shared history
            for pid in list(rp.get("participants", {uid})):
                active_roleplays.pop(pid, None)
            roleplay_histories.pop(uid, None)
            save_roleplay_state()
            stopped.append("🎭 Roleplay")

    if uid in active_blackjack_games:
        amount = active_blackjack_games[uid]["amount"]
        del active_blackjack_games[uid]
        stopped.append(f"🃏 Blackjack (forfeited {amount} 🪙)")

    if cid in active_hangman_games and active_hangman_games[cid]["user_id"] == uid:
        game = active_hangman_games[cid]
        word = game["word"]
        game["last_move"] = f"{ctx.author.display_name} forfeited. The word was `{word}`"
        asyncio.create_task(_edit_board(ctx.channel, game, emb("🏳️ Hangman Forfeited", build_hangman_display(game) + f"\n\nThe word was `{word}`.\n\n**Last move:** {game['last_move']}", C_RED)))
        del active_hangman_games[cid]
        stopped.append(f"🔤 Hangman (the word was `{word}`)")

    if cid in active_ttt_games and uid in active_ttt_games[cid]["players"]:
        game = active_ttt_games[cid]
        amount = game.get("amount", 0)
        opponent_uid = [p for p in game["players"] if p != uid][0]
        if amount > 0:
            winnings = amount * 2
            add_balance(opponent_uid, winnings)
            game["last_move"] = f"{ctx.author.display_name} forfeited — opponent wins {winnings} 🪙"
            stopped.append(f"🎮 Tic-Tac-Toe (forfeited, opponent wins {winnings} 🪙)")
        else:
            game["last_move"] = f"{ctx.author.display_name} forfeited"
            stopped.append("🎮 Tic-Tac-Toe (forfeited)")
        asyncio.create_task(_edit_board(ctx.channel, game, emb("🏳️ Tic-Tac-Toe Forfeited", build_ttt_display(game) + f"\n\n**Last move:** {game['last_move']}", C_RED)))
        del active_ttt_games[cid]

    if cid in active_c4_games and uid in active_c4_games[cid]["players"]:
        game = active_c4_games[cid]
        amount = game.get("amount", 0)
        opponent_uid = [p for p in game["players"] if p != uid][0]
        if amount > 0:
            winnings = amount * 2
            add_balance(opponent_uid, winnings)
            game["last_move"] = f"{ctx.author.display_name} forfeited — opponent wins {winnings} 🪙"
            stopped.append(f"🟡 Connect 4 (forfeited, opponent wins {winnings} 🪙)")
        else:
            game["last_move"] = f"{ctx.author.display_name} forfeited"
            stopped.append("🟡 Connect 4 (forfeited)")
        asyncio.create_task(_edit_board(ctx.channel, game, emb("🏳️ Connect 4 Forfeited", build_c4_display(game) + f"\n\n**Last move:** {game['last_move']}", C_RED)))
        del active_c4_games[cid]

    if cid in active_chess_games and uid in active_chess_games[cid]["players"]:
        game = active_chess_games[cid]
        amount = game.get("amount", 0)
        opponent_uid = [p for p in game["players"] if p != uid][0]
        is_black = uid == game["players"][1]
        if amount > 0:
            winnings = amount * 2
            add_balance(opponent_uid, winnings)
            game["last_move"] = f"{ctx.author.display_name} forfeited — opponent wins {winnings} 🪙"
            stopped.append(f"♟️ Chess (forfeited, opponent wins {winnings} 🪙)")
        else:
            game["last_move"] = f"{ctx.author.display_name} forfeited"
            stopped.append("♟️ Chess (forfeited)")
        asyncio.create_task(_edit_board(ctx.channel, game, emb("🏳️ Chess Forfeited", build_chess_display(game["board"], is_black_perspective=is_black) + f"\n\n**Last move:** {game['last_move']}", C_RED)))
        del active_chess_games[cid]

    if cid in active_race_games and uid in active_race_games[cid]["players"]:
        game = active_race_games[cid]
        amount = game.get("amount", 0)
        opponents = [p for p in game["players"] if p != uid]
        del active_race_games[cid]
        if amount > 0 and opponents:
            share = amount * len(game["players"]) // len(opponents)
            for opp in opponents:
                add_balance(opp, share)
            stopped.append(f"🏇 Race (forfeited, opponent(s) win {share} 🪙 each)")
        else:
            stopped.append("🏇 Race (forfeited)")

    if not stopped:
        await ctx.send(embed=emb("⏹️ Nothing to Stop", "No active game or roleplay.", C_GREY))
        return

    save_chess_games()
    await ctx.send(embed=emb("⏹️ Stopped", "\n".join(stopped), C_GREY))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Shop
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="shop", aliases=["store"])
async def cmd_shop(ctx: commands.Context, subcommand: str = None, *args):
    if ctx.guild:
        cfg = get_guild_cfg(ctx.guild.id)
        lottery_channel_id = cfg.get("lottery_channel")
        if lottery_channel_id and ctx.channel.id == lottery_channel_id:
            await _wrong_channel_reply(ctx, "Shop commands are not allowed in the lottery channel.")
            return
    uid = ctx.author.id

    if subcommand is None:
        _si = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}
        sections = {}

        # Nicknames (sorted by cost)
        if _si.get("nickname", True):
            nickname_items = [
                (5000, "`!shop nickname <new_name>` — Change your own nickname — **5,000 🪙**"),
                (2000, "`!shop removenickname` — Remove your own nickname — **2,000 🪙**"),
                (10000, "`!shop nickname @user <new_name>` — Nickname user — **10,000 🪙**"),
            ]
            nickname_items.sort(key=lambda x: x[0])
            sections["🎭 Nicknames"] = [item[1] for item in nickname_items]

        # Roles (sorted by cost)
        role_items = []
        if _si.get("removerole", True):
            role_items.append((2000, "`!shop removerole <name>` — Delete a bot-created role — **2,000 🪙**"))
        if _si.get("role", True):
            role_items.append((10000, "`!shop role @user <name> <hex>` — Create a custom colored role for a user — **10,000 🪙**"))
        if _si.get("roleup", True):
            role_items.append((20000, "`!shop roleup <role name>` — Move a bot-created role up one position — **20,000 🪙**"))
        if _si.get("roledown", True):
            role_items.append((20000, "`!shop roledown <role name>` — Move a bot-created role down one position — **20,000 🪙**"))
        if role_items:
            role_items.sort(key=lambda x: x[0])
            sections["👑 Roles"] = [item[1] for item in role_items]

        # Channels (sorted by cost)
        channel_items = []
        if _si.get("channel", True):
            channel_items.append((20000, "`!shop channel <name>` — Create a new text channel — **20,000 🪙**"))
        if _si.get("channel", True):
            channel_items.append((20000, "`!shop removechannel <name>` — Delete a bot-created channel — **20,000 🪙**"))
        if channel_items:
            channel_items.sort(key=lambda x: x[0])
            sections["📢 Channels"] = [item[1] for item in channel_items]

        # Fun & Social (sorted by cost)
        fun_items = [
            (500, "`!shop insurance` — Protect yourself for 24 hours — **500 🪙**"),
            (1000, "`!shop simp @user` — Make a user simp for you — **1,000 🪙**"),
            (1500, "`!shop mock @user` — Mock someone's next 5 messages — **1,500 🪙**"),
            (2000, "`!shop rolecolor <role name> <color>` — Change a role's color — **2,000 🪙**"),
        ]
        if _si.get("ragebait", True):
            fun_items.append((2500, "`!shop ragebait @user [topic]` — Ragebait for 5 messages — **2,500 🪙**"))
        fun_items.append((5000, "`!shop mute @user` — Server mute for 5 minutes — **5,000 🪙**"))
        fun_items.append((10000, "`!shop curse @user` — Curse someone's messages for 5 messages — **10,000 🪙**"))
        fun_items.sort(key=lambda x: x[0])
        sections["🎉 Fun & Social"] = [item[1] for item in fun_items]

        if not sections:
            await send_ephemeral(ctx, embed=emb("🛒 Shop", "No shop items are currently available.", C_PURPLE))
            return

        desc = "\n\n".join(f"**{section}**\n" + "\n".join(items) for section, items in sections.items())
        await send_ephemeral(ctx, embed=emb("🛒 Shop", desc, C_PURPLE))
        return

    _shop_cfg = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}

    # ── !shop nickname ────────────────────────────────────────────────────────
    if subcommand == "nickname":
        if not _shop_cfg.get("nickname", True):
            await ctx.send(embed=emb("🛒 Disabled", "The nickname shop item is disabled in this server.", C_GREY))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop nickname <new_name>` or `!shop nickname @user <new_name>`", C_PURPLE))
            return

        # Determine target: @mention or self
        if ctx.message.mentions and args[0].startswith("<@"):
            target = ctx.message.mentions[0]
            new_name = " ".join(args[1:])
            cost = 0 if uid in godmode_users else 10000
            cost_label = "10,000"
        else:
            target = ctx.author
            new_name = " ".join(args)
            cost = 0 if uid in godmode_users else 5000
            cost_label = "5,000"

        if not new_name:
            await ctx.send(embed=emb("🛒 Shop", "Please provide a new nickname.", C_PURPLE))
            return
        if is_insured(target.id, "nickname"):
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be renamed.", C_GOLD))
            return
        if not await shop_charge(ctx, uid, cost, cost_label=cost_label):
            return
        try:
            await target.edit(nick=new_name)
            await ctx.send(embed=emb("✅ Nickname Changed", f"**{target.display_name}**'s nickname is now **{new_name}**!", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
            log_bot_permission_error(ctx, "change nickname")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to change that nickname.", C_RED))
        except discord.HTTPException as e:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
        return

    # ── !shop removenickname ──────────────────────────────────────────────────
    if subcommand == "removenickname":
        if not _shop_cfg.get("nickname", True):
            await ctx.send(embed=emb("🛒 Disabled", "The nickname shop item is disabled in this server.", C_GREY))
            return
        cost = 0 if uid in godmode_users else 2000
        if is_insured(uid, "nickname"):
            await ctx.send(embed=emb("🛡️ Protected", "You have insurance and can't have your nickname changed.", C_GOLD))
            return
        if not await shop_charge(ctx, uid, cost, cost_label="2,000"):
            return
        try:
            await ctx.author.edit(nick=None)
            await ctx.send(embed=emb("✅ Nickname Removed", "Your nickname has been reset to your username.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to change your nickname.", C_RED))
        except discord.HTTPException as e:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
        return

    # ── !shop role ────────────────────────────────────────────────────────────
    if subcommand == "role":
        if not _shop_cfg.get("role", True):
            await ctx.send(embed=emb("🛒 Disabled", "The role shop item is disabled in this server.", C_GREY))
            return
        if len(args) < 3:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop role @user <name> <hex_color>` (e.g. `!shop role @CoolGuy MyRole ff00aa`)", C_PURPLE))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        # Parse target from mention or ID
        target_arg = args[0]
        target_id = None
        if target_arg.startswith("<@") and target_arg.endswith(">"):
            target_id = int(target_arg.strip("<@!>"))
        else:
            try:
                target_id = int(target_arg)
            except ValueError:
                pass
        if target_id is None:
            await ctx.send(embed=emb("❌ Invalid User", "First argument must be a @mention or user ID.", C_RED))
            return
        target = await fetch_member(ctx.guild, target_id)
        if target is None:
            await ctx.send(embed=emb("❌ User Not Found", "That user isn't in this server.", C_RED))
            return
        hex_color = args[-1].lstrip("#")
        name = " ".join(args[1:-1])
        if "admin" in name.lower():
            await ctx.send(embed=emb("❌ Invalid Name", "Role names cannot contain \"admin\".", C_RED))
            return
        try:
            color_int = int(hex_color, 16)
            if not (0 <= color_int <= 0xFFFFFF):
                raise ValueError
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Color", "Example: `ff00aa` or `#ff00aa`", C_RED))
            return
        if is_insured(target.id, "role"):
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be given new roles.", C_GOLD))
            return
        cost = 0 if uid in godmode_users else 10000
        if not await shop_charge(ctx, uid, cost, cost_label="10,000"):
            return
        try:
            new_role = await ctx.guild.create_role(name=name, color=discord.Color(color_int), hoist=True)
            await target.add_roles(new_role)
            bot_roles.add(new_role.id)
            save_bot_roles()
            await ctx.send(embed=emb("✅ Role Created", f"Role **{name}** created and assigned to **{target.display_name}**!", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
            log_bot_permission_error(ctx, "manage roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
        except Exception as e:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
        return

    # ── !shop removerole ──────────────────────────────────────────────────────
    if subcommand == "removerole":
        if not _shop_cfg.get("removerole", True):
            await ctx.send(embed=emb("🛒 Disabled", "The removerole shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            # List bot-created roles still present in the server
            existing = [r for r in ctx.guild.roles if r.id in bot_roles]
            if not existing:
                await ctx.send(embed=emb("🛒 Bot Roles", "No bot-created roles found in this server.", C_PURPLE))
            else:
                lines = "\n".join(f"• **{r.name}**" for r in existing)
                await ctx.send(embed=emb("🛒 Bot Roles", f"Removable roles:\n{lines}\n\nUse `!shop removerole <name>` to delete one.", C_PURPLE))
            return
        name = " ".join(args)
        role = resolve_role(ctx.guild, name) if len(args) == 1 else None
        if role is None:
            role = discord.utils.find(lambda r: r.name.lower() == name.lower() and r.id in bot_roles, ctx.guild.roles)
        elif role.id not in bot_roles:
            role = None
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", f"No bot-created role named **{name}** exists.", C_RED))
            return
        # Check if user is the only one with this role (allow if 0 or 1 member who is user)
        if len(role.members) > 1 or (len(role.members) == 1 and role.members[0].id != uid):
            member_count = len(role.members)
            await ctx.send(embed=emb("❌ Not Alone", f"This role has **{member_count}** member(s). You must be the only one with this role to remove it.", C_RED))
            return
        cost = 0 if uid in godmode_users else 2000
        if not await shop_charge(ctx, uid, cost, cost_label="2,000"):
            return
        try:
            await role.delete()
            bot_roles.discard(role.id)
            save_bot_roles()
            await ctx.send(embed=emb("✅ Role Removed", f"Role **{name}** has been deleted.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
            log_bot_permission_error(ctx, "delete role")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete that role.", C_RED))
        except Exception as e:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
        return

    # ── !shop channel ─────────────────────────────────────────────────────────
    if subcommand == "channel":
        if not _shop_cfg.get("channel", True):
            await ctx.send(embed=emb("🛒 Disabled", "The channel shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop channel <name>`", C_PURPLE))
            return
        channel_name = " ".join(args).lower()
        # Validate channel name (Discord requirements: 2-100 chars, no spaces converted to hyphens)
        channel_name = channel_name.replace(" ", "-")[:100]
        if len(channel_name) < 2:
            await ctx.send(embed=emb("❌ Invalid Name", "Channel name must be at least 2 characters.", C_RED))
            return
        cost = 0 if uid in godmode_users else 20000
        if not await shop_charge(ctx, uid, cost, cost_label="20,000"):
            return
        try:
            # Find or create the "bot-channels" category
            bot_category = discord.utils.find(lambda c: isinstance(c, discord.CategoryChannel) and c.name.lower() == "bot-channels", ctx.guild.channels)
            if bot_category is None:
                bot_category = await ctx.guild.create_category("bot-channels")

            # Create channel with topic indicating it's a bot-created channel
            new_channel = await ctx.guild.create_text_channel(
                channel_name,
                topic=f"Created by {ctx.author.display_name}",
                category=bot_category
            )
            # Track channel in guild settings
            cfg = get_guild_cfg(ctx.guild.id)
            if "bot_channels" not in cfg:
                cfg["bot_channels"] = []
            cfg["bot_channels"].append(new_channel.id)
            save_guild_settings()
            await ctx.send(embed=emb("✅ Channel Created", f"Channel {new_channel.mention} created in #bot-channels!", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
            log_bot_permission_error(ctx, "create channels")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to create channels.", C_RED))
        except Exception as e:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
        return

    # ── !shop removechannel ────────────────────────────────────────────────────
    if subcommand == "removechannel":
        if not _shop_cfg.get("channel", True):
            await ctx.send(embed=emb("🛒 Disabled", "The channel shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            # List bot-created channels
            cfg = get_guild_cfg(ctx.guild.id)
            bot_channel_ids = cfg.get("bot_channels", [])
            existing = [ch for ch in ctx.guild.channels if ch.id in bot_channel_ids]
            if not existing:
                await ctx.send(embed=emb("🛒 Bot Channels", "No bot-created channels found in this server.", C_PURPLE))
            else:
                lines = "\n".join(f"• {ch.mention}" for ch in existing)
                await ctx.send(embed=emb("🛒 Bot Channels", f"Removable channels:\n{lines}\n\nUse `!shop removechannel <name>` to delete one.", C_PURPLE))
            return
        channel_name = " ".join(args).lower()
        cfg = get_guild_cfg(ctx.guild.id)
        bot_channel_ids = cfg.get("bot_channels", [])
        # Find channel by name
        channel = discord.utils.find(lambda ch: ch.name.lower() == channel_name and ch.id in bot_channel_ids, ctx.guild.channels)
        if channel is None:
            await ctx.send(embed=emb("❌ Not Found", f"No bot-created channel named **{channel_name}** exists.", C_RED))
            return
        cost = 0 if uid in godmode_users else 20000
        if not await shop_charge(ctx, uid, cost, cost_label="20,000"):
            return
        try:
            channel_name = channel.name
            await channel.delete()
            cfg["bot_channels"].remove(channel.id)
            save_guild_settings()
            await ctx.send(embed=emb("✅ Channel Removed", f"Channel **{channel_name}** has been deleted.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
            log_bot_permission_error(ctx, "delete channel")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete that channel.", C_RED))
        except Exception as e:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
        return

    # ── !shop ragebait ────────────────────────────────────────────────────────
    if subcommand == "ragebait":
        if not _shop_cfg.get("ragebait", True):
            await ctx.send(embed=emb("🛒 Disabled", "The ragebait shop item is disabled in this server.", C_GREY))
            return
        if not ctx.message.mentions:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
            return
        target = ctx.message.mentions[0]
        if is_insured(target.id, "ragebait"):
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance against ragebait.", C_GOLD))
            return
        topic = " ".join(a for a in args if not a.startswith("<@"))
        cost = 0 if uid in godmode_users else 2500
        if not await shop_charge(ctx, uid, cost, cost_label="2,500"):
            return
        topic_clause = f" The topic should be specifically about: {topic}." if topic else ""
        ragebait_system = (
            "You are an expert at crafting ragebait — messages specifically engineered to provoke "
            "an emotional reaction. Your goal is to write something that will genuinely irritate, "
            "annoy, or get under the skin of the target. "
            "Rules: be specific to the target by referring to them by name (no @ symbols), be witty and cutting rather than just insulting, "
            "use irony or condescension where effective, keep it under 200 characters, "
            "and make it feel natural — like something a person would actually say. "
            "Output only the ragebait message with no preamble, explanation, or quotation marks."
        )
        prompt = (
            f"Write a ragebait message aimed at {target.display_name}.{topic_clause} "
            "Make it personal, pointed, and likely to provoke a reaction. Do not use @ symbols."
        )
        placeholder = await ctx.send("...")
        typing_task = asyncio.create_task(keep_typing(ctx.channel))
        try:
            async with aiohttp.ClientSession() as session:
                full_response = await stream_ollama(session, [
                    {"role": "system", "content": ragebait_system},
                    {"role": "user", "content": prompt},
                ], placeholder)
            await finalize(placeholder, ctx.channel, f"{target.mention} {full_response}")
            active_ragebaits[target.id] = {"remaining": 4, "history": []}
            save_ragebait()
        except Exception as e:
            if cost > 0:
                add_balance(uid, cost)
            await placeholder.edit(content=f"⚠️ {e}")
        finally:
            typing_task.cancel()
        return

    # ── !shop mock ────────────────────────────────────────────────────────────
    if subcommand == "mock":
        if not ctx.message.mentions:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mock @user`", C_PURPLE))
            return
        target = ctx.message.mentions[0]
        cost = 0 if uid in godmode_users else 1500
        if not await shop_charge(ctx, uid, cost, cost_label="1,500"):
            return
        active_mocks[target.id] = {"remaining": 5, "started_by": uid}
        save_mock()
        await ctx.send(embed=emb(
            "🎭 Mock Activated",
            f"**{target.display_name}** will have their next 5 messages mocked!",
            C_PURPLE,
        ))
        return

    # ── !shop insurance ───────────────────────────────────────────────────────
    if subcommand == "insurance":
        key = str(uid)
        cost = 0 if uid in godmode_users else 500
        if not await shop_charge(ctx, uid, cost, cost_label="500"):
            return
        expires_at = int(time.time() + 86400)
        insurance[key] = {
            "expires_at": expires_at,
            "protected_from": ["ragebait", "mock", "nickname", "role"],
        }
        save_insurance()
        await ctx.send(embed=emb(
            "🛡️ Insurance Purchased",
            f"Protected against ragebait, mock, nickname, and role changes! (expires <t:{expires_at}:R>)",
            C_GREEN,
        ))
        return

    # ── !shop rolecolor ───────────────────────────────────────────────────────
    if subcommand == "rolecolor":
        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop rolecolor <role name> <color>`", C_PURPLE))
            return

        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return

        # Last token is the color; everything before is the role name
        color_str = args[-1]
        role_token = " ".join(args[:-1])
        role = resolve_role(ctx.guild, role_token) if len(args) == 2 else None
        if role is None:
            role = discord.utils.find(lambda r: r.name.lower() == role_token.lower(), ctx.guild.roles)
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", f"No role named **{role_token}** exists.", C_RED))
            return

        try:
            color = discord.Color.from_str(color_str)
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Color", f"Could not parse color: `{color_str}`. Try hex codes like `#FF0000` or color names.", C_RED))
            return

        cost = 0 if uid in godmode_users else 2000
        if not await shop_charge(ctx, uid, cost, cost_label="2,000"):
            return

        try:
            await role.edit(color=color)
            await ctx.send(embed=emb("🎨 Role Color Changed", f"**{role.name}** color set to `{color_str}`.", C_PURPLE))
        except discord.Forbidden:
            log_bot_permission_error(ctx, "edit roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to edit roles.", C_RED))
        except Exception as e:
            await ctx.send(embed=emb("❌ Error", f"Failed to change role color: {str(e)}", C_RED))
        return

    # ── !shop mute ────────────────────────────────────────────────────────────
    if subcommand == "mute":
        if not ctx.message.mentions:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop mute @user`", C_PURPLE))
            return
        target = ctx.message.mentions[0]

        cost = 0 if uid in godmode_users else 5000
        if not await shop_charge(ctx, uid, cost, cost_label="5,000"):
            return

        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return

        try:
            member = await fetch_member(ctx.guild, target.id)
            if not member:
                await ctx.send(embed=emb("❌ User Not Found", f"Could not find **{target.display_name}** in this server.", C_RED))
                return

            # Mute for 5 minutes
            await member.edit(timed_out_until=discord.utils.utcnow() + datetime.timedelta(minutes=5))
            await ctx.send(embed=emb(
                "🔕 Muted",
                f"**{target.display_name}** has been muted for 5 minutes!",
                C_PURPLE,
            ))
        except discord.Forbidden:
            log_bot_permission_error(ctx, "timeout members")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to timeout members.", C_RED))
        except Exception as e:
            await ctx.send(embed=emb("❌ Error", f"Failed to mute: {str(e)}", C_RED))
        return

    # ── !shop simp / concubine ────────────────────────────────────────────────
    if subcommand in ("simp", "concubine"):
        if not ctx.message.mentions:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop simp @user`", C_PURPLE))
            return
        target = ctx.message.mentions[0]

        if target.id == uid:
            await ctx.send(embed=emb("❌ Self Simp", "You can't simp for yourself!", C_RED))
            return

        cost = 0 if uid in godmode_users else 1000
        if not await shop_charge(ctx, uid, cost, cost_label="1,000"):
            return

        global active_simps
        tax_type = "concubine" if subcommand == "concubine" else "simp"
        simp_data = {"master": uid, "type": tax_type}
        # Add timestamp for concubine to expire after 24h
        if tax_type == "concubine":
            simp_data["activated_at"] = time.time()
        active_simps[target.id] = simp_data
        save_simp(active_simps)

        title = "🍆 Concubine Tax Activated" if tax_type == "concubine" else "🍆 Simp Tax Activated"
        await ctx.send(embed=emb(
            title,
            f"**{target.display_name}** now owes **{ctx.author.display_name}** **10 🪙** per message!",
            C_PURPLE,
        ))
        return

    # ── !shop curse ───────────────────────────────────────────────────────────
    if subcommand == "curse":
        if not ctx.message.mentions:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop curse @user`", C_PURPLE))
            return
        target = ctx.message.mentions[0]

        if target.id == uid:
            await ctx.send(embed=emb("❌ Self Curse", "You can't curse yourself!", C_RED))
            return

        cost = 0 if uid in godmode_users else 10000
        if not await shop_charge(ctx, uid, cost, cost_label="10,000"):
            return

        global active_curses
        active_curses[target.id] = {"cursed_by": uid, "remaining": 5}
        save_curse(active_curses)

        await ctx.send(embed=emb(
            "🔮 Curse Activated",
            f"**{target.display_name}** is now cursed for the next **5** messages!",
            C_PURPLE,
        ))
        return

    # ── !shop roleup / roledown ───────────────────────────────────────────────
    if subcommand in ("roleup", "roledown"):
        direction = subcommand  # "roleup" or "roledown"
        cfg_key = "roleup" if direction == "roleup" else "roledown"
        if not _shop_cfg.get(cfg_key, True):
            await ctx.send(embed=emb("🛒 Disabled", f"The {direction} shop item is disabled in this server.", C_GREY))
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not args:
            await ctx.send(embed=emb("🛒 Shop", f"Usage: `!shop {direction} <role name>`", C_PURPLE))
            return
        name = " ".join(args)
        role = resolve_role(ctx.guild, name) if len(args) == 1 else None
        if role is None:
            role = discord.utils.find(lambda r: r.name.lower() == name.lower() and r.id in bot_roles, ctx.guild.roles)
        elif role.id not in bot_roles:
            role = None
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", f"No bot-created role named **{name}** exists.", C_RED))
            return
        # Check boundary relative to other bot-created roles only
        bot_role_positions = sorted(
            r.position for r in ctx.guild.roles if r.id in bot_roles
        )
        if direction == "roleup" and role.position == bot_role_positions[-1]:
            await ctx.send(embed=emb("❌ Already Highest", f"**{role.name}** is already the highest bot-created role.", C_RED))
            return
        if direction == "roledown" and role.position == bot_role_positions[0]:
            await ctx.send(embed=emb("❌ Already Lowest", f"**{role.name}** is already the lowest bot-created role.", C_RED))
            return
        cost = 0 if uid in godmode_users else 20000
        if not await shop_charge(ctx, uid, cost, cost_label="20,000"):
            return
        # Roles are ordered lowest position (bottom) to highest; "up" = higher position value
        new_pos = role.position + (1 if direction == "roleup" else -1)
        new_pos = max(1, min(new_pos, ctx.guild.me.top_role.position - 1))
        try:
            await role.edit(position=new_pos)
            label = "up" if direction == "roleup" else "down"
            await ctx.send(embed=emb("✅ Role Moved", f"Role **{role.name}** moved {label} to position {new_pos}.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
            log_bot_permission_error(ctx, "manage roles")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to manage roles.", C_RED))
        except Exception as e:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ Failed", str(e), C_RED))
        return

    await ctx.send(embed=emb("🛒 Unknown Item", "Try `!shop` to see what's available.", C_PURPLE))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Settings
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="settings", aliases=["setting"])
async def cmd_settings(ctx: commands.Context, subcommand: str = None, *args):
    if ctx.guild is None:
        await ctx.send(embed=emb("❌", "Settings are only available in servers.", C_RED))
        return
    if not can_manage_settings(ctx):
        await ctx.send(embed=emb("❌ No Permission", "Requires server admin or bot admin.", C_RED))
        return

    cfg = get_guild_cfg(ctx.guild.id)

    # ── Show current settings ─────────────────────────────────────────────────
    if subcommand is None:
        ai_channels = cfg.get("ai_channels", [])
        cmd_whitelist = cfg.get("command_whitelist", [])
        cmd_blacklist = cfg.get("command_blacklist", [])
        game_channels = cfg.get("game_channels", [])
        chess_channels = cfg.get("chess_channels", [])
        shop_items = cfg.get("shop_items", {})
        r34_enabled = cfg.get("rule34_enabled", True)
        r34_channels = cfg.get("rule34_channels", [])
        r34_banned = cfg.get("rule34_banned_tags", [])
        lottery_channel_id = cfg.get("lottery_channel")

        ai_val = " ".join(f"<#{c}>" for c in ai_channels) if ai_channels else "all channels"
        whitelist_val = " ".join(f"<#{c}>" for c in cmd_whitelist) if cmd_whitelist else "none (all allowed)"
        blacklist_val = " ".join(f"<#{c}>" for c in cmd_blacklist) if cmd_blacklist else "none"
        game_val = " ".join(f"<#{c}>" for c in game_channels) if game_channels else "all channels"
        chess_val = " ".join(f"<#{c}>" for c in chess_channels) if chess_channels else "game channels (or all)"
        item_names = ["nickname", "role", "removerole", "roleup", "roledown", "ragebait"]
        shop_val = "  ".join(
            f"{n} {'✅' if shop_items.get(n, True) else '❌'}" for n in item_names
        )
        r34_val = ("✅ enabled" if r34_enabled else "❌ disabled")
        r34_ch_val = " ".join(f"<#{c}>" for c in r34_channels) if r34_channels else "all channels"
        r34_val += f"\nChannels: {r34_ch_val}"
        if r34_banned:
            r34_val += f"\nBanned tags: {', '.join(r34_banned)}"
        lottery_val = f"<#{lottery_channel_id}>" if lottery_channel_id else "❌ disabled"
        soundboard_rl = cfg.get("soundboard_ratelimit", [])
        if soundboard_rl:
            rl_names = []
            for uid in soundboard_rl:
                member = ctx.guild.get_member(uid)
                rl_names.append(member.display_name if member else str(uid))
            rl_val = ", ".join(rl_names)
        else:
            rl_val = "none"

        gambler_role_val = "✅ enabled" if cfg.get("gambler_role_enabled", False) else "❌ disabled"

        embed = discord.Embed(title="⚙️ Server Settings", color=C_BLUE)
        embed.add_field(name="🤖 AI channels", value=ai_val, inline=False)
        embed.add_field(name="✅ Channel whitelist", value=whitelist_val, inline=False)
        embed.add_field(name="❌ Channel blacklist", value=blacklist_val, inline=False)
        embed.add_field(name="🎮 Game channels", value=game_val, inline=False)
        embed.add_field(name="♟️ Chess channels", value=chess_val, inline=False)
        embed.add_field(name="🛒 Shop items", value=shop_val, inline=False)
        embed.add_field(name="🔞 rule34", value=r34_val, inline=False)
        embed.add_field(name="🎰 Lottery channel", value=lottery_val, inline=False)
        embed.add_field(name="🔇 Soundboard rate-limit", value=rl_val, inline=False)
        embed.add_field(name="🎲 Gambler role", value=gambler_role_val, inline=False)
        footer_text = (
            "Subcommands:\n"
            "`ai-channels #ch... / clear` • `cmd-whitelist #ch... / clear` • `cmd-blacklist #ch... / clear` • `game-channels #ch... / clear` • `chess-channels #ch... / clear`\n"
            "`shop <item> on|off` • `rule34 on|off / channels add|remove|list / ban <tag> / unban <tag> / banned` • `lottery-channel #channel / clear` • `soundboard-ratelimit add|remove @user|<userid> / list` • `gambler-role on|off`"
        )
        embed.set_footer(text=footer_text)
        await send_ephemeral(ctx, embed=embed)
        return

    # ── ai-channels ───────────────────────────────────────────────────────────
    if subcommand == "ai-channels":
        if args and args[0].lower() == "clear":
            cfg["ai_channels"] = []
            save_guild_settings()
            await ctx.send(embed=emb("⚙️ AI Channels", "AI channel restriction removed — all channels allowed.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["ai_channels"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("⚙️ AI Channels", f"AI commands restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("⚙️ AI Channels", "Usage: `!settings ai-channels #channel ...` or `!settings ai-channels clear`", C_GREY))
        return

    # ── cmd-whitelist ─────────────────────────────────────────────────────────
    if subcommand == "cmd-whitelist":
        if args and args[0].lower() == "clear":
            cfg["command_whitelist"] = []
            save_guild_settings()
            await ctx.send(embed=emb("✅ Channel Whitelist", "Whitelist removed — commands allowed in all channels.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["command_whitelist"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("✅ Channel Whitelist", f"Commands restricted to: {names}\n(Note: `!settings` always works everywhere)", C_GREEN))
        else:
            await ctx.send(embed=emb("✅ Channel Whitelist", "Usage: `!settings cmd-whitelist #channel ...` or `!settings cmd-whitelist clear`", C_GREY))
        return

    # ── cmd-blacklist ─────────────────────────────────────────────────────────
    if subcommand == "cmd-blacklist":
        if args and args[0].lower() == "clear":
            cfg["command_blacklist"] = []
            save_guild_settings()
            await ctx.send(embed=emb("❌ Channel Blacklist", "Blacklist cleared — commands allowed in all channels.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["command_blacklist"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("❌ Channel Blacklist", f"Commands blocked in: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("❌ Channel Blacklist", "Usage: `!settings cmd-blacklist #channel ...` or `!settings cmd-blacklist clear`", C_GREY))
        return

    # ── chess-channels ────────────────────────────────────────────────────────
    if subcommand == "chess-channels":
        if args and args[0].lower() == "clear":
            cfg["chess_channels"] = []
            save_guild_settings()
            await ctx.send(embed=emb("♟️ Chess Channels", "Chess channel restriction removed — all channels allowed.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["chess_channels"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("♟️ Chess Channels", f"Chess restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("♟️ Chess Channels", "Usage: `!settings chess-channels #channel ...` or `!settings chess-channels clear`", C_GREY))
        return

    # ── game-channels ─────────────────────────────────────────────────────────
    if subcommand == "game-channels":
        if args and args[0].lower() == "clear":
            cfg["game_channels"] = []
            save_guild_settings()
            await ctx.send(embed=emb("🎮 Game Channels", "Game channel restriction removed — games and gambling allowed everywhere.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["game_channels"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("🎮 Game Channels", f"Games and gambling restricted to: {names}", C_GREEN))
        else:
            await ctx.send(embed=emb("🎮 Game Channels", "Usage: `!settings game-channels #channel ...` or `!settings game-channels clear`", C_GREY))
        return

    # ── shop ──────────────────────────────────────────────────────────────────
    if subcommand == "shop":
        valid_items = {"nickname", "role", "removerole", "roleup", "roledown", "ragebait"}
        if len(args) < 2 or args[0].lower() not in valid_items or args[1].lower() not in ("on", "off"):
            await ctx.send(embed=emb("⚙️ Shop", f"Usage: `!settings shop <item> on|off`\nItems: {', '.join(valid_items)}", C_GREY))
            return
        item = args[0].lower()
        enabled = args[1].lower() == "on"
        if "shop_items" not in cfg:
            cfg["shop_items"] = {}
        cfg["shop_items"][item] = enabled
        save_guild_settings()
        status = "✅ enabled" if enabled else "❌ disabled"
        await ctx.send(embed=emb("⚙️ Shop", f"**{item}** is now {status}.", C_GREEN))
        return

    # ── rule34 ────────────────────────────────────────────────────────────────
    if subcommand == "rule34":
        if not args:
            await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 on|off` / `channels <add|remove|list> [#channel]` / `ban <tag>` / `unban <tag>` / `banned`", C_GREY))
            return
        action = args[0].lower()
        if action in ("on", "off"):
            cfg["rule34_enabled"] = (action == "on")
            save_guild_settings()
            status = "✅ enabled" if action == "on" else "❌ disabled"
            await ctx.send(embed=emb("⚙️ rule34", f"rule34 is now {status}.", C_GREEN))
        elif action == "channels":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 channels <add|remove|list> [#channel]`", C_GREY))
                return
            channel_action = args[1].lower()
            r34_channels = cfg.setdefault("rule34_channels", [])

            if channel_action == "add":
                if not ctx.message.channel_mentions:
                    await ctx.send(embed=emb("⚙️ rule34", "Please mention a channel to add.", C_GREY))
                    return
                for channel in ctx.message.channel_mentions:
                    if channel.id not in r34_channels:
                        r34_channels.append(channel.id)
                save_guild_settings()
                names = " ".join(f"<#{cid}>" for cid in ctx.message.channel_mentions)
                await ctx.send(embed=emb("⚙️ rule34 Channels", f"Added {names} to whitelist.", C_GREEN))
            elif channel_action == "remove":
                if not ctx.message.channel_mentions:
                    await ctx.send(embed=emb("⚙️ rule34", "Please mention a channel to remove.", C_GREY))
                    return
                for channel in ctx.message.channel_mentions:
                    if channel.id in r34_channels:
                        r34_channels.remove(channel.id)
                save_guild_settings()
                names = " ".join(f"<#{cid}>" for cid in ctx.message.channel_mentions)
                await ctx.send(embed=emb("⚙️ rule34 Channels", f"Removed {names} from whitelist.", C_GREEN))
            elif channel_action == "list":
                val = " ".join(f"<#{cid}>" for cid in r34_channels) if r34_channels else "none"
                await ctx.send(embed=emb("⚙️ rule34 Channels", val, C_GREY))
            else:
                await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 channels <add|remove|list> [#channel]`", C_GREY))
        elif action == "ban" and len(args) >= 2:
            tag = args[1].lower()
            banned = cfg.setdefault("rule34_banned_tags", [])
            if tag not in banned:
                banned.append(tag)
                save_guild_settings()
            await ctx.send(embed=emb("⚙️ rule34", f"Tag `{tag}` banned.", C_GREEN))
        elif action == "unban" and len(args) >= 2:
            tag = args[1].lower()
            banned = cfg.get("rule34_banned_tags", [])
            if tag in banned:
                banned.remove(tag)
                save_guild_settings()
                await ctx.send(embed=emb("⚙️ rule34", f"Tag `{tag}` unbanned.", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ rule34", f"Tag `{tag}` was not banned.", C_GREY))
        elif action == "banned":
            banned = cfg.get("rule34_banned_tags", [])
            val = ", ".join(f"`{t}`" for t in banned) if banned else "none"
            await ctx.send(embed=emb("⚙️ rule34 Banned Tags", val, C_GREY))
        else:
            await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 on|off` / `channels <add|remove|list> [#channel]` / `ban <tag>` / `unban <tag>` / `banned`", C_GREY))
        return

    # ── quote ────────────────────────────────────────────────────────────────
    if subcommand == "quote":
        if not args:
            await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
            return
        action = args[0].lower()
        if action == "bypass":
            if len(args) < 2:
                await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
                return
            bypass_action = args[1].lower()
            if bypass_action in ("on", "off"):
                cfg["quote_bypass_restrictions"] = (bypass_action == "on")
                save_guild_settings()
                status = "✅ enabled" if bypass_action == "on" else "❌ disabled"
                await ctx.send(embed=emb("⚙️ quote", f"Quote bypass is now {status} (quote works in any channel).", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
        else:
            await ctx.send(embed=emb("⚙️ quote", "Usage: `!settings quote bypass on|off`", C_GREY))
        return

    # ── lottery-channel ───────────────────────────────────────────────────────
    if subcommand == "lottery-channel":
        if args and args[0].lower() == "clear":
            cfg["lottery_channel"] = None
            save_guild_settings()
            await ctx.send(embed=emb("🎰 Lottery Channel", "Lottery disabled.", C_GREEN))
        elif ctx.message.channel_mentions:
            channel = ctx.message.channel_mentions[0]
            cfg["lottery_channel"] = channel.id
            save_guild_settings()

            # Start first lottery if none is active
            current_week = datetime.datetime.now().isocalendar()[1]
            lottery = load_lottery(ctx.guild.id)
            if lottery.get("last_posted_week", 0) != current_week:
                lottery = {"prize_pool": 2000, "players": {}, "last_posted_week": current_week}
                drain_bot_balance_into_lottery(lottery, ctx.guild.id)
                save_lottery(ctx.guild.id, lottery)

                # Announce to lottery channel
                try:
                    await announce_new_lottery(channel, lottery["prize_pool"])
                except:
                    pass

            await ctx.send(embed=emb("🎰 Lottery Channel", f"Lottery channel set to {channel.mention}\n🎟️ Lottery ready!", C_GREEN))
        else:
            await ctx.send(embed=emb("🎰 Lottery Channel", "Usage: `!settings lottery-channel #channel` or `!settings lottery-channel clear`", C_GREY))
        return

    # ── soundboard-ratelimit ──────────────────────────────────────────────────
    if subcommand == "soundboard-ratelimit":
        action = args[0].lower() if args else ""
        rl_list = cfg.setdefault("soundboard_ratelimit", [])

        if action == "add":
            # Collect user IDs from mentions and/or numeric arguments
            user_ids = []
            if ctx.message.mentions:
                user_ids.extend([m.id for m in ctx.message.mentions])
            # Also check remaining args for numeric IDs
            for arg in args[1:]:
                try:
                    user_ids.append(int(arg))
                except ValueError:
                    pass

            if not user_ids:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "Usage: `!settings soundboard-ratelimit add @user` or `!settings soundboard-ratelimit add <userid>`", C_GREY))
                return

            added = []
            for uid in user_ids:
                if uid not in rl_list:
                    rl_list.append(uid)
                    added.append(f"`{uid}`")

            if added:
                save_guild_settings()
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", f"Added: {' '.join(added)}", C_GREEN))
            else:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "All users already in the list.", C_GREY))

        elif action == "remove":
            # Collect user IDs from mentions and/or numeric arguments
            user_ids = []
            if ctx.message.mentions:
                user_ids.extend([m.id for m in ctx.message.mentions])
            # Also check remaining args for numeric IDs
            for arg in args[1:]:
                try:
                    user_ids.append(int(arg))
                except ValueError:
                    pass

            if not user_ids:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "Usage: `!settings soundboard-ratelimit remove @user` or `!settings soundboard-ratelimit remove <userid>`", C_GREY))
                return

            removed = []
            for uid in user_ids:
                if uid in rl_list:
                    rl_list.remove(uid)
                    removed.append(f"`{uid}`")

            if removed:
                save_guild_settings()
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", f"Removed: {' '.join(removed)}", C_RED))
            else:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "None of those users were in the list.", C_GREY))

        elif action == "list":
            if not rl_list:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "No users on the list.", C_GREY))
            else:
                await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", f"**{len(rl_list)} user(s):**\n" + " ".join(f"`{uid}`" for uid in rl_list), C_GOLD))

        else:
            await ctx.send(embed=emb("⚙️ Soundboard Rate-Limit", "Usage: `!settings soundboard-ratelimit add|remove @user|<userid>` or `list`", C_GREY))
        return

    # ── gambler-role ──────────────────────────────────────────────────────────
    if subcommand == "gambler-role":
        if not args or args[0].lower() not in ("on", "off"):
            await ctx.send(embed=emb("⚙️ Gambler Role", "Usage: `!settings gambler-role on|off`", C_GREY))
            return
        enabled = args[0].lower() == "on"
        cfg["gambler_role_enabled"] = enabled
        save_guild_settings()
        status = "✅ enabled" if enabled else "❌ disabled"
        detail = "\nThe **Gamblers** role will be auto-created and assigned to users who use all 3 scratchoffs 2 days in a row. They will be pinged when a progressive jackpot is won." if enabled else ""
        await ctx.send(embed=emb("⚙️ Gambler Role", f"Gambler role tracking is now {status}.{detail}", C_GREEN))
        return

    await ctx.send(embed=emb("⚙️ Settings", "Unknown subcommand. Use `!settings` to see options.", C_GREY))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Moderation
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="audit")
async def cmd_audit(ctx: commands.Context):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if not audit_log:
        await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "No failed attempts recorded.", C_GREY))
        return
    recent = list(audit_log)[-5:]
    lines = []
    for e in reversed(recent):
        ts = time.strftime("%H:%M:%S", time.localtime(e["time"]))
        lines.append(f"**{ts}** — {e['user']}\n`{e['command']}`\n_{e['error']}_")
    await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "\n\n".join(lines), C_GOLD))


@bot.command(name="clearbot")
async def cmd_clear(ctx: commands.Context, n: int = 50):
    if not can_manage_settings(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    deleted = 0
    async for message in ctx.channel.history(limit=500):
        if deleted >= n:
            break
        if message.author == bot.user:
            await message.delete()
            deleted += 1
    confirm = await ctx.send(embed=emb(
        "🗑️ Cleared",
        f"Deleted {deleted} bot message{'s' if deleted != 1 else ''}.",
        C_GREY,
    ))
    await asyncio.sleep(5)
    await confirm.delete()


@bot.command(name="clearall")
async def cmd_clearall(ctx: commands.Context, n: str = None):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return

    if n is None:
        await ctx.send(embed=emb("❌ Missing Argument", "Usage: `!clearall <n>` — Delete last n messages", C_RED))
        return

    try:
        n = int(n) + 1
        if n <= 1:
            await ctx.send(embed=emb("❌ Invalid Number", "Please provide a positive integer.", C_RED))
            return
        if n > 101:
            await ctx.send(embed=emb("❌ Too Many", "Maximum 100 messages at a time.", C_RED))
            return
    except ValueError:
        await ctx.send(embed=emb("❌ Invalid Input", "Please provide a valid number.", C_RED))
        return

    try:
        messages = []
        async for message in ctx.channel.history(limit=n):
            messages.append(message)

        if not messages:
            await ctx.send(embed=emb("❌ No Messages", "No messages found to delete.", C_RED))
            return

        await ctx.channel.delete_messages(messages)
        confirm = await ctx.send(embed=emb(
            "🗑️ Cleared",
            f"Deleted {len(messages)-1} message{'s' if len(messages) != 1 else ''}.",
            C_GREY,
        ))
        await asyncio.sleep(5)
        await confirm.delete()
    except discord.Forbidden:
        await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete messages.", C_RED))
    except Exception as e:
        await ctx.send(embed=emb("❌ Error", f"Failed to delete messages: {str(e)}", C_RED))


@bot.command(name="saved", aliases=["persistent", "saves"])
async def cmd_saved(ctx: commands.Context):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return

    embed = discord.Embed(title="💾 Saved Data", color=C_GOLD)

    # Insurance data
    embed.add_field(
        name="🛡️ Insurance",
        value=f"**{len(insurance)}** users with active insurance",
        inline=False
    )

    # Mock data
    embed.add_field(
        name="🎭 Mock",
        value=f"**{len(active_mocks)}** users being mocked",
        inline=False
    )

    # Ragebait data
    embed.add_field(
        name="🎯 Ragebait",
        value=f"**{len(active_ragebaits)}** users with ragebait active",
        inline=False
    )

    # Simp data
    embed.add_field(
        name="🍆 Simp Tax",
        value=f"**{len(active_simps)}** users with simp tax active",
        inline=False
    )

    # Curse data
    embed.add_field(
        name="🔮 Curse",
        value=f"**{len(active_curses)}** users with curse active",
        inline=False
    )

    # Rigged slots
    embed.add_field(
        name="🎰 Rigged Slots",
        value=f"**{len(rigged_slots)}** users rigged for jackpot",
        inline=False
    )

    # Godmode users
    embed.add_field(
        name="👑 Godmode",
        value=f"**{len(godmode_users)}** users with godmode",
        inline=False
    )

    # Slot jackpot
    embed.add_field(
        name="💰 Slot Jackpot",
        value=f"**{slot_jackpot:,} 🪙** in jackpot",
        inline=False
    )

    # Chess games
    embed.add_field(
        name="♟️ Chess Games",
        value=f"**{len(active_chess_games)}** active correspondence chess games",
        inline=False
    )

    # Quote log
    _all_saved = load_saved_quotes()
    _guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    saved_quotes_count = len(_all_saved.get(_guild_id, []))
    embed.add_field(
        name="📜 Quotes",
        value=f"**{saved_quotes_count}** saved quotes (this server) | **{len(quote_log)}** in searchquote log (max 10)",
        inline=False
    )

    # Economy stats
    total_users = len(economy.get("users", {}))
    total_balance = sum(u.get("balance", 0) for u in economy.get("users", {}).values())
    embed.add_field(
        name="🪙 Economy",
        value=f"**{total_users}** users with **{total_balance:,} 🪙** total balance",
        inline=False
    )

    await send_ephemeral(ctx, embed=embed)


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Admin (.xeph only)
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="godmode")
async def cmd_godmode(ctx: commands.Context, user: discord.User = None):
    global godmode_users
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return

    target_user = user if user else ctx.author
    if target_user.id in godmode_users:
        godmode_users.remove(target_user.id)
        state = "disabled"
    else:
        godmode_users.add(target_user.id)
        state = "enabled"

    save_godmode_users()
    await ctx.send(embed=emb("👑 Godmode", f"Godmode **{state}** for {target_user.mention}.", C_GOLD))


@bot.command(name="adminragebait")
async def cmd_adminragebait(ctx: commands.Context, user_input: str = None, n: str = None):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return

    if user_input is None:
        await ctx.send(embed=emb("❌ Missing User", "Usage: `!adminragebait @user [n]` or `!adminragebait <userid> [n]`", C_RED))
        return

    # Determine target user ID
    uid = None

    if ctx.message.mentions:
        # Priority: use mention if present
        uid = ctx.message.mentions[0].id
    else:
        # Try to parse as user ID
        try:
            uid = int(user_input)
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Input", f"Could not parse `{user_input}` as a user ID or mention.", C_RED))
            return

    # Parse optional message count (default 5)
    try:
        count = int(n) if n else 5
        if count <= 0:
            await ctx.send(embed=emb("❌ Invalid Count", "Please provide a positive number.", C_RED))
            return
    except ValueError:
        await ctx.send(embed=emb("❌ Invalid Count", f"Could not parse `{n}` as a number.", C_RED))
        return

    active_ragebaits[uid] = {"remaining": count, "history": []}
    save_ragebait()
    await ctx.send(embed=emb(
        "🎭 Ragebait Activated",
        f"Ragebait enabled for user `{uid}` (next **{count}** message(s))",
        C_PURPLE,
    ))


@bot.command(name="model")
async def cmd_model(ctx: commands.Context, model_name: str = None):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if ctx.guild is None:
        await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
        return
    cfg = get_guild_cfg(ctx.guild.id)
    if model_name is None:
        current = cfg.get("ask_model", OLLAMA_MODEL)
        await ctx.send(embed=emb("⚙️ Model", f"Current model: `{current}`", C_GREY))
        return
    cfg["ask_model"] = model_name
    save_guild_settings()
    await ctx.send(embed=emb("⚙️ Model", f"Switched to `{model_name}`", C_GREY))


@bot.command(name="roleplaymodel")
async def cmd_roleplaymodel(ctx: commands.Context, model_name: str = None):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if ctx.guild is None:
        await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
        return
    cfg = get_guild_cfg(ctx.guild.id)
    if model_name is None:
        current = cfg.get("roleplay_model", OLLAMA_MODEL)
        await ctx.send(embed=emb("⚙️ Roleplay Model", f"Current roleplay model: `{current}`", C_GREY))
        return
    cfg["roleplay_model"] = model_name
    save_guild_settings()
    await ctx.send(embed=emb("⚙️ Roleplay Model", f"Switched to `{model_name}`", C_GREY))


@bot.command(name="codingmodel")
async def cmd_codingmodel(ctx: commands.Context, model_name: str = None):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if ctx.guild is None:
        await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
        return
    cfg = get_guild_cfg(ctx.guild.id)
    if model_name is None:
        current = cfg.get("coding_model", OLLAMA_MODEL)
        await ctx.send(embed=emb("⚙️ Coding Model", f"Current coding puzzle model: `{current}`", C_GREY))
        return
    cfg["coding_model"] = model_name
    save_guild_settings()
    await ctx.send(embed=emb("⚙️ Coding Model", f"Switched to `{model_name}`", C_GREY))


@bot.command(name="vramtext")
async def cmd_vramtext(ctx: commands.Context, *, text: str = None):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if text is None:
        await ctx.send(embed=emb("⚙️ vRAM Text", bot_settings.get("vram_text", "16GB"), C_GREY))
        return
    bot_settings["vram_text"] = text
    save_bot_settings()
    await ctx.send(embed=emb("⚙️ vRAM Text", f"Set to: {text}", C_GREY))


@bot.command(name="setprompt")
async def cmd_setprompt(ctx: commands.Context, *, prompt: str):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    channel_prompts[ctx.channel.id] = prompt
    save_channel_prompts(channel_prompts)
    await ctx.send(embed=emb("⚙️ Prompt Updated", "System prompt updated for this channel.", C_GREY))


@bot.command(name="clearprompt")
async def cmd_clearprompt(ctx: commands.Context):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    channel_prompts.pop(ctx.channel.id, None)
    save_channel_prompts(channel_prompts)
    await ctx.send(embed=emb("⚙️ Prompt Cleared", "Using default system prompt.", C_GREY))


@bot.command(name="event")
async def cmd_event(ctx: commands.Context, amount: str = None, duration: str = None):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        logging.warning(f"[event] No permission to delete command message in {ctx.channel}")
    except Exception as e:
        logging.warning(f"[event] Failed to delete command message: {e}")
    if amount is None:
        await ctx.send(embed=emb("⚙️ Event", "Usage: `!event <amount> [duration_hours] [#channel]`", C_GREY))
        return
    try:
        amount = int(amount)
        assert amount > 0
    except (ValueError, AssertionError):
        await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a positive whole number.", C_RED))
        return

    duration_hours = None
    if duration is not None:
        # If duration looks like a channel mention, it's actually the channel arg
        if duration.startswith("<#"):
            duration = None
        else:
            try:
                duration_hours = float(duration)
                assert duration_hours > 0
            except (ValueError, AssertionError):
                await ctx.send(embed=emb("❌ Invalid Duration", "Duration must be a positive number of hours.", C_RED))
                return

    # Resolve target channel
    target_channel = ctx.channel
    if ctx.message.channel_mentions:
        target_channel = ctx.message.channel_mentions[-1]

    duration_str = ""
    if duration_hours:
        expires_at = int(time.time() + duration_hours * 3600)
        duration_str = f" (expires <t:{expires_at}:R>)"
    event_msg = await target_channel.send(embed=emb(
        "🎉 Coin Event!",
        f"React with 🪙 to receive **{amount} 🪙**!{duration_str}",
        C_GOLD,
    ))
    await event_msg.add_reaction("🪙")
    active_events[event_msg.id] = {"amount": amount, "rewarded": set()}

    if target_channel != ctx.channel:
        await ctx.send(embed=emb("✅ Event Started", f"Event posted in {target_channel.mention}.", C_GREEN))

    if duration_hours:
        async def _close_event():
            await asyncio.sleep(duration_hours * 3600)
            if event_msg.id in active_events:
                del active_events[event_msg.id]
                await event_msg.edit(embed=emb(
                    "🎉 Event Ended",
                    f"This event has ended. **{amount} 🪙** per reaction was given out.",
                    C_GREY,
                ))
        asyncio.create_task(_close_event())


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if reaction.message.id not in active_events:
        return
    if str(reaction.emoji) != "🪙":
        return
    event = active_events[reaction.message.id]
    if user.id in event["rewarded"]:
        return
    try:
        event["rewarded"].add(user.id)
        add_balance(user.id, event["amount"])
    except Exception as e:
        logging.error(f"[event] Error rewarding {user.id}: {e}")
        event["rewarded"].discard(user.id)


@bot.command(name="admingive", aliases=["adminpay"])
async def cmd_give(ctx: commands.Context, target: discord.Member = None, amount: str = None):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if target is None or amount is None:
        await ctx.send(embed=emb("⚙️ Give", "Usage: `!give @user <amount>`", C_GREY))
        return
    try:
        amount = int(amount)
        assert amount != 0
        if amount < 0:
            if bot.user and target.id == bot.user.id:
                amount = max(amount, -1 * get_guild_house_balance(ctx.guild.id if ctx.guild else 0))
            else:
                amount = max(amount, -1*get_balance(target.id))
    except (ValueError, AssertionError):
        await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a non-zero whole number.", C_RED))
        return
    if bot.user and target.id == bot.user.id and ctx.guild:
        add_guild_house(ctx.guild.id, amount)
        action = "given to" if amount > 0 else "removed from"
        await ctx.send(embed=emb(
            "💸 Give",
            f"**{abs(amount)} 🪙** {action} the **house pot** for this server. "
            f"House pot: {get_guild_house_balance(ctx.guild.id)} 🪙",
            C_GOLD,
        ))
    else:
        add_balance(target.id, amount)
        action = "given" if amount > 0 else "removed"
        await ctx.send(embed=emb(
            "💸 Give",
            f"**{abs(amount)} 🪙** {action} {'to' if amount > 0 else 'from'} **{target.display_name}**. "
            f"New balance: {get_balance(target.id)} 🪙",
            C_GOLD,
        ))


@bot.command(name="say")
async def cmd_say(ctx: commands.Context, *, text: str = None):
    if not can_manage_settings(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if text is None:
        await ctx.send(embed=emb("🔊 Say", "Usage: `!say <text>`", C_GREY))
        return
    # Try to delete the command message (fail silently)
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass
    # Send the message
    await ctx.send(text)


@bot.command(name="botinvitelink", aliases=["botinvite"])
async def cmd_botinvite(ctx: commands.Context):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return

    invite_url = "https://discord.com/oauth2/authorize?client_id=1489403251303518322&permissions=6192724835560529&integration_type=0&scope=bot"

    # Create a view with a button
    class InviteView(ui.View):
        @ui.button(label="Get Bot Invitation Link", style=discord.ButtonStyle.primary)
        async def copy_button(self, interaction: discord.Interaction, button: ui.Button):
            # Verify the user clicking the button is an admin
            user_ctx = await bot.get_context(interaction.message)
            user_ctx.author = interaction.user
            if not is_admin(user_ctx):
                await interaction.response.send_message("❌ You don't have permission to view this link.", ephemeral=True)
                return
            await interaction.response.send_message(f"```\n{invite_url}\n```", ephemeral=True)

    embed = discord.Embed(
        title="🤖 Bot Invite Link",
        description="Click the button below to get a copy of the bot invite URL",
        color=discord.Color(0x9932CC)
    )
    embed.add_field(name="Client ID", value="1489403251303518322", inline=False)
    embed.add_field(name="Permissions", value="6192724835560529", inline=False)

    await ctx.send(embed=embed, view=InviteView())


@bot.command(name="invitelink")
async def cmd_invite(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
        return

    try:
        # Try to get vanity URL first (if server has one)
        if ctx.guild.vanity_url:
            invite_url = str(ctx.guild.vanity_url)
        else:
            # Create an invite link
            invite = await ctx.channel.create_invite(max_age=0, max_uses=0)
            invite_url = invite.url

        # Create a view with a button
        class ServerInviteView(ui.View):
            @ui.button(label="Get Server Invitation Link", style=discord.ButtonStyle.primary)
            async def copy_button(self, interaction: discord.Interaction, button: ui.Button):
                await interaction.response.send_message(f"```\n{invite_url}\n```", ephemeral=True)

        embed = discord.Embed(
            title=f"📩 Invite to {ctx.guild.name}",
            description="Click the button below to get a copy of the server invite URL",
            color=discord.Color(0x9932CC)
        )
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)

        await ctx.send(embed=embed, view=ServerInviteView())
    except discord.Forbidden:
        log_bot_permission_error(ctx, "create invites")
        await ctx.send(embed=emb("❌ No Permission", "I don't have permission to create invites in this channel.", C_RED))
    except Exception as e:
        await ctx.send(embed=emb("❌ Error", f"Failed to generate invite: {str(e)}", C_RED))


@bot.command(name="restart")
async def cmd_restart(ctx: commands.Context):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "Only bot admins can use this command.", C_RED))
        return
    msg = await ctx.send(embed=emb("🔄 Restarting", "Bot is restarting...", C_GOLD))
    _save_json(RESTART_MSG_FILE, {"channel_id": msg.channel.id, "message_id": msg.id})
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Rule34
# ─────────────────────────────────────────────────────────────────────────────

# Tracks the last rule34 bot message per (channel_id, user_id)
_r34_last_msg: dict[tuple[int, int], discord.Message] = {}

async def _r34_fetch(session: aiohttp.ClientSession, search_tags: str) -> list[dict]:
    async def _fetch_pid(pid: int) -> list[dict]:
        url = (
            f"https://api.rule34.xxx/index.php"
            f"?page=dapi&s=post&q=index&json=1&limit=100&pid={pid}&tags={search_tags}"
        )
        if RULE34_API_KEY and RULE34_USER_ID:
            url += f"&api_key={RULE34_API_KEY}&user_id={RULE34_USER_ID}"
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            text = (await resp.text()).strip()
        if not text or text == "0" or text.startswith("<"):
            return []
        try:
            import json as _json
            data = _json.loads(text)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [p for p in data if isinstance(p, dict) and p.get("file_url")]

    # Try a random page first (pages 0–19 = up to 2000 posts), fall back to page 0
    rand_pid = random.randint(0, 19)
    if rand_pid > 0:
        posts = await _fetch_pid(rand_pid)
        if posts:
            return posts
    return await _fetch_pid(0)



@bot.command(name="rule34", aliases=["r34"])
async def cmd_rule34(ctx: commands.Context, *, tags: str = ""):
    cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
    if not cfg.get("rule34_enabled", False):
        await ctx.send(embed=emb("🔞 Disabled", "rule34 is disabled in this server.", C_GREY))
        return

    # Check channel whitelist
    if ctx.guild:
        r34_channels = cfg.get("rule34_channels", [])
        if r34_channels and ctx.channel.id not in r34_channels:
            names = " ".join(f"<#{cid}>" for cid in r34_channels)
            await _wrong_channel_reply(ctx, f"rule34 is only allowed in: {names}")
            return
    await ctx.typing()
    _STOP = {"and", "or", "with", "the", "a", "an"}
    tag_parts = [w for w in tags.strip().split() if w.lower() not in _STOP]
    banned = [t.lower() for t in cfg.get("rule34_banned_tags", [])]
    tag_parts = [w for w in tag_parts if w.lower() not in banned]

    def _filter_banned(posts: list[dict]) -> list[dict]:
        if not banned:
            return posts
        return [
            p for p in posts
            if not any(
                any(bt in tag for tag in p.get("tags", "").lower().split())
                for bt in banned
            )
        ]

    # Append server-side exclusions so the API pre-filters results
    ban_query = "".join(f"+-{bt}" for bt in banned)

    try:
        async with aiohttp.ClientSession() as session:
            # Try all tags combined first, then fall back to each tag alone
            search_tags = "+".join(tag_parts) if tag_parts else "solo"
            posts = _filter_banned(await _r34_fetch(session, search_tags + ban_query))

            if not posts and len(tag_parts) > 1:
                for part in tag_parts:
                    posts = _filter_banned(await _r34_fetch(session, part + ban_query))
                    if posts:
                        search_tags = part
                        break
    except Exception as e:
        await ctx.send(embed=emb("❌ rule34", f"Request failed: {e}", C_RED))
        return

    if not posts:
        label = " ".join(tag_parts) if tag_parts else "solo"
        await ctx.send(embed=emb("🔞 rule34", f"No results for `{label}`.", C_GREY))
        return

    logging.info(f"[rule34] {len(posts)} posts after filtering")
    post = random.choice(posts)
    file_url = post["file_url"]
    logging.info(f"[rule34] picked id={post.get('id')} url={file_url}")
    # Bust Discord's embed image cache
    file_url = f"{file_url}?v={post.get('id', random.randint(0, 999999))}"
    display = search_tags.replace("+", " ") if tag_parts else "random"

    embed = discord.Embed(title=f"🔞 rule34: {display}", color=C_PURPLE)
    embed.set_image(url=file_url)
    embed.set_footer(text=f"Score: {post.get('score', '?')} | Rating: {post.get('rating', '?')}")
    msg = await ctx.send(embed=embed)
    _r34_last_msg[(ctx.channel.id, ctx.author.id)] = msg


@bot.command(name="ew")
async def cmd_ew(ctx: commands.Context):
    key = (ctx.channel.id, ctx.author.id)
    msg = _r34_last_msg.pop(key, None)
    if msg is None:
        return
    try:
        await msg.delete()
    except discord.NotFound:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Quote
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="quote")
async def cmd_quote(ctx: commands.Context):
    """Save a replied-to message as a persistent quote, or display a random saved quote.

    Usage:
    - !quote (reply) — save the replied-to message as a quote
    - !quote — display a random saved quote
    """
    guild_id = str(ctx.guild.id) if ctx.guild else "dm"
    all_quotes = load_saved_quotes()
    guild_quotes = all_quotes.get(guild_id, [])

    if ctx.message.reference:
        # Save the replied-to message as a quote
        try:
            replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except Exception:
            await ctx.send(embed=emb("❌ Error", "Could not fetch the replied-to message.", C_RED))
            return

        if replied_msg.author == bot.user:
            await ctx.send(embed=emb("📜 Quote", "You can't save the bot's messages as quotes.", C_GREY))
            return

        quote_entry = {
            "content": replied_msg.content,
            "author": replied_msg.author.display_name,
            "author_id": replied_msg.author.id,
            "saved_by": ctx.author.display_name,
            "saved_by_id": ctx.author.id,
            "timestamp": replied_msg.created_at.isoformat(),
        }
        guild_quotes.append(quote_entry)
        all_quotes[guild_id] = guild_quotes
        save_saved_quotes(all_quotes)

        clean_content = re.sub(r'<@!?\d+>', '', replied_msg.content).strip()
        await ctx.send(embed=emb("📜 Quote Saved", f"> {clean_content}\n— **{replied_msg.author.display_name}**", C_GREEN))
    else:
        # Display a random saved quote
        if not guild_quotes:
            await ctx.send(embed=emb("📜 Quote", "No saved quotes yet. Reply to a message with `!quote` to save one.", C_GREY))
            return

        entry = random.choice(guild_quotes)
        clean_content = re.sub(r'<@!?\d+>', '', entry["content"]).strip()
        await ctx.send(f"> {clean_content}\n— **{entry['author']}**")


@bot.command(name="searchquote", aliases=["quotesearch"])
async def cmd_searchquote(ctx: commands.Context):
    """Find a funny and controversial message from recent chat history.

    Usage:
    - !searchquote — search current channel
    - !searchquote #channel — search specific channel
    - !searchquote @user — search quotes from user in current channel
    - !searchquote #channel @user — search quotes from user in specific channel
    """
    await ctx.typing()
    global quote_log

    try:
        # Parse arguments (could be channel, user, or both)
        target_channel = ctx.channel
        target_user = None

        if ctx.message.channel_mentions:
            target_channel = ctx.message.channel_mentions[0]
        if ctx.message.mentions:
            target_user = ctx.message.mentions[0]

        # Fetch ALL messages from entire history
        all_messages = []
        async for msg in target_channel.history():
            # Filter: no bot messages, no commands, reasonable length, no URLs
            if msg.author == bot.user or msg.content.startswith("!") or "http" in msg.content.lower():
                continue
            if len(msg.content) < 10 or len(msg.content) > 500:
                continue
            # Filter by user if specified
            if target_user and msg.author.id != target_user.id:
                continue
            # Skip if already in recent quotes log
            if msg.content in quote_log:
                continue
            # Skip if message is only mentions
            clean_content = re.sub(r'<@!?\d+>', '', msg.content).strip()
            if not clean_content:
                continue
            all_messages.append({
                "author": msg.author.display_name,
                "content": msg.content,
            })

        if not all_messages:
            await ctx.send(embed=emb("📜 Quote", "No messages found to quote.", C_GREY))
            return

        # Split messages: 100 with "fuck"/"ass"/"bitch"/"gay", 900 random
        spicy_keywords = {"fuck", "ass", "bitch", "gay"}
        spicy_msgs = [m for m in all_messages if any(kw in m["content"].lower() for kw in spicy_keywords)]
        regular_msgs = [m for m in all_messages if m not in spicy_msgs]

        # Sample: up to 100 from spicy, up to 900 from regular
        spicy_sample = spicy_msgs[:100]  # Take up to 100 spicy messages
        regular_sample = random.sample(regular_msgs, min(900, len(regular_msgs))) if regular_msgs else []
        messages = spicy_sample + regular_sample

        # Use AI to rank messages by entertainment/volatility value
        # Show a sample of messages and ask AI to pick the best one
        prompt = f"""Rank these {len(messages)} chat messages by how entertaining and funny they are. Consider:
- Absurd or ridiculous claims that are genuinely funny (good)
- Self-aware humor or witty comebacks (good)
- Unexpected punchlines or plot twists (good)
- Strong emotional language paired with humor (good)
- Spicy/bold takes that land well (good)
- Messages that made people laugh or got strong reactions (good)
- Absurd/funny/goofy statements that would make people laugh (good)
- Inflammatory statements with "gay" in them are usually decently funny jokes (good)
- Random absurd/funny/goofy statements without context (bad if not funny)
- Generic statements about being angry/sad (bad)
- Bland or neutral messages (bad)

Example: "I don't even know" = 2/10 (bland)
Example: "He said he's gay and he wants you to be his little fuck boy." = 9/10 (bold, emotional, absurd, would get laughs)
Example: "I hate everyone" = 1/10 (just venting, no humor)

From this sample, pick the SINGLE message with the HIGHEST entertainment/humor value:

Messages:
{chr(10).join(f'{i+1}. [{m["author"]}]: {m["content"]}' for i, m in enumerate(messages))}

Respond with ONLY the message number of the highest-ranked message (just the number)."""

        system_prompt = "You are an expert at finding genuinely funny and entertaining messages. Prioritize absurdity, wit, and unexpected humor over just strong language. Pick messages that would make people laugh when quoted."

        placeholder = await ctx.send("🔍 Searching for quotes...")
        typing_task = asyncio.create_task(keep_typing(ctx.channel))

        try:
            async with aiohttp.ClientSession() as session:
                guild_id = ctx.guild.id if ctx.guild else None
                model = get_guild_ask_model(guild_id) if guild_id else OLLAMA_MODEL
                response = await stream_ollama(
                    session,
                    [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
                    placeholder,
                    model=model
                )

            # Parse the response to get message number
            try:
                msg_num = int(response.strip()) - 1
                if msg_num < 0 or msg_num >= len(messages):
                    msg_num = random.randint(0, len(messages) - 1)
            except ValueError:
                msg_num = random.randint(0, len(messages) - 1)

            selected = messages[msg_num]
            # Add to quote log to prevent reuse
            quote_log.append(selected['content'])
            save_quote_log(quote_log)

            # Remove mentions from displayed quote
            clean_content = re.sub(r'<@!?\d+>', '', selected['content']).strip()

            await placeholder.delete()
            await ctx.send(f"> {clean_content}\n— **{selected['author']}**")

        except aiohttp.ClientError as e:
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Ollama offline: {e}")
            await placeholder.edit(content="", embed=emb("", "The AI is currently offline", C_RED))
        except Exception as e:
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"{type(e).__name__}: {e}")
            await placeholder.edit(content=f"⚠️ {e}")
        finally:
            typing_task.cancel()

    except Exception as e:
        await ctx.send(embed=emb("❌ Error", f"Failed to find quote: {str(e)}", C_RED))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Dog & Cat
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="dog")
async def cmd_dog(ctx: commands.Context):
    await ctx.typing()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://dog.ceo/api/breeds/image/random", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await ctx.send(embed=emb("🐕 Dog", "Failed to fetch dog image.", C_RED))
                    return
                data = await resp.json()
                if data.get("status") != "success" or not data.get("message"):
                    await ctx.send(embed=emb("🐕 Dog", "No dog image available.", C_GREY))
                    return
                embed = discord.Embed(title="🐕 Random Dog", color=C_BLUE)
                embed.set_image(url=data["message"])
                await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=emb("🐕 Dog", f"Failed to fetch: {e}", C_RED))


@bot.command(name="cat")
async def cmd_cat(ctx: commands.Context):
    await ctx.typing()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.thecatapi.com/v1/images/search", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    await ctx.send(embed=emb("🐱 Cat", "Failed to fetch cat image.", C_RED))
                    return
                data = await resp.json()
                if not data or not isinstance(data, list) or not data[0].get("url"):
                    await ctx.send(embed=emb("🐱 Cat", "No cat image available.", C_GREY))
                    return
                embed = discord.Embed(title="🐱 Random Cat", color=C_BLUE)
                embed.set_image(url=data[0]["url"])
                await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(embed=emb("🐱 Cat", f"Failed to fetch: {e}", C_RED))


# ─────────────────────────────────────────────────────────────────────────────
# Lottery System
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="lottery")
async def cmd_lottery(ctx: commands.Context, n: str = None):
    uid = ctx.author.id
    _ensure_user(uid)

    # Check if lottery channel is configured
    if ctx.guild is None:
        await ctx.send(embed=emb("🎰 Lottery", "Lottery only works in servers.", C_RED))
        return

    cfg = get_guild_cfg(ctx.guild.id)
    lottery_channel_id = cfg.get("lottery_channel")
    if not lottery_channel_id:
        await ctx.send(embed=emb("🎰 Lottery Disabled", "Lottery channel not configured.", C_GREY))
        return

    lottery = load_lottery(ctx.guild.id)

    if n is None:
        # Show lottery info
        pool = lottery.get("prize_pool", 0)
        players_dict = lottery.get("players", {})
        user_tickets = int(players_dict.get(str(uid), 0))

        # Calculate next Saturday 6pm CT (handles CST/CDT automatically)
        ct = ZoneInfo("America/Chicago")
        now_cst = datetime.datetime.now(datetime.timezone.utc).astimezone(ct)
        days_until_saturday = (5 - now_cst.weekday()) % 7
        if days_until_saturday == 0:
            days_until_saturday = 7
        next_saturday = now_cst + datetime.timedelta(days=days_until_saturday)
        next_saturday = next_saturday.replace(hour=18, minute=0, second=0, microsecond=0)
        timestamp = int(next_saturday.timestamp())

        info = f"**Prize Pool:** {pool:,} 🪙 (+1,000 🪙 per player)\n"
        info += f"**Players:** {len(players_dict)}\n"
        info += f"**Ticket Cost:** 10 🪙 for 1 🎟️\n\n"
        info += f"**Your Tickets:** {user_tickets}\n"
        info += f"Use `!lottery <n>` to buy more tickets"

        await ctx.send(embed=emb(f"🎰 Current Lottery • ends <t:{timestamp}:R>", info, C_PURPLE))
        return

    try:
        tickets = int(n)
        assert tickets > 0
    except (ValueError, AssertionError):
        await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a positive number.", C_RED))
        return

    cost = tickets * 10
    if not deduct_balance(uid, cost):
        await ctx.send(embed=emb("💸 Insufficient Funds", f"Need {cost} 🪙. Balance: {get_balance(uid)} 🪙", C_RED))
        return

    # Add to lottery
    players = lottery.setdefault("players", {})
    was_new_player = str(uid) not in players

    players[str(uid)] = players.get(str(uid), 0) + tickets
    lottery.setdefault("prize_pool", 0)
    lottery["prize_pool"] += cost
    if was_new_player:
        lottery["prize_pool"] += 1000

    save_lottery(ctx.guild.id, lottery)

    bonus_msg = "(+1,000 bonus as new player)" if was_new_player else ""

    # Calculate when lottery ends
    ct = ZoneInfo("America/Chicago")
    now_cst = datetime.datetime.now(datetime.timezone.utc).astimezone(ct)
    days_until_saturday = (5 - now_cst.weekday()) % 7
    if days_until_saturday == 0:
        days_until_saturday = 7
    next_saturday = now_cst + datetime.timedelta(days=days_until_saturday)
    next_saturday = next_saturday.replace(hour=18, minute=0, second=0, microsecond=0)
    timestamp = int(next_saturday.timestamp())

    embed_msg = emb(
        "🎰 Tickets Purchased",
        f"Bought **{tickets}** 🎟️ for **{cost} 🪙**\n\n"
        f"**Prize Pool:** {lottery['prize_pool']:,} 🪙 {bonus_msg}\n"
        f"**Your Tickets:** {players[str(uid)]}\n"
        f"**Total Players:** {len(players)}\n"
        f"**Ends:** <t:{timestamp}:R>",
        C_GREEN
    )
    await ctx.send(embed=embed_msg)


@tasks.loop(minutes=1)
async def lottery_scheduler():
    """Check every minute if it's Saturday 6pm CST for lottery tasks."""
    now = datetime.datetime.now()
    # Saturday = 5 (Monday = 0)
    is_saturday = now.weekday() == 5

    if not is_saturday:
        return

    # Get all guilds with lottery channel configured
    for guild in bot.guilds:
        cfg = get_guild_cfg(guild.id)
        lottery_channel_id = cfg.get("lottery_channel")
        if not lottery_channel_id:
            continue

        try:
            channel = await bot.fetch_channel(lottery_channel_id)
        except:
            continue

        # Saturday 6pm CST - post results and start new lottery
        if now.hour == 18 and now.minute == 0:
            lottery = load_lottery(guild.id)
            current_week = now.isocalendar()[1]
            last_posted = lottery.get("last_posted_week", 0)

            if current_week != last_posted:
                # Post results
                pool = lottery.get("prize_pool", 0)
                players = lottery.get("players", {})

                if players and pool > 0:
                    winner_id = random.choice(list(players.keys()))
                    winner = await bot.fetch_user(int(winner_id))
                    add_balance(int(winner_id), pool)

                    embed = discord.Embed(title="🎰 Lottery Results", color=C_GOLD)
                    embed.description = (
                        f"**Winner:** {winner.mention}\n"
                        f"**Prize:** {pool:,} 🪙\n"
                        f"**Players:** {len(players)}\n"
                        f"**Tickets Sold:** {sum(players.values())}"
                    )
                    await channel.send(embed=embed)

                # Reset lottery immediately for next week
                lottery = {"prize_pool": 2000, "players": {}, "last_posted_week": current_week}
                drain_bot_balance_into_lottery(lottery, guild.id)
                save_lottery(guild.id, lottery)

                await announce_new_lottery(channel, lottery["prize_pool"], now)


@tasks.loop(minutes=1)
async def scratchoff_scheduler():
    """Reset daily scratchoff counts at 5am every day."""
    now = datetime.datetime.now()
    if now.hour != 5 or now.minute != 0:
        return
    today = datetime.date.today().isoformat()
    if economy.get("last_daily_reset") != today:
        do_daily_reset()


@bot.event
async def on_ready():
    """Check on startup if lottery results need posting and resetting."""
    logging.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if ACTIVE_CHANNEL_IDS:
        logging.info(f"Listening in channels: {ACTIVE_CHANNEL_IDS}")
    else:
        logging.info("Listening in all channels")

    for thread_id, messages in load_fanfic_histories().items():
        fanfic_thread_ids.add(thread_id)
        channel_histories[thread_id].extend(messages)
    fanfic_owners.update(load_fanfic_owners())
    saved_roleplays, saved_rp_histories = load_roleplay_state()
    active_roleplays.update(saved_roleplays)
    roleplay_histories.update(saved_rp_histories)

    if not lottery_scheduler.is_running():
        lottery_scheduler.start()
    if not scratchoff_scheduler.is_running():
        scratchoff_scheduler.start()

    restart_info = _load_json(RESTART_MSG_FILE, None)
    if restart_info:
        os.remove(RESTART_MSG_FILE)
        try:
            channel = await bot.fetch_channel(restart_info["channel_id"])
            msg = await channel.fetch_message(restart_info["message_id"])
            await msg.edit(embed=emb("✅ Restarted", "Bot has restarted.", 0x2ecc71))
        except Exception as e:
            logging.warning(f"[RESTART] Failed to edit restart message: {e}")

    # If it's past 5am and the scratchoff reset hasn't happened today, do it now
    now = datetime.datetime.now()
    today = datetime.date.today().isoformat()
    if now.hour >= 5 and economy.get("last_daily_reset") != today:
        do_daily_reset()

    # Check if results need posting or lottery needs resetting
    now = datetime.datetime.now()
    current_week = now.isocalendar()[1]

    # Initialize/reset lottery for each guild
    await asyncio.sleep(1)  # Wait for guilds to fully load
    for guild in bot.guilds:
        cfg = get_guild_cfg(guild.id)
        lottery_channel_id = cfg.get("lottery_channel")
        if not lottery_channel_id:
            continue

        lottery = load_lottery(guild.id)
        last_posted = lottery.get("last_posted_week", 0)

        # If not Saturday, initialize if no active lottery
        if now.weekday() != 5 and last_posted != current_week:
            lottery = {"prize_pool": 2000, "players": {}, "last_posted_week": current_week}
            drain_bot_balance_into_lottery(lottery, guild.id)
            save_lottery(guild.id, lottery)

            try:
                channel = await bot.fetch_channel(lottery_channel_id)
                await announce_new_lottery(channel, lottery["prize_pool"], now)
                logging.info(f"[LOTTERY] Initialized lottery for {guild.name}")
            except Exception as e:
                logging.error(f"[LOTTERY] Error initializing lottery in {guild.name}: {e}")

        # If Saturday 6pm+, always reset and announce
        elif now.weekday() == 5 and now.hour >= 18:
            try:
                channel = await bot.fetch_channel(lottery_channel_id)

                # Only post results if there were players and we haven't posted yet this week
                if last_posted != current_week:
                    pool = lottery.get("prize_pool", 0)
                    players = lottery.get("players", {})

                    if players and pool > 0:
                        winner_id = random.choice(list(players.keys()))
                        winner = await bot.fetch_user(int(winner_id))
                        add_balance(int(winner_id), pool)

                        embed = discord.Embed(title="🎰 Lottery Results", color=C_GOLD)
                        embed.description = (
                            f"**Winner:** {winner.mention}\n"
                            f"**Prize:** {pool:,} 🪙\n"
                            f"**Players:** {len(players)}\n"
                            f"**Tickets Sold:** {sum(players.values())}"
                        )
                        await channel.send(embed=embed)

                # Always reset lottery for new week on Saturday 6pm+
                lottery = {"prize_pool": 2000, "players": {}, "last_posted_week": current_week}
                drain_bot_balance_into_lottery(lottery, guild.id)
                save_lottery(guild.id, lottery)

                await announce_new_lottery(channel, lottery["prize_pool"], now)
                logging.info(f"[LOTTERY] Reset lottery for {guild.name}")
            except Exception as e:
                logging.error(f"[LOTTERY] Error resetting lottery in {guild.name}: {e}")

    # Clean up any ephemeral bot messages that weren't deleted before last shutdown.
    records = _load_json(EPHEMERAL_MSG_FILE, [])
    if records:
        os.remove(EPHEMERAL_MSG_FILE)
        deleted = 0
        for record in records:
            try:
                channel = await bot.fetch_channel(record["channel_id"])
                msg = await channel.fetch_message(record["message_id"])
                await msg.delete()
                deleted += 1
            except (discord.NotFound, discord.Forbidden):
                pass
            except Exception as e:
                print(f"[STARTUP] Failed to delete ephemeral message: {e}")
        if deleted:
            print(f"[STARTUP] Cleaned up {deleted} leftover ephemeral message(s).")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    bot.run(DISCORD_TOKEN)
