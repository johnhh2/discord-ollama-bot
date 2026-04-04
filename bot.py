import os
import asyncio
import aiohttp
import discord
import json
import time
import random
from discord.ext import commands
from dotenv import load_dotenv
from collections import defaultdict, deque

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "dolphin3:8b")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
RATE_LIMIT_SECONDS = float(os.getenv("RATE_LIMIT_SECONDS", "5.0"))

_raw_channels = os.getenv("ACTIVE_CHANNEL_IDS", "")
ACTIVE_CHANNEL_IDS = (
    {int(cid.strip()) for cid in _raw_channels.split(",") if cid.strip()}
    if _raw_channels.strip()
    else set()
)

CHANNEL_PROMPTS_FILE = "channel_prompts.json"
ECONOMY_FILE = "data/economy.json"
BOT_ROLES_FILE = "data/bot_roles.json"
BOT_ADMINS_FILE = "data/bot_admins.json"
BOT_SETTINGS_FILE = "data/bot_settings.json"
GUILD_SETTINGS_FILE = "data/guild_settings.json"
INSURANCE_FILE = "data/insurance.json"
MODELS_FILE = "data/models.json"
INITIAL_BOT_ADMIN_ID = 139928946044174336


def load_channel_prompts() -> dict[int, str]:
    if os.path.exists(CHANNEL_PROMPTS_FILE):
        with open(CHANNEL_PROMPTS_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def save_channel_prompts(prompts: dict[int, str]):
    with open(CHANNEL_PROMPTS_FILE, "w") as f:
        json.dump({str(k): v for k, v in prompts.items()}, f, indent=2)


def load_economy() -> dict:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(ECONOMY_FILE):
        with open(ECONOMY_FILE) as f:
            return json.load(f)
    return {"users": {}}


def save_economy():
    os.makedirs("data", exist_ok=True)
    with open(ECONOMY_FILE, "w") as f:
        json.dump(economy, f, indent=2)


def load_bot_roles() -> set[int]:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(BOT_ROLES_FILE):
        with open(BOT_ROLES_FILE) as f:
            return set(json.load(f))
    return set()


def save_bot_roles():
    os.makedirs("data", exist_ok=True)
    with open(BOT_ROLES_FILE, "w") as f:
        json.dump(list(bot_roles), f)


def load_bot_admins() -> set[int]:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(BOT_ADMINS_FILE):
        with open(BOT_ADMINS_FILE) as f:
            return set(json.load(f))
    return {INITIAL_BOT_ADMIN_ID}


def save_bot_admins():
    os.makedirs("data", exist_ok=True)
    with open(BOT_ADMINS_FILE, "w") as f:
        json.dump(list(bot_admins), f)


def load_bot_settings() -> dict:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(BOT_SETTINGS_FILE):
        with open(BOT_SETTINGS_FILE) as f:
            return json.load(f)
    return {"vram_text": "16GB"}


def save_bot_settings():
    os.makedirs("data", exist_ok=True)
    with open(BOT_SETTINGS_FILE, "w") as f:
        json.dump(bot_settings, f, indent=2)


def load_guild_settings() -> dict:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(GUILD_SETTINGS_FILE):
        with open(GUILD_SETTINGS_FILE) as f:
            return json.load(f)
    return {}


def save_guild_settings():
    os.makedirs("data", exist_ok=True)
    with open(GUILD_SETTINGS_FILE, "w") as f:
        json.dump(guild_settings, f, indent=2)


def get_guild_cfg(guild_id: int) -> dict:
    key = str(guild_id)
    if key not in guild_settings:
        guild_settings[key] = {}
    return guild_settings[key]


def load_insurance() -> dict:
    os.makedirs("data", exist_ok=True)
    if os.path.exists(INSURANCE_FILE):
        with open(INSURANCE_FILE) as f:
            data = json.load(f)
            now = time.time()
            return {k: v for k, v in data.items() if v.get("expires_at", 0) > now}
    return {}


def save_insurance():
    os.makedirs("data", exist_ok=True)
    with open(INSURANCE_FILE, "w") as f:
        json.dump(insurance, f, indent=2)


def is_insured(uid: int, against: str) -> bool:
    if str(uid) not in insurance:
        return False
    entry = insurance[str(uid)]
    if entry.get("expires_at", 0) <= time.time():
        del insurance[str(uid)]
        save_insurance()
        return False
    return against in entry.get("protected_from", [])


def load_models() -> tuple[str, str]:
    """Load persisted AI models. Returns (ask_model, roleplay_model)."""
    os.makedirs("data", exist_ok=True)
    if os.path.exists(MODELS_FILE):
        with open(MODELS_FILE) as f:
            data = json.load(f)
            return data.get("ask_model", OLLAMA_MODEL), data.get("roleplay_model", OLLAMA_MODEL)
    return OLLAMA_MODEL, OLLAMA_MODEL


def save_models(ask_model: str, roleplay_model: str):
    """Save AI models to disk."""
    os.makedirs("data", exist_ok=True)
    with open(MODELS_FILE, "w") as f:
        json.dump({"ask_model": ask_model, "roleplay_model": roleplay_model}, f, indent=2)


channel_prompts = load_channel_prompts()
economy = load_economy()
bot_roles: set[int] = load_bot_roles()
bot_admins: set[int] = load_bot_admins()
bot_settings: dict = load_bot_settings()
guild_settings: dict = load_guild_settings()
insurance: dict = load_insurance()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ── AI model globals ──────────────────────────────────────────────────────────
current_model, current_roleplay_model = load_models()

# ── Admin state ───────────────────────────────────────────────────────────────
godmode = False

# ── In-memory game state ──────────────────────────────────────────────────────
active_blackjack_games: dict[int, dict] = {}
active_hangman_games: dict[int, dict] = {}
active_roleplays: dict[int, dict] = {}
roleplay_histories: dict[int, list] = {}
active_events: dict[int, dict] = {}     # message_id → {amount, rewarded: set}
active_ragebaits: dict[int, dict] = {} # user_id → {remaining: int, history: list[str]}

# ── Misc state ────────────────────────────────────────────────────────────────
channel_histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
user_last_request: dict[int, float] = {}

# ── Stats ─────────────────────────────────────────────────────────────────────
bot_start_time = time.monotonic()
stats_commands_ran: int = 0
stats_messages_seen: int = 0

# ── Audit log ─────────────────────────────────────────────────────────────────
audit_log: deque = deque(maxlen=20)

# ── Mock state ────────────────────────────────────────────────────────────────
active_mocks: dict[int, dict] = {}  # user_id → {expires_at: float, started_by: int}


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

HANGMAN_WORDS = [
    "python", "discord", "hangman", "wizard", "quantum", "jungle", "oxygen",
    "breeze", "falcon", "sphinx", "goblin", "mystic", "planet", "silver",
    "knight", "dragon", "zombie", "voyage", "castle", "frozen", "marble",
    "mitten", "candle", "fossil", "gravel", "hollow", "jigsaw", "lizard",
    "muffin", "napkin", "oyster", "parrot", "quartz", "riddle", "saddle",
    "tandem", "velvet", "walnut", "yellow", "zipper", "abacus", "bamboo",
    "clover", "dagger", "feline", "goblet", "hermit", "ignite", "jackal",
    "kernel",
]

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


# ── Slots ─────────────────────────────────────────────────────────────────────

SLOT_SYMBOLS = ["🍒", "🍋", "🍊", "🍇", "💎", "🪙"]

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
# Async infrastructure
# ─────────────────────────────────────────────────────────────────────────────

async def keep_typing(channel: discord.abc.Messageable):
    try:
        while True:
            await channel.trigger_typing()
            await asyncio.sleep(8)
    except asyncio.CancelledError:
        pass


async def stream_ollama(
    session: aiohttp.ClientSession,
    messages: list[dict],
    placeholder: discord.Message,
    model: str = None,
) -> str:
    used_model = model or current_model
    payload = {"model": used_model, "messages": messages, "stream": True}

    full_response = ""
    last_edit = 0.0
    EDIT_INTERVAL = 0.8

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


async def respond(
    channel: discord.abc.Messageable,
    user_id: int,
    content: str,
    reply_to: discord.Message,
    system_prompt: str = None,
):
    channel_id = channel.id
    history = channel_histories[channel_id]
    system_prompt = system_prompt or get_system_prompt(channel_id)

    history.append({"role": "user", "content": content})
    messages = [{"role": "system", "content": system_prompt}] + list(history)

    placeholder = await reply_to.reply("...")
    typing_task = asyncio.create_task(keep_typing(channel))

    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(session, messages, placeholder)
        history.append({"role": "assistant", "content": full_response})
        await finalize(placeholder, channel, full_response)
    except aiohttp.ClientError as e:
        history.pop()
        await placeholder.edit(content=f"⚠️ Could not reach Ollama: `{e}`")
    except Exception as e:
        history.pop()
        await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
    finally:
        typing_task.cancel()


async def respond_roleplay(
    channel: discord.abc.Messageable,
    user_id: int,
    content: str,
    reply_to: discord.Message,
):
    rp = active_roleplays[user_id]
    history = roleplay_histories.setdefault(user_id, [])
    system_prompt = (
        f"You are roleplaying as the following character and must stay in character "
        f"for every response, no matter what: {rp['character_prompt']}. "
        f"Never break character or acknowledge that you are an AI."
    )

    history.append({"role": "user", "content": content})
    messages = [{"role": "system", "content": system_prompt}] + history

    placeholder = await reply_to.reply("...")
    typing_task = asyncio.create_task(keep_typing(channel))

    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(
                session, messages, placeholder, model=current_roleplay_model
            )
        history.append({"role": "assistant", "content": full_response})
        await finalize(placeholder, channel, full_response)
    except aiohttp.ClientError as e:
        history.pop()
        await placeholder.edit(content=f"⚠️ Could not reach Ollama: `{e}`")
    except Exception as e:
        history.pop()
        await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
    finally:
        typing_task.cancel()


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
    if ctx.command and ctx.command.name == "settings":
        return True  # always allow !settings so admins can't lock themselves out
    cfg = get_guild_cfg(ctx.guild.id)
    command_channels = cfg.get("command_channels", [])
    if command_channels and ctx.channel.id not in command_channels:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Events
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        print(f"[debug] {error}")
        return
    if isinstance(error, commands.CheckFailure):
        return  # silently ignore — command blocked by channel restriction
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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if ACTIVE_CHANNEL_IDS:
        print(f"Listening in channels: {ACTIVE_CHANNEL_IDS}")
    else:
        print("Listening in all channels")


async def _auto_daily(message: discord.Message):
    """Award daily coins on first interaction of the day. Sends a short message if awarded."""
    uid = message.author.id
    _ensure_user(uid)
    now = time.time()
    user_data = economy["users"][str(uid)]
    if now - user_data["last_daily"] < 86400:
        return
    is_new = user_data["last_daily"] == 0.0
    add_balance(uid, 200)
    user_data["last_daily"] = now
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
        "Rules: be specific to the target by name, be witty and cutting rather than just insulting, "
        "use irony or condescension where effective, keep it under 200 characters, "
        "and make it feel natural — like something a person would actually say. "
        "Output only the ragebait message with no preamble, explanation, or quotation marks."
    )
    prompt = (
        f"Write a ragebait reply aimed at {message.author.display_name} based on what they just said. "
        f"Their recent messages for context:\n{context}\n"
        "Make it personal, pointed, and reactive to what they actually wrote."
    )
    placeholder = await message.reply("...")
    typing_task = asyncio.create_task(keep_typing(message.channel))
    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(session, [
                {"role": "system", "content": ragebait_system},
                {"role": "user", "content": prompt},
            ], placeholder)
        await finalize(placeholder, message.channel, full_response)
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

    global stats_messages_seen
    stats_messages_seen += 1

    # Passive ragebait: track targeted users and fire at 50% chance
    if uid in active_ragebaits and not message.content.startswith("!"):
        rage = active_ragebaits[uid]
        rage["history"].append(f"[{message.author.display_name}]: {message.content[:200]}")
        rage["remaining"] -= 1
        if rage["remaining"] <= 0:
            del active_ragebaits[uid]
        if random.random() < 0.5:
            asyncio.create_task(_passive_ragebait(message, list(rage["history"])))

    # Mock: track mocked users and repeat their messages in mocking font
    if uid in active_mocks and not message.content.startswith("!"):
        mock = active_mocks[uid]
        if mock["expires_at"] <= time.time():
            del active_mocks[uid]
        else:
            mocked = mocking_font(message.content)
            await message.channel.send(f"**{message.author.display_name}:** {mocked}")

    # Auto-award daily on any bot interaction
    if (
        message.content.startswith("!")
        or bot.user in message.mentions
        or isinstance(message.channel, discord.DMChannel)
        or message.channel.id in active_hangman_games
        or uid in active_blackjack_games
    ):
        await _auto_daily(message)

    # Intercept hit / stand for active blackjack (with or without ! prefix)
    if content_lower in ("!hit", "!stand", "hit", "stand") and uid in active_blackjack_games:
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

    # Channel guard
    if ACTIVE_CHANNEL_IDS and message.channel.id not in ACTIVE_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    # Intercept free-text hangman guesses (no prefix needed when game is active)
    cid = message.channel.id
    if cid in active_hangman_games and not message.content.startswith("!"):
        guess = message.content.lower().strip()
        if guess and guess.isalpha():
            await _process_hangman_guess(message.channel, uid, cid, guess)
            return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions
    in_roleplay = uid in active_roleplays

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
        await respond_roleplay(message.channel, uid, content, message)
    else:
        await respond(message.channel, uid, content, message)

    await bot.process_commands(message)


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Utility
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="clearhistory")
async def cmd_clearhistory(ctx: commands.Context):
    channel_histories[ctx.channel.id].clear()
    await ctx.send(embed=emb("🔧 History Cleared", "Conversation history cleared for this channel.", C_GREY))


