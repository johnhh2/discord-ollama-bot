import os
from dotenv import load_dotenv

# Load .env only in dev (not in Docker)
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
# `or` fallbacks (not getenv defaults): docker-compose exports these as empty
# strings when unset in the stack, and int("") would crash-loop the container.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL") or "dolphin3:8b"
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT") or "You are a helpful assistant."
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT") or "20")
NSFW_API_URL = os.getenv("NSFW_API_URL", "")
NSFW_API_KEY = os.getenv("NSFW_API_KEY")
NSFW_API_USER_ID = os.getenv("NSFW_API_USER_ID")
RACE_TRACK_LEN = 20

# Minecraft Bedrock server status (optional; empty host disables the feature).
# MC_SERVER_HOST must be the server's EXTERNAL address (DDNS name or WAN IP)
# so !mc latency measures the internet-facing route players actually take,
# not a ~0ms same-host container hop. Requires router NAT-loopback; fall back
# to host.docker.internal if yours lacks it (latency is then local-only).
# The `or` fallbacks matter: docker-compose exports these as empty strings
# when unset, which os.getenv's default doesn't cover.
MC_SERVER_HOST = os.getenv("MC_SERVER_HOST", "")
MC_SERVER_PORT = int(os.getenv("MC_SERVER_PORT") or "19132")
MC_POLL_SECONDS = int(os.getenv("MC_POLL_SECONDS") or "60")
# Show the server address (host:port) in !mc embeds and monitor alerts.
# Off by default so the public address isn't leaked into Discord channels.
MC_SERVER_SHOW_IP = (os.getenv("MC_SERVER_SHOW_IP") or "").strip().lower() in ("1", "true", "yes", "on")

_raw_channels = os.getenv("ACTIVE_CHANNEL_IDS", "")
ACTIVE_CHANNEL_IDS = (
    {int(cid.strip()) for cid in _raw_channels.split(",") if cid.strip()}
    if _raw_channels.strip()
    else set()
)

COMMAND_PERMS_FILE = "src/command_perms.json"
RIDDLES_FILE = "assets/riddles.csv"

PUZZLE_REWARDS = {
    "easy":   10,
    "medium": 20,
    "hard":   35,
    "extreme": 50,
}

# Slot machine configuration
SLOT_REEL = (
    ["🍒"] * 7 +
    ["🍋"] * 5 +
    ["🔔"] * 4 +
    ["🎰"] * 3 +
    ["7️⃣"] * 1 +
    ["⬛"] * 4
)
SLOT_JACKPOT_SEED = 5_000
SLOT_JACKPOT_CONTRIB = 0.02
SLOT_HOUSE_CHANCE = 0.05

INITIAL_BOT_ADMIN_IDS = [
    int(uid.strip())
    for uid in os.getenv("BOT_ADMIN_IDS", "").split(",")
    if uid.strip().isdigit()
]

# Scratchoff lottery configuration
SCRATCH_SYMBOLS = ["🍒", "🍋", "🍇", "🍊"]
SCRATCHOFF_MAX_DAILY = 3
SCRATCHOFF_PAYOUTS = {1: 100, 2: 1000, 3: 10000, 4: 100000}

# Soundboard rate-limiting
SOUNDBOARD_WINDOW_SECS = 10.0

