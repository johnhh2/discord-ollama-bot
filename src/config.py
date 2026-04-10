import os
from dotenv import load_dotenv

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
RIDDLES_FILE = "data/riddles_crawsome.csv"
FANFIC_HISTORIES_FILE = "data/fanfic_histories.json"
FANFIC_OWNERS_FILE = "data/fanfic_owners.json"
ROLEPLAY_STATE_FILE = "data/roleplay_state.json"
SCRATCHOFF_FILE = "data/scratchoff.json"

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
SCRATCH_SYMBOLS = ["🍒", "🍋", "🍇", "🍊"]
SCRATCHOFF_MAX_DAILY = 3
SCRATCHOFF_PAYOUTS = {1: 100, 2: 1000, 3: 10000, 4: 100000}

# Soundboard rate-limiting
SOUNDBOARD_WINDOW_SECS = 3.0
SOUNDBOARD_MAX_SOUNDS  = 5

# Economy
DAILY_REWARD = 200
DAILY_RESET_HOUR = 5  # 5am CT

# Slots multipliers & limits
SLOT_MIN_BET = 25
SLOT_MULT_JACKPOT = 75
SLOT_MULT_3BAR    = 15
SLOT_MULT_3BELL   = 7
SLOT_MULT_3LEMON  = 4
SLOT_MULT_3CHERRY = 3
SLOT_MULT_2CHERRY = 2
SLOT_MULT_1CHERRY = 1
SLOT_JACKPOT_BONUS_MIN_BET  = 25    # bet at which jackpot bonus = 1x
SLOT_JACKPOT_BONUS_MAX_BET  = 1000  # bet at which jackpot bonus reaches max
SLOT_JACKPOT_BONUS_MAX_MULT = 4.0

# Hangman
HANGMAN_MAX_WRONG       = 6
HANGMAN_BASE_REWARD     = 10
HANGMAN_LENGTH_OFFSET   = 3
HANGMAN_LENGTH_MULT     = 6
HANGMAN_UNIQUE_MULT     = 3
HANGMAN_RARE_MULT       = 25
HANGMAN_ULTRA_RARE_MULT = 50

# Blackjack
BLACKJACK_NATURAL_MULT = 2.5

# Shop costs
SHOP_NICKNAME_SELF_COST   = 5_000
SHOP_NICKNAME_REMOVE_COST = 2_000
SHOP_NICKNAME_OTHER_COST  = 10_000
SHOP_ROLE_CREATE_COST     = 10_000
SHOP_ROLE_REMOVE_COST     = 2_000
SHOP_ROLE_MOVE_COST       = 20_000
SHOP_ROLECOLOR_COST       = 2_000
SHOP_CHANNEL_COST         = 20_000
SHOP_INSURANCE_COST       = 500
SHOP_SIMP_COST            = 1_000
SHOP_MOCK_COST            = 1_500
SHOP_RAGEBAIT_COST        = 2_500
SHOP_MUTE_COST            = 5_000
SHOP_CURSE_COST           = 10_000

# Shop effect parameters
SHOP_INSURANCE_DURATION_SECS = 86_400  # 24 hours
SHOP_MOCK_MESSAGES           = 5
SHOP_RAGEBAIT_MESSAGES       = 4       # remaining after initial AI send
SHOP_CURSE_MESSAGES          = 5
SHOP_MUTE_MINUTES            = 5
SHOP_SIMP_TAX_PER_MESSAGE    = 10
SHOP_CONCUBINE_DURATION_SECS = 86_400  # 24 hours

# Ephemeral message auto-delete timeout
EPHEMERAL_DELETE_AFTER = 60
