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


channel_prompts = load_channel_prompts()
economy = load_economy()
bot_roles: set[int] = load_bot_roles()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ── AI model globals ──────────────────────────────────────────────────────────
current_model = OLLAMA_MODEL
current_roleplay_model = OLLAMA_MODEL

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
    return ctx.author.name == ".xeph"


def check_rate_limit(user_id: int) -> bool:
    now = time.monotonic()
    last = user_last_request.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    user_last_request[user_id] = now
    return False


def get_system_prompt(channel_id: int) -> str:
    return channel_prompts.get(channel_id, SYSTEM_PROMPT)


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
# Events
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        print(f"[debug] {error}")
        return
    raise error


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
        "You are a master provocateur. Generate a short, sharp, witty message "
        "specifically designed to annoy and provoke the target user into an emotional "
        "reaction. Be creative and targeted. Stay under 200 characters."
    )
    prompt = (
        f"Generate a ragebait reply targeting {message.author.display_name} "
        f"based on what they just said. Their recent messages for context:\n{context}\n"
        "Just the message, no preamble."
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

    # Passive ragebait: track targeted users and fire at 50% chance
    if uid in active_ragebaits and not message.content.startswith("!"):
        rage = active_ragebaits[uid]
        rage["history"].append(f"[{message.author.display_name}]: {message.content[:200]}")
        rage["remaining"] -= 1
        if rage["remaining"] <= 0:
            del active_ragebaits[uid]
        if random.random() < 0.5:
            asyncio.create_task(_passive_ragebait(message, list(rage["history"])))

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


@bot.command(name="help", aliases=["llama"])
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
        "`!shop nickname <name>` — Change own nickname (2,000 🪙)\n"
        "`!shop nickname @user <name>` — Change someone's nickname (10,000 🪙)\n"
        "`!shop role <name> <hex>` — Custom colored role (10,000 🪙)\n"
        "`!shop removerole <name>` — Delete a bot-created role (2,000 🪙)\n"
        "`!shop ragebait @user [topic]` — Ragebait someone for 10 messages (5,000 🪙)"
    ))
    embed.add_field(name="🔧 Utility", inline=False, value=(
        "`!clearhistory` — Reset AI chat history for this channel"
    ))
    await ctx.send(embed=embed)


