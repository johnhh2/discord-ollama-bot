import os
from dotenv import load_dotenv

# Dev only: .env is kept out of the Docker image, so this is a no-op there.
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
# `or` fallbacks (not getenv defaults): docker-compose exports these as empty
# strings when unset in the stack, and int("") would crash-loop the container.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL") or "dolphin3:8b"
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT") or "You are a helpful assistant."
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT") or "20")
# Ollama's own default (4096) can't hold the command reference (~6k tokens) every
# !ask / @mention answer carries plus the channel history.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX") or "16384")
NSFW_API_URL = os.getenv("NSFW_API_URL", "")
NSFW_API_KEY = os.getenv("NSFW_API_KEY")
NSFW_API_USER_ID = os.getenv("NSFW_API_USER_ID")
RACE_TRACK_LEN = 20

# Minecraft Bedrock server status (optional; empty host disables the feature).
# MC_SERVER_HOST must be the server's EXTERNAL address (DDNS name or WAN IP)
# so !mc latency measures the internet-facing route players actually take,
# not a ~0ms same-host container hop. Requires router NAT-loopback; fall back
# to host.docker.internal if yours lacks it (latency is then local-only).
# `or` fallbacks for the same empty-string reason as above.
MC_SERVER_HOST = os.getenv("MC_SERVER_HOST", "")
MC_SERVER_PORT = int(os.getenv("MC_SERVER_PORT") or "19132")
MC_POLL_SECONDS = int(os.getenv("MC_POLL_SECONDS") or "60")
# Show the server address (host:port) in !mc embeds and monitor alerts.
# Off by default so the public address isn't leaked into Discord channels.
MC_SERVER_SHOW_IP = (os.getenv("MC_SERVER_SHOW_IP") or "").strip().lower() in ("1", "true", "yes", "on")
# Bedrock console for the block shop (!mc shop): the itzg image's SSH remote
# console (ENABLE_SSH=true on the bedrock container, port 2222 inside it,
# password = its RCON_PASSWORD). Host is the bedrock container as the bot's
# container sees it, not the public address. Both must be set to enable it.
MC_CONSOLE_HOST = os.getenv("MC_CONSOLE_HOST", "")
MC_CONSOLE_PORT = int(os.getenv("MC_CONSOLE_PORT") or "2222")
MC_CONSOLE_PASSWORD = os.getenv("MC_CONSOLE_PASSWORD", "")

# V Rising / Vintage Story server notices (optional; empty host disables each).
# V Rising is polled on its Steam query port, not the game port; that needs
# ListOnSteam, so with VRISING_RCON_PORT set it's instead online-only via a TCP
# connect to its RCON port (no password needed). Vintage Story's
# version comes from the public master list, matched on this host's address,
# so use the address the server advertises (DDNS name or WAN IP).
VRISING_SERVER_HOST = os.getenv("VRISING_SERVER_HOST", "")
VRISING_QUERY_PORT = int(os.getenv("VRISING_QUERY_PORT") or "9877")
VRISING_RCON_PORT = int(os.getenv("VRISING_RCON_PORT") or "0")
VINTAGESTORY_SERVER_HOST = os.getenv("VINTAGESTORY_SERVER_HOST", "")
VINTAGESTORY_SERVER_PORT = int(os.getenv("VINTAGESTORY_SERVER_PORT") or "42420")