@bot.command(name="help", aliases=["llama", "h"])
async def cmd_help(ctx: commands.Context):
    embed = discord.Embed(title="📖 Commands", color=0x3498db)
    embed.add_field(name="💰 Economy", inline=False, value=(
        "`!daily` — Claim 200 🪙 (24h cooldown)\n"
        "`!balance [@user]` — Check balance\n"
        "`!leaderboard` — Top 10 richest users"
    ))
    embed.add_field(name="🎲 Gambling", inline=False, value=(
        "`!flip <amount>` — 50/50 coinflip\n"
        "`!slots <amount>` — 3-reel slot machine\n"
        "`!blackjack <amount>` — Interactive blackjack (type `hit` / `stand`)"
    ))
    embed.add_field(name="🎮 Games", inline=False, value=(
        "`!hangman` — Start hangman (type guesses directly)\n"
        "`!guess <letter or word>` — Explicit hangman guess"
    ))
    embed.add_field(name="🤖 AI", inline=False, value=(
        "`!ask <question>` — Ask the AI a question\n"
        "`!roleplay <character prompt>` — Start a roleplay (costs 100 🪙)\n"
        "`!stop` — Stop roleplay / forfeit active game"
    ))
    embed.add_field(name="🛒 Shop", inline=False, value=(
        "`!shop` — Browse items\n"
    ))
    embed.add_field(name="🔞 NSFW", inline=False, value=(
        "`!rule34 [tags]` — Random image from rule34 (alias: `!r34`)"
    ))
    embed.add_field(name="🔧 Utility", inline=False, value=(
        "`!stats` — Show bot statistics\n"
        "`!clearhistory` — Reset AI chat history for this channel"
    ))
    await ctx.send(embed=embed)