@bot.command(name="adminhelp")
async def cmd_adminhelp(ctx: commands.Context):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    embed = discord.Embed(title="⚙️ Admin Commands", color=C_GOLD)
    embed.add_field(name="🪙 Economy", inline=False, value=(
        "`!give @user <amount>` — Add or remove coins from a user\n"
        "`!event <amount> [hours]` — Start a reaction event"
    ))
    embed.add_field(name="🤖 AI", inline=False, value=(
        "`!model [name]` — View or change the AI model\n"
        "`!roleplaymodel [name]` — View or change the roleplay model\n"
        "`!ragebait @user [topic]` — Generate targeted ragebait"
    ))
    embed.add_field(name="⚙️ Config", inline=False, value=(
        "`!setprompt <prompt>` — Set a custom system prompt for this channel\n"
        "`!clearprompt` — Reset this channel's prompt to default\n"
        "`!godmode` — Toggle free costs on/off"
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


@bot.command(name="balance")
async def cmd_balance(ctx: commands.Context):
    target = ctx.message.mentions[0] if ctx.message.mentions else ctx.author
    bal = get_balance(target.id)
    await ctx.send(embed=emb("💰 Balance", f"**{target.display_name}**: {bal} 🪙", C_GREEN))


@bot.command(name="leaderboard")
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
        member = ctx.guild.get_member(int(uid_str))
        name = member.display_name if member else f"User {uid_str}"
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} **{name}** — {data['balance']} 🪙")
    await ctx.send(embed=emb("🪙 Leaderboard", "\n".join(lines), C_GREEN))


# ─────────────────────────────────────────────────────────────────────────────
# Commands — Gambling
# ─────────────────────────────────────────────────────────────────────────────

@bot.command(name="flip")
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
    await ctx.send(embed=emb("🔤 Hangman", build_hangman_display(active_hangman_games[cid]) + "\n\nJust type a letter or word to guess!", C_ORANGE))


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

    if cid in active_hangman_games:
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
        await ctx.send(embed=emb(
            "🛒 Shop",
            "`!shop nickname <new_name>` — Change your own nickname — **2,000 🪙**\n"
            "`!shop nickname @user <new_name>` — Change someone else's nickname — **10,000 🪙**\n"
            "`!shop role <name> <hex>` — Create a custom colored role — **10,000 🪙**\n"
            "`!shop removerole <name>` — Delete a bot-created role — **2,000 🪙**\n"
            "`!shop ragebait @user [topic]` — Ragebait someone for 10 messages — **5,000 🪙**",
            C_PURPLE,
        ))
        return

    # ── !shop nickname ────────────────────────────────────────────────────────
    if subcommand == "nickname":
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

    # ── !shop role ────────────────────────────────────────────────────────────
    if subcommand == "role":
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
        if not ctx.message.mentions:
            await ctx.send(embed=emb("🛒 Shop", "Usage: `!shop ragebait @user [topic]`", C_PURPLE))
            return
        target = ctx.message.mentions[0]
        topic = " ".join(a for a in args if not a.startswith("<@"))
        cost = 0 if godmode else 5000
        if cost > 0 and not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"This costs **5,000 🪙**. Balance: {get_balance(uid)} 🪙", C_RED))
            return
        topic_clause = f" about {topic}" if topic else ""
        ragebait_system = (
            "You are a master provocateur. Generate a short, sharp, witty message "
            "specifically designed to annoy and provoke the target user into an emotional "
            "reaction. Be creative and targeted. Stay under 200 characters."
        )
        prompt = (
            f"Generate a ragebait message targeted at {target.display_name}{topic_clause}. "
            "Just the message, no preamble."
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

    await ctx.send(embed=emb("🛒 Unknown Item", "Try `!shop` to see what's available.", C_PURPLE))


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


@bot.command(name="ragebait")
async def cmd_ragebait(ctx: commands.Context, target: discord.Member = None, *, topic: str = ""):
    if not is_admin(ctx):
        await ctx.send(embed=emb("❌ No Permission", "", C_RED))
        return
    if target is None:
        await ctx.send("Usage: `!ragebait @user [optional topic]`")
        return
    topic_clause = f" about {topic}" if topic else ""
    ragebait_system = (
        "You are a master provocateur. Generate a short, sharp, witty message "
        "specifically designed to annoy and provoke the target user into an emotional "
        "reaction. Be creative and targeted. Stay under 200 characters."
    )
    prompt = (
        f"Generate a ragebait message targeted at {target.display_name}{topic_clause}. "
        "Just the message, no preamble."
    )
    placeholder = await ctx.send("...")
    typing_task = asyncio.create_task(keep_typing(ctx.channel))
    try:
        messages = [
            {"role": "system", "content": ragebait_system},
            {"role": "user", "content": prompt},
        ]
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(session, messages, placeholder)
        await finalize(placeholder, ctx.channel, f"{target.mention} {full_response}")
        # Register passive ragebait — 50% chance per message for next 10 messages
        active_ragebaits[target.id] = {"remaining": 10, "history": []}
    except Exception as e:
        await placeholder.edit(content=f"⚠️ {e}")
    finally:
        typing_task.cancel()


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
    await ctx.send(embed=emb("⚙️ Roleplay Model", f"Switched to `{model_name}`", C_GREY))


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
    if amount is None:
        await ctx.send(embed=emb("⚙️ Event", "Usage: `!event <amount> [duration_hours]`", C_GREY))
        return
    try:
        amount = int(amount)
        assert amount > 0
    except (ValueError, AssertionError):
        await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a positive whole number.", C_RED))
        return

    duration_hours = None
    if duration is not None:
        try:
            duration_hours = float(duration)
            assert duration_hours > 0
        except (ValueError, AssertionError):
            await ctx.send(embed=emb("❌ Invalid Duration", "Duration must be a positive number of hours.", C_RED))
            return

    duration_str = f" for {duration_hours}h" if duration_hours else ""
    event_msg = await ctx.send(embed=emb(
        "🎉 Coin Event!",
        f"React with 🪙 to receive **{amount} 🪙**!{duration_str}",
        C_GOLD,
    ))
    await event_msg.add_reaction("🪙")
    active_events[event_msg.id] = {"amount": amount, "rewarded": set()}

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
    event["rewarded"].add(user.id)
    add_balance(user.id, event["amount"])


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
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(DISCORD_TOKEN)