# Lifetime of an invite minted by !invitelink. Previously max_age=0 —
# permanent, unlimited-use, one more created on every invocation and never
# revoked. A vanity URL, where the guild has one, is used unchanged.
SERVER_INVITE_MAX_AGE_SECS = 86_400  # 24 hours
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
HANGMAN_BASE_REWARD     = 60
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
SHOP_NICKNAME_OTHER_COST  = 20_000
SHOP_ROLE_CREATE_COST     = 10_000
SHOP_ROLE_ASSIGN_COST     = 15_000
SHOP_ROLE_REMOVE_COST     = 15_000
SHOP_ROLE_DELETE_COST     = 75_000
SHOP_ROLE_MOVE_COST       = 20_000
SHOP_ROLECOLOR_COST       = 10_000
SHOP_ROLECHANNEL_COST     = 50_000
SHOP_RENAME_COST          = 30_000
SHOP_LOCK_COST            = 100_000
SHOP_CHANNEL_COST         = 40_000
SHOP_CHANNEL_DELETE_COST  = 75_000
SHOP_INSURANCE_COST       = 1_000  # per day (prepay or subscription renewal)
SHOP_TAX_COST             = 1_000
SHOP_MOCK_COST            = 1_500
SHOP_RAGEBAIT_COST        = 2_500
SHOP_MUTE_COST            = 5_000
SHOP_CURSE_COST           = 10_000
SHOP_UNOREVERSE_COST      = 10_000
SHOP_SPELLCHECK_COST      = 10_000  # per day
SHOP_XP_COST_PER_XP       = 100     # coins per XP for !shop buyxp

# Artifact costs (see src/artifacts.py for the catalog)
ARTIFACT_SLOTS_BLANK_COST   = 15_000
ARTIFACT_CHESSTHREATS_COST  = 25_000
ARTIFACT_BAIL_DISCOUNT_COST = 50_000
ARTIFACT_EXTRA_SCRATCH_COST = 75_000
ARTIFACT_STEAL_BOOST_COST   = 100_000
ARTIFACT_CRIME_CATCH_COST   = 200_000
ARTIFACT_STREAK_SCRATCH_COST = 250_000
ARTIFACT_PROPERTY_CAP_COST   = 300_000
ARTIFACT_PROPERTY_BOOST_COST = 1_000_000

# Shop effect parameters
SHOP_INSURANCE_DURATION_SECS = 86_400  # 24 hours (one prepaid/renewed day)
SHOP_INSURANCE_MAX_DAYS      = 30      # cap on total remaining prepaid coverage
SHOP_MOCK_MESSAGES           = 5
SHOP_RAGEBAIT_MESSAGES       = 4       # remaining after initial AI send
SHOP_CURSE_MESSAGES          = 5
SHOP_MUTE_MINUTES            = 5
SHOP_TAX_PER_MESSAGE         = 10
SHOP_TAX_DURATION_SECS       = 86_400  # 24 hours
SHOP_SPELLCHECK_DURATION_SECS = 86_400  # 24 hours per purchased day

# Bounty (!shop bounty / !bounty) parameters
BOUNTY_MIN_AMOUNT        = 1_000      # smallest bounty an author may post
BOUNTY_CLAIM_DURATION_SECS   = 7 * 86_400   # author has 1 week to accept/reject a claim
BOUNTY_CONTEST_DURATION_SECS = 3 * 86_400   # rejected claimant has 3 days to contest
BOUNTY_POLL_DURATION_SECS    = 3 * 86_400   # @everyone contest poll runs for 3 days
# Poll payout scaling: <50% yes → 0; 50%→50% payout, ramping linearly up to
# ≥66.666% yes → 100% payout.
BOUNTY_POLL_MIN_RATIO    = 0.5            # below this, no payout
BOUNTY_POLL_FULL_RATIO   = 2.0 / 3.0      # at/above this, full payout
# Fraction of escrow refunded to the author when THEY walk away — a self-cancel
# (only allowed on bounties with no deadline) or an open bounty hitting its
# optional expiration. The remaining 10% is a house cut. Claim failures
# (reject/contest-drop/poll-no) leave the bounty open and refund nothing.
BOUNTY_AUTHOR_REFUND_FRACTION = 0.9
# Flat reward minted to each eligible voter on a contest poll (paid once per
# voter when the poll is tallied, regardless of outcome). Author and claimant
# are excluded. This is freshly minted, not drawn from the escrow.
BOUNTY_POLL_VOTER_REWARD = 100

# Ephemeral message auto-delete timeout
EPHEMERAL_DELETE_AFTER = 60