@bot.command(name="stats", aliases=["stat"])
async def cmd_stats(ctx: commands.Context):
    elapsed = time.monotonic() - bot_start_time
    msg_rate = stats_messages_seen / elapsed if elapsed > 0 else 0
    text_channels = sum(len(g.text_channels) for g in bot.guilds)
    voice_channels = sum(len(g.voice_channels) for g in bot.guilds)
    ai_connected = await check_ollama_connected()
    vram_text = bot_settings.get("vram_text", "16GB")

    embed = discord.Embed(title="📊 Bot Stats", color=C_BLUE)
    embed.add_field(name="🤖 Bot", value=str(bot.user), inline=True)
    embed.add_field(name="🆔 Bot ID", value=str(bot.user.id), inline=True)
    embed.add_field(name="⚙️ Shard", value="#0 / 1", inline=True)
    embed.add_field(name="💬 Commands Ran", value=str(stats_commands_ran), inline=True)
    embed.add_field(name="📨 Messages", value=f"{stats_messages_seen} ({msg_rate:.2f}/sec)", inline=True)
    embed.add_field(name="🧠 Memory", value=f"{get_memory_mb():.2f} MB", inline=True)
    embed.add_field(name="⏱️ Uptime", value=format_uptime(), inline=True)
    embed.add_field(name="🌐 Presence", value=(
        f"{len(bot.guilds)} Servers\n"
        f"{text_channels} Text Channels\n"
        f"{voice_channels} Voice Channels"
    ), inline=True)
    ai_status = "🟢 Connected" if ai_connected else "🔴 Disconnected"
    embed.add_field(name="🧠 AI Status", value=(
        f"{ai_status}\n"
        f"Ask model: `{current_model}`\n"
        f"Roleplay model: `{current_roleplay_model}`\n"
        f"vRAM: {vram_text}"
    ), inline=True)
    await ctx.send(embed=embed)