# Voice announcements (!announce): a Wyoming text-to-speech server (Piper, for
# one) as the bot's container sees it. Unset leaves the command off. The voice
# is one of the server's voice names; empty takes its default.
WYOMING_TTS_HOST = os.getenv("WYOMING_TTS_HOST", "")
WYOMING_TTS_PORT = int(os.getenv("WYOMING_TTS_PORT") or "10200")
WYOMING_TTS_VOICE = os.getenv("WYOMING_TTS_VOICE", "")
# The yt-dlp binary !adminannounce runs for YouTube links; the image sets it to
# the self-updating copy in /opt/ytdlp. Empty means PATH lookup.
YTDLP_PATH = os.getenv("YTDLP_PATH", "") or "yt-dlp"
# Where the standalone yt-dlp unpacks itself on each start (its TMPDIR). The
# container's /tmp is a noexec tmpfs, so the image points this at a second,
# exec-allowed one. Empty leaves the process's own temp dir.
YTDLP_TMPDIR = os.getenv("YTDLP_TMPDIR", "")
ANNOUNCE_MAX_CHARS = 500  # synthesis time and the modal's text box grow with it
# An attached audio file instead of text: Discord's own upload cap, and the
# clip is cut at ANNOUNCE_MAX_SECS (decoded PCM is ~190 KB a second).
ANNOUNCE_MAX_FILE_BYTES = 10 * 1024 * 1024
ANNOUNCE_MAX_SECS = 600  # ~115 MB decoded, well under the container's 512 MB limit
# `x<N>` repeats: at most this many, and a message's length times N can't
# exceed ANNOUNCE_MAX_TOTAL_CHARS so a repeat can't hold the channel longer
# than a long message would. The paid !announce charges per character of
# that product, so this cap is also its price ceiling.
ANNOUNCE_MAX_REPEATS = 50
ANNOUNCE_MAX_TOTAL_CHARS = 1000

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
SLOT_JACKPOT_CONTRIB_DIVISOR = 150   # pool gains bet // 150 per spin (~0.67%, rounded down, no minimum)
SLOT_JACKPOT_CAP = 100_000           # contributions stop once the pool reaches this
SLOT_HOUSE_CHANCE = 0.055           # chance of a house spin: three distinct symbols, never a triple

# Prize pool every fresh monthly lottery starts with (the guild-house drain
# and per-player bonuses stack on top).
LOTTERY_SEED_POOL = 50_000

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

# Lifetime of an invite minted by !invitelink. Never max_age=0: that left a
# permanent, unlimited-use, never-revoked invite behind on every invocation.
# A vanity URL, where the guild has one, is used unchanged.
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
SHOP_TAX_COST             = 1_000
SHOP_MOCK_COST            = 1_500
SHOP_RAGEBAIT_COST        = 2_500
SHOP_CURSE_COST           = 10_000
SHOP_UNOREVERSE_COST      = 10_000
SHOP_ANNOUNCE_COST_PER_CHAR = 100   # !shop announce: per character, times the x<N> repeats…
SHOP_ANNOUNCE_MAX_COST    = 10_000  # …but never more than this for one announcement
SHOP_XP_COST_PER_XP       = 100     # coins per XP for !shop buyxp
# Catch-up discount: XP that only brings you up to your best level in another
# server is this much cheaper. Levels above that best are full price.
SHOP_XP_CATCHUP_DISCOUNT_PCT = 50
SHOP_XP_CATCHUP_COST_PER_XP  = SHOP_XP_COST_PER_XP * (100 - SHOP_XP_CATCHUP_DISCOUNT_PCT) // 100

# Artifact costs (see src/artifacts.py for the catalog)
ARTIFACT_SLOTS_BLANK_COST   = 15_000
ARTIFACT_CHESSTHREATS_COST  = 25_000
ARTIFACT_BAIL_DISCOUNT_COST = 50_000
ARTIFACT_EXTRA_SCRATCH_COST = 75_000
ARTIFACT_STEAL_BOOST_COST   = 100_000
ARTIFACT_CRIME_CATCH_COST   = 200_000
ARTIFACT_STREAK_SCRATCH_COST = 300_000
ARTIFACT_HEIST_PARTNER_COST  = 400_000
ARTIFACT_UPGRADE_DISCOUNT_COST = 500_000
# Retired artifacts (off sale): the price their owners paid, which sets the
# half-price sell-back.
ARTIFACT_PROPERTY_CAP_COST   = 150_000
ARTIFACT_SAVINGS_BOOST_COST  = 200_000
ARTIFACT_PROPERTY_BOOST_COST = 500_000

# Savings interest, compound per day. The base rate is what every piggy bank
# earns; the boosted rate is what the savings artifact pays from the moment
# it's bought (src/economy.py savings_growth, src/artifacts.py).
SAVINGS_DAILY_MULT          = 1.006
ARTIFACT_SAVINGS_DAILY_MULT = 1.008