@bot.command(name="adminhelp")
async def cmd_adminhelp(ctx: commands.Context):
    if not can_manage_settings(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    embed = discord.Embed(title="⚙️ Admin Commands", color=C_GOLD)
    embed.add_field(name="🔧 Server Settings", inline=False, value=(
        "`!settings` — View current server settings\n"
        "`!settings ai-channels #ch... / clear` — Restrict AI commands to channels\n"
        "`!settings cmd-channels #ch... / clear` — Restrict all commands to channels\n"
        "`!settings shop <item> on|off` — Toggle shop items\n"
        "`!settings rule34 on|off / ban <tag> / unban <tag> / banned` — rule34 config"
    ))
    embed.add_field(name="🔍 Moderation", inline=False, value=(
        "`!audit` — Last 5 failed command attempts\n"
        "`!clear [n]` — Delete last n bot messages (default 50)"
    ))
    if is_admin(ctx):
        embed.add_field(name="🪙 Economy", inline=False, value=(
            "`!give @user <amount>` — Add or remove coins from a user\n"
            "`!event <amount> [hours]` — Start a reaction event"
        ))
        embed.add_field(name="🤖 AI", inline=False, value=(
            "`!model [name]` — View or change the AI model\n"
            "`!roleplaymodel [name]` — View or change the roleplay model"
        ))
        embed.add_field(name="⚙️ Config", inline=False, value=(
            "`!setprompt <prompt>` — Set a custom system prompt for this channel\n"
            "`!clearprompt` — Reset this channel's prompt to default\n"
            "`!godmode` — Toggle free costs on/off\n"
            "`!vramtext [text]` — View or set the vRAM display text in !stats"
        ))
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Economy
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="daily")
async def cmd_daily(ctx: commands.Context):
    uid = ctx.author.id
    _ensure_user(uid)
    now = time.time()
    last = economy["users"][str(uid)]["last_daily"]
    elapsed = now - last
    if elapsed < 86400:
        remaining = int(86400 - elapsed)
        hours, rem = divmod(remaining, 3600)
        minutes = rem // 60
        await ctx.send(embed=emb("⏳ Already Claimed", f"Come back in **{hours}h {minutes}m**.", C_GOLD))
        return
    add_balance(uid, 200)
    economy["users"][str(uid)]["last_daily"] = now
    save_economy()
    await ctx.send(embed=emb("🪙 Daily Reward", f"+200 🪙 claimed! Balance: **{get_balance(uid)} 🪙**", C_GREEN))


@bot.command(name="balance", aliases=["bal"])
async def cmd_balance(ctx: commands.Context):
    target = ctx.message.mentions[0] if ctx.message.mentions else ctx.author
    bal = get_balance(target.id)
    await ctx.send(embed=emb("💰 Balance", f"**{target.display_name}**: {bal} 🪙", C_GREEN))


@bot.command(name="leaderboard", aliases=["leaderboards"])
async def cmd_leaderboard(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("Leaderboard is only available in servers.")
        return
    sorted_users = sorted(
        economy["users"].items(), key=lambda x: x[1]["balance"], reverse=True
    )[:10]
    if not sorted_users:
        await ctx.send(embed=emb("🪙 Leaderboard", "No users yet.", C_GREEN))
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid_str, data) in enumerate(sorted_users):
        uid_int = int(uid_str)
        name = None
        # Try guild cache first
        member = ctx.guild.get_member(uid_int)
        if member:
            name = member.display_name
        # Try fetching from guild if not cached
        if name is None:
            try:
                member = await ctx.guild.fetch_member(uid_int)
                name = member.display_name
            except (discord.NotFound, discord.HTTPException):
                pass
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


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Gambling
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="flip", aliases=["coinflip"])
async def cmd_flip(ctx: commands.Context, amount: str = None):
    uid = ctx.author.id
    if amount is None:
        await ctx.send("Usage: `!flip <amount>`")
        return
    try:
        amount = int(amount)
        assert amount > 0
    except (ValueError, AssertionError):
        await ctx.send("Please provide a positive whole number amount.")
        return
    if not godmode and not deduct_balance(uid, amount):
        await ctx.send(embed=emb("💸 Insufficient Funds", f"Balance: {get_balance(uid)} 🪙", C_RED))
        return
    win = random.random() < 0.5
    if win:
        add_balance(uid, amount * 2)
        await ctx.send(embed=emb("🪙 Heads!", f"You won **{amount} 🪙**! Balance: {get_balance(uid)} 🪙", C_GREEN))
    else:
        await ctx.send(embed=emb("🪙 Tails!", f"You lost **{amount} 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))


@bot.command(name="slots")
async def cmd_slots(ctx: commands.Context, amount: str = None):
    uid = ctx.author.id
    if amount is None:
        await ctx.send("Usage: `!slots <amount>`")
        return
    try:
        amount = int(amount)
        assert amount > 0
    except (ValueError, AssertionError):
        await ctx.send("Please provide a positive whole number amount.")
        return
    if not godmode and not deduct_balance(uid, amount):
        await ctx.send(embed=emb("💸 Insufficient Funds", f"Balance: {get_balance(uid)} 🪙", C_RED))
        return
    reels = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
    display = " | ".join(reels)
    if reels[0] == reels[1] == reels[2]:
        sym = reels[0]
        if sym == "🪙":
            mult = 10
        elif sym == "💎":
            mult = 5
        else:
            mult = 3
        winnings = amount * mult
        add_balance(uid, winnings)
        await ctx.send(embed=emb(
            "🎰 Jackpot!",
            f"{display}\n\n**{mult}x!** You won **{winnings} 🪙**! Balance: {get_balance(uid)} 🪙",
            C_GREEN,
        ))
    else:
        await ctx.send(embed=emb(
            "🎰 No Match",
            f"{display}\n\nYou lost **{amount} 🪙**. Balance: {get_balance(uid)} 🪙",
            C_RED,
        ))


@bot.command(name="blackjack")
async def cmd_blackjack(ctx: commands.Context, amount: str = None):
    uid = ctx.author.id
    if amount is None:
        await ctx.send("Usage: `!blackjack <amount>`")
        return
    try:
        amount = int(amount)
        assert amount > 0
    except (ValueError, AssertionError):
        await ctx.send("Please provide a positive whole number amount.")
        return
    if uid in active_blackjack_games:
        await ctx.send(embed=emb("🃏 Already Playing", "Just type `hit` or `stand`.", C_GOLD))
        return
    if not godmode and not deduct_balance(uid, amount):
        await ctx.send(embed=emb("💸 Insufficient Funds", f"Balance: {get_balance(uid)} 🪙", C_RED))
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

async def _process_hangman_guess(channel: discord.abc.Messageable, author_id: int, cid: int, guess: str):
    """Shared hangman guess logic used by both !guess command and free-text intercept."""
    game = active_hangman_games[cid]

    if not guess.isalpha():
        return  # silently ignore non-alpha free-text; cmd_guess shows an error

    # Full word guess
    if len(guess) > 1:
        if guess == game["word"]:
            del active_hangman_games[cid]
            add_balance(author_id, 50)
            await channel.send(embed=emb(
                "🎉 Correct!",
                f"The word was `{game['word']}`!\n**+50 🪙** | Balance: {get_balance(author_id)} 🪙",
                C_GREEN,
            ))
        else:
            game["wrong_guesses"] += 1
            if game["wrong_guesses"] >= 6:
                del active_hangman_games[cid]
                await channel.send(embed=emb("💀 Game Over", build_hangman_display(game) + f"\n\nThe word was `{game['word']}`.", C_RED))
            else:
                await channel.send(embed=emb("❌ Wrong Word", build_hangman_display(game), C_RED))
        return

    # Single letter guess
    if guess in game["guessed_letters"]:
        await channel.send(embed=emb("⚠️ Already Guessed", f"You already guessed `{guess}`!", C_GOLD))
        return
    game["guessed_letters"].add(guess)
    if guess in game["word"]:
        if all(c in game["guessed_letters"] for c in game["word"]):
            del active_hangman_games[cid]
            add_balance(author_id, 50)
            await channel.send(embed=emb(
                "🎉 You Got It!",
                f"The word was `{game['word']}`!\n**+50 🪙** | Balance: {get_balance(author_id)} 🪙",
                C_GREEN,
            ))
        else:
            await channel.send(embed=emb("✅ Good Guess!", build_hangman_display(game), C_GREEN))
    else:
        game["wrong_guesses"] += 1
        if game["wrong_guesses"] >= 6:
            del active_hangman_games[cid]
            await channel.send(embed=emb("💀 Game Over", build_hangman_display(game) + f"\n\nThe word was `{game['word']}`.", C_RED))
        else:
            await channel.send(embed=emb("❌ Wrong Letter", build_hangman_display(game), C_ORANGE))


@bot.command(name="hangman")
async def cmd_hangman(ctx: commands.Context):
    cid = ctx.channel.id
    if cid in active_hangman_games:
        await ctx.send(embed=emb("🔤 Already Playing", "Just type your guess directly!", C_ORANGE))
        return
    word = random.choice(HANGMAN_WORDS)
    active_hangman_games[cid] = {
        "word": word,
        "guessed_letters": set(),
        "wrong_guesses": 0,
        "user_id": ctx.author.id,
    }
    await ctx.send(embed=emb("🔤 Hangman", build_hangman_display(active_hangman_games[cid]) + "\n\nJust type a letter or use `!guess` to guess the full word!", C_ORANGE))


@bot.command(name="guess")
async def cmd_guess(ctx: commands.Context, *, guess: str = None):
    cid = ctx.channel.id
    if cid not in active_hangman_games:
        await ctx.send(embed=emb("🔤 No Game", "No active hangman game. Start one with `!hangman`.", C_ORANGE))
        return
    if guess is None:
        await ctx.send(embed=emb("🔤 Hangman", "Usage: `!guess <letter or word>`", C_ORANGE))
        return
    await _process_hangman_guess(ctx.channel, ctx.author.id, cid, guess.lower().strip())


# ─────────────────────────────────────────────────────────────────────────────
# Commands — AI
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="ask")
async def cmd_ask(ctx: commands.Context, *, question: str = None):
    if ctx.guild:
        cfg = get_guild_cfg(ctx.guild.id)
        ai_channels = cfg.get("ai_channels", [])
        if ai_channels and ctx.channel.id not in ai_channels:
            names = " ".join(f"<#{cid}>" for cid in ai_channels)
            await ctx.send(embed=emb("❌ Wrong Channel", f"AI commands are only allowed in: {names}", C_RED))
            return
    if question is None:
        await ctx.send("Usage: `!ask <question>`")
        return
    if check_rate_limit(ctx.author.id):
        await ctx.send("⚠️ Slow down! Please wait a moment before sending another message.")
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

    await respond(ctx.channel, ctx.author.id, question, ctx.message, system_prompt=system_prompt)


@bot.command(name="roleplay")
async def cmd_roleplay(ctx: commands.Context, *, character_prompt: str = None):
    if ctx.guild:
        cfg = get_guild_cfg(ctx.guild.id)
        ai_channels = cfg.get("ai_channels", [])
        if ai_channels and ctx.channel.id not in ai_channels:
            names = " ".join(f"<#{cid}>" for cid in ai_channels)
            await ctx.send(embed=emb("❌ Wrong Channel", f"AI commands are only allowed in: {names}", C_RED))
            return
    uid = ctx.author.id
    if character_prompt is None:
        await ctx.send("Usage: `!roleplay <character prompt>`")
        return
    cost = 0 if godmode else 100
    if cost > 0 and not deduct_balance(uid, cost):
        await ctx.send(embed=emb("💸 Insufficient Funds", f"Starting a roleplay costs **100 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
        return
    active_roleplays[uid] = {"character_prompt": character_prompt}
    roleplay_histories[uid] = []
    preview = character_prompt[:100] + ("..." if len(character_prompt) > 100 else "")
    await ctx.send(embed=emb(
        "🎭 Roleplay Started",
        f"Responding as: *{preview}*\nType freely — no @mention needed. Use `!stop` to end.",
        C_BLUE,
    ))


@bot.command(name="stop")
async def cmd_stop(ctx: commands.Context):
    uid = ctx.author.id
    cid = ctx.channel.id
    stopped = []

    if uid in active_roleplays:
        del active_roleplays[uid]
        roleplay_histories.pop(uid, None)
        stopped.append("🎭 Roleplay")

    if uid in active_blackjack_games:
        amount = active_blackjack_games[uid]["amount"]
        del active_blackjack_games[uid]
        stopped.append(f"🃏 Blackjack (forfeited {amount} 🪙)")

    if cid in active_hangman_games and active_hangman_games[cid]["user_id"] == uid:
        word = active_hangman_games[cid]["word"]
        del active_hangman_games[cid]
        stopped.append(f"🔤 Hangman (the word was `{word}`)")

    if not stopped:
        await ctx.send(embed=emb("⏹️ Nothing to Stop", "No active roleplay, blackjack, or hangman game.", C_GREY))
        return

    await ctx.send(embed=emb("⏹️ Stopped", "\n".join(stopped), C_GREY))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Shop
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="shop")
async def cmd_shop(ctx: commands.Context, subcommand: str = None, *args):
    uid = ctx.author.id

    if subcommand is None:
        _si = get_guild_cfg(ctx.guild.id).get("shop_items", {}) if ctx.guild else {}
        _items = []
        if _si.get("nickname", True):
            _items.append("`!shop nickname <new_name>` — Change your own nickname — **2,000 🪙**")
            _items.append("`!shop nickname @user <new_name>` — Change someone else's nickname — **10,000 🪙**")
            _items.append("`!shop removenickname` — Remove your own nickname — **2,000 🪙**")
        if _si.get("role", True):
            _items.append("`!shop role <name> <hex>` — Create a custom colored role — **10,000 🪙**")
        if _si.get("removerole", True):
            _items.append("`!shop removerole <name>` — Delete a bot-created role — **2,000 🪙**")
        if _si.get("ragebait", True):
            _items.append("`!shop ragebait @user [topic]` — Ragebait someone for 10 messages — **5,000 🪙**")
        _items.append("`!shop mock @user` — Mock someone's messages for 5 minutes — **3,000 🪙**")
        _items.append("`!shop insurance` — Protect yourself for 24 hours — **1,000 🪙**")
        desc = "\n".join(_items) if _items else "No shop items are currently available."
        await ctx.send(embed=emb("🛒 Shop", desc, C_PURPLE))
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
            cost = 0 if godmode else 10000
            cost_label = "10,000"
        else:
            target = ctx.author
            new_name = " ".join(args)
            cost = 0 if godmode else 2000
            cost_label = "2,000"

        if not new_name:
            await ctx.send(embed=emb("🛒 Shop", "Please provide a new nickname.", C_PURPLE))
            return
        if is_insured(target.id, "nickname"):
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be renamed.", C_GOLD))
            return
        if cost > 0 and not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"This costs **{cost_label} 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
            return
        try:
            await target.edit(nick=new_name)
            await ctx.send(embed=emb("✅ Nickname Changed", f"**{target.display_name}**'s nickname is now **{new_name}**!", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
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
        cost = 0 if godmode else 2000
        if is_insured(uid, "nickname"):
            await ctx.send(embed=emb("🛡️ Protected", "You have insurance and can't have your nickname changed.", C_GOLD))
            return
        if cost > 0 and not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"This costs **2,000 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
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
        if len(args) < 2:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop role <name> <hex_color>` (e.g. `!shop role CoolGuy ff00aa`)", C_PURPLE))
            return
        hex_color = args[-1].lstrip("#")
        name = " ".join(args[:-1])
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
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if is_insured(uid, "role"):
            await ctx.send(embed=emb("🛡️ Protected", "You have insurance and can't be given new roles.", C_GOLD))
            return
        cost = 0 if godmode else 10000
        if cost > 0 and not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"This costs **10,000 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
            return
        try:
            new_role = await ctx.guild.create_role(name=name, color=discord.Color(color_int))
            await ctx.author.add_roles(new_role)
            bot_roles.add(new_role.id)
            save_bot_roles()
            await ctx.send(embed=emb("✅ Role Created", f"Role **{name}** created and assigned!", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
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
        role = discord.utils.find(lambda r: r.name.lower() == name.lower() and r.id in bot_roles, ctx.guild.roles)
        if role is None:
            await ctx.send(embed=emb("❌ Not Found", f"No bot-created role named **{name}** exists.", C_RED))
            return
        cost = 0 if godmode else 2000
        if cost > 0 and not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"This costs **2,000 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
            return
        try:
            await role.delete()
            bot_roles.discard(role.id)
            save_bot_roles()
            await ctx.send(embed=emb("✅ Role Removed", f"Role **{name}** has been deleted.", C_GREEN))
        except discord.Forbidden:
            if cost > 0:
                add_balance(uid, cost)
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete that role.", C_RED))
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
        cost = 0 if godmode else 5000
        if cost > 0 and not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"This costs **5,000 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
            return
        topic_clause = f" The topic should be specifically about: {topic}." if topic else ""
        ragebait_system = (
            "You are an expert at crafting ragebait — messages specifically engineered to provoke "
            "an emotional reaction. Your goal is to write something that will genuinely irritate, "
            "annoy, or get under the skin of the target. "
            "Rules: be specific to the target by name, be witty and cutting rather than just insulting, "
            "use irony or condescension where effective, keep it under 200 characters, "
            "and make it feel natural — like something a person would actually say. "
            "Output only the ragebait message with no preamble, explanation, or quotation marks."
        )
        prompt = (
            f"Write a ragebait message aimed at {target.display_name}.{topic_clause} "
            "Make it personal, pointed, and likely to provoke a reaction."
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
            active_ragebaits[target.id] = {"remaining": 10, "history": []}
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
        cost = 0 if godmode else 3000
        if cost > 0 and not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"This costs **3,000 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
            return
        active_mocks[target.id] = {"expires_at": time.time() + 300, "started_by": uid}
        await ctx.send(embed=emb(
            "🎭 Mock Activated",
            f"**{target.display_name}** will be mocked for 5 minutes!",
            C_PURPLE,
        ))
        return

    # ── !shop insurance ───────────────────────────────────────────────────────
    if subcommand == "insurance":
        key = str(uid)
        cost = 0 if godmode else 1000
        if cost > 0 and not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"Insurance costs **1,000 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
            return
        insurance[key] = {
            "expires_at": time.time() + 86400,
            "protected_from": ["ragebait", "mock", "nickname", "role"],
        }
        save_insurance()
        await ctx.send(embed=emb(
            "🛡️ Insurance Purchased",
            "Protected for 24 hours against ragebait, mock, nickname, and role changes!",
            C_GREEN,
        ))
        return

    await ctx.send(embed=emb("🛒 Unknown Item", "Try `!shop` to see what's available.", C_PURPLE))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Settings
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="settings")
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
        cmd_channels = cfg.get("command_channels", [])
        shop_items = cfg.get("shop_items", {})
        r34_enabled = cfg.get("rule34_enabled", True)
        r34_banned = cfg.get("rule34_banned_tags", [])

        ai_val = " ".join(f"<#{c}>" for c in ai_channels) if ai_channels else "all channels"
        cmd_val = " ".join(f"<#{c}>" for c in cmd_channels) if cmd_channels else "all channels"
        item_names = ["nickname", "role", "removerole", "ragebait"]
        shop_val = "  ".join(
            f"{n} {'✅' if shop_items.get(n, True) else '❌'}" for n in item_names
        )
        r34_val = ("✅ enabled" if r34_enabled else "❌ disabled")
        if r34_banned:
            r34_val += f"\nBanned tags: {', '.join(r34_banned)}"

        embed = discord.Embed(title="⚙️ Server Settings", color=C_BLUE)
        embed.add_field(name="🤖 AI channels", value=ai_val, inline=False)
        embed.add_field(name="💬 Command channels", value=cmd_val, inline=False)
        embed.add_field(name="🛒 Shop items", value=shop_val, inline=False)
        embed.add_field(name="🔞 rule34", value=r34_val, inline=False)
        embed.set_footer(text="Use !settings <subcommand> to change. Subcommands: ai-channels, cmd-channels, shop, rule34")
        await ctx.send(embed=embed)
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

    # ── cmd-channels ──────────────────────────────────────────────────────────
    if subcommand == "cmd-channels":
        if args and args[0].lower() == "clear":
            cfg["command_channels"] = []
            save_guild_settings()
            await ctx.send(embed=emb("⚙️ Command Channels", "Command channel restriction removed — all channels allowed.", C_GREEN))
        elif ctx.message.channel_mentions:
            cfg["command_channels"] = [c.id for c in ctx.message.channel_mentions]
            save_guild_settings()
            names = " ".join(c.mention for c in ctx.message.channel_mentions)
            await ctx.send(embed=emb("⚙️ Command Channels", f"All commands restricted to: {names}\n(Note: `!settings` always works everywhere)", C_GREEN))
        else:
            await ctx.send(embed=emb("⚙️ Command Channels", "Usage: `!settings cmd-channels #channel ...` or `!settings cmd-channels clear`", C_GREY))
        return

    # ── shop ──────────────────────────────────────────────────────────────────
    if subcommand == "shop":
        valid_items = {"nickname", "role", "removerole", "ragebait"}
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
            await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 on|off` / `ban <tag>` / `unban <tag>` / `banned`", C_GREY))
            return
        action = args[0].lower()
        if action in ("on", "off"):
            cfg["rule34_enabled"] = (action == "on")
            save_guild_settings()
            status = "✅ enabled" if action == "on" else "❌ disabled"
            await ctx.send(embed=emb("⚙️ rule34", f"rule34 is now {status}.", C_GREEN))
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
            await ctx.send(embed=emb("⚙️ rule34", "Usage: `!settings rule34 on|off` / `ban <tag>` / `unban <tag>` / `banned`", C_GREY))
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
        await ctx.send(embed=emb("🔍 Audit Log", "No failed attempts recorded.", C_GREY))
        return
    recent = list(audit_log)[-5:]
    lines = []
    for e in reversed(recent):
        ts = time.strftime("%H:%M:%S", time.localtime(e["time"]))
        lines.append(f"**{ts}** — {e['user']}\n`{e['command']}`\n_{e['error']}_")
    await ctx.send(embed=emb("🔍 Audit Log", "\n\n".join(lines), C_GOLD))


@bot.command(name="clear")
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


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Admin (.xeph only)
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="godmode")
async def cmd_godmode(ctx: commands.Context):
    global godmode
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    godmode = not godmode
    state = "ON" if godmode else "OFF"
    await ctx.send(embed=emb("👑 Godmode", f"Godmode is now **{state}**.", C_GOLD))



@bot.command(name="model")
async def cmd_model(ctx: commands.Context, model_name: str = None):
    global current_model
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if model_name is None:
        await ctx.send(embed=emb("⚙️ Model", f"Current model: `{current_model}`", C_GREY))
        return
    current_model = model_name
    save_models(current_model, current_roleplay_model)
    await ctx.send(embed=emb("⚙️ Model", f"Switched to `{model_name}`", C_GREY))


@bot.command(name="roleplaymodel")
async def cmd_roleplaymodel(ctx: commands.Context, model_name: str = None):
    global current_roleplay_model
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if model_name is None:
        await ctx.send(embed=emb("⚙️ Roleplay Model", f"Current roleplay model: `{current_roleplay_model}`", C_GREY))
        return
    current_roleplay_model = model_name
    save_models(current_model, current_roleplay_model)
    await ctx.send(embed=emb("⚙️ Roleplay Model", f"Switched to `{model_name}`", C_GREY))


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
        print(f"[event] No permission to delete command message in {ctx.channel}")
    except Exception as e:
        print(f"[event] Failed to delete command message: {e}")
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

    duration_str = f" for {duration_hours}h" if duration_hours else ""
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
    if user.bot:
        return
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
        await reaction.message.channel.send(
            f"✅ **{user.display_name}** earned **{event['amount']} 🪙**!"
        )
    except Exception as e:
        print(f"[event] Error rewarding {user.id}: {e}")
        event["rewarded"].discard(user.id)


@bot.command(name="give")
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
    except (ValueError, AssertionError):
        await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a non-zero whole number.", C_RED))
        return
    add_balance(target.id, amount)
    action = "given" if amount > 0 else "removed"
    await ctx.send(embed=emb(
        "💸 Give",
        f"**{abs(amount)} 🪙** {action} {'to' if amount > 0 else 'from'} **{target.display_name}**. "
        f"New balance: {get_balance(target.id)} 🪙",
        C_GOLD,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Rule34
# ─────────────────────────────────────────────────────────────────────────────

async def _r34_fetch(session: aiohttp.ClientSession, search_tags: str) -> list[dict]:
    url = (
        f"https://api.rule34.xxx/index.php"
        f"?page=dapi&s=post&q=index&json=1&limit=100&tags={search_tags}"
    )
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            return []
        text = await resp.text()
    # API returns "0\n" or XML when no results instead of []
    text = text.strip()
    if not text or text == "0" or text.startswith("<"):
        return []
    try:
        import json as _json
        data = _json.loads(text)
    except Exception:
        print(f"[rule34] JSON parse failed for tags={search_tags!r}, body={text[:200]!r}")
        return []
    print(f"[rule34] tags={search_tags!r} data type={type(data).__name__} len={len(data) if isinstance(data, list) else 'N/A'}")
    if isinstance(data, list) and data:
        print(f"[rule34] first item type={type(data[0]).__name__} val={str(data[0])[:200]}")
    if not isinstance(data, list):
        return []
    return [p for p in data if isinstance(p, dict) and p.get("file_url")]


@bot.command(name="rule34", aliases=["r34"])
async def cmd_rule34(ctx: commands.Context, *, tags: str = ""):
    cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
    if not cfg.get("rule34_enabled", True):
        await ctx.send(embed=emb("🔞 Disabled", "rule34 is disabled in this server.", C_GREY))
        return
    await ctx.typing()
    _STOP = {"and", "or", "with", "the", "a", "an"}
    tag_parts = [w for w in tags.strip().split() if w.lower() not in _STOP]
    banned = [t.lower() for t in cfg.get("rule34_banned_tags", [])]
    tag_parts = [w for w in tag_parts if w.lower() not in banned]

    try:
        async with aiohttp.ClientSession() as session:
            # Try all tags combined first, then fall back to each tag alone
            search_tags = "+".join(tag_parts) if tag_parts else "solo"
            posts = await _r34_fetch(session, search_tags)

            if not posts and len(tag_parts) > 1:
                for part in tag_parts:
                    posts = await _r34_fetch(session, part)
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

    post = random.choice(posts)
    file_url = post["file_url"]
    display = search_tags.replace("+", " ") if tag_parts else "random"

    embed = discord.Embed(title=f"🔞 rule34: {display}", color=C_PURPLE)
    embed.set_image(url=file_url)
    embed.set_footer(text=f"Score: {post.get('score', '?')} | Rating: {post.get('rating', '?')}")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(DISCORD_TOKEN)