# Shop effect parameters
#
# Insurance (`!shop insurance`) comes in three types, each its own policy and
# subscription. Personal: crime (steal/mug/bankheist) still goes through, and
# the insurer refunds `refund_pct` of each loss to the victim's wallet —
# minted, not taken from the thief — up to `refund_cap` per incident. Social:
# one tier, blocks the shop effects aimed at another user
# (economy.INSURANCE_PROTECTS) outright. Property: refunds repair bills after
# a property theft (src/property_theft.py); PROPERTY_INSURANCE_LIVE takes it
# off sale. Tier keys are what users type; order is
# cheapest → dearest (the tier picker renders in this order).
#
# Personal and property premiums scale with what crime can reach
# (economy.insurance_premium): `rate_bp` basis points a day of the exposure,
# never below `cost`. Personal exposure stops at `exposure_cap`, the wallet +
# savings where a 20% heist hits refund_cap, since more wealth buys no more
# cover. Property has no cap: each deed is robbed on its own.
SHOP_INSURANCE_TIERS = {
    "basic":    {"cost": 1_000, "refund_pct": 50,  "refund_cap": 100_000, "rate_bp": 40,  "exposure_cap": 1_000_000},
    "standard": {"cost": 3_000, "refund_pct": 75,  "refund_cap": 200_000, "rate_bp": 60,  "exposure_cap": 1_333_333},
    "premium":  {"cost": 6_000, "refund_pct": 100, "refund_cap": 400_000, "rate_bp": 100, "exposure_cap": 2_000_000},
}
SHOP_SOCIAL_INSURANCE_TIERS = {
    "social": {"cost": 1_000},
}
SHOP_PROPERTY_INSURANCE_TIERS = {
    "basic":    {"cost": 2_000,  "refund_pct": 50,  "refund_cap": 30_000,  "rate_bp": 10},
    "standard": {"cost": 5_000,  "refund_pct": 75,  "refund_cap": 60_000,  "rate_bp": 15},
    "premium":  {"cost": 10_000, "refund_pct": 100, "refund_cap": 120_000, "rate_bp": 25},
}
# Each premium adds this share of the policy's refunds over the last
# INSURANCE_CLAIMS_DAYS gameplay days, spread per day, so a victim robbed
# daily (or a friend farming refunds) pays back part of the minted coins.
INSURANCE_CLAIMS_DAYS = 7
INSURANCE_SURCHARGE_PCT = 50
INSURANCE_TYPES = {
    "personal": {"label": "Personal", "emoji": "🛡️", "tiers": SHOP_INSURANCE_TIERS, "default": "basic"},
    "social":   {"label": "Social", "emoji": "🎭", "tiers": SHOP_SOCIAL_INSURANCE_TIERS, "default": "social"},
    "property": {"label": "Property", "emoji": "🏠", "tiers": SHOP_PROPERTY_INSURANCE_TIERS, "default": "basic"},
}
PROPERTY_INSURANCE_LIVE = True
# Personal tier every pre-tier policy and subscription was backfilled to
# (migration 0065) and the fallback when a stored tier is missing or unknown.
SHOP_INSURANCE_DEFAULT_TIER  = "basic"
SHOP_INSURANCE_DURATION_SECS = 86_400  # 24 hours (one renewed day)
SHOP_MOCK_MESSAGES           = 5
SHOP_RAGEBAIT_MESSAGES       = 4       # remaining after initial AI send
SHOP_CURSE_MESSAGES          = 5
SHOP_TAX_PER_MESSAGE         = 10
SHOP_TAX_DURATION_SECS       = 86_400  # 24 hours
# What a server admin's `!event` drops may pay out per guild per week
# (Monday 5am CT to Monday); bot admins' events are unlimited.
EVENT_WEEKLY_BUDGET          = 10_000

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

# Ephemeral message auto-delete timeout (seconds)
EPHEMERAL_DELETE_AFTER = 120
