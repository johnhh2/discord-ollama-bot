import os
import asyncio
import aiohttp
import discord
import json
import time
from discord.ext import commands
from discord import app_commands
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

current_model = OLLAMA_MODEL
channel_histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
user_last_request: dict[int, float] = {}


def load_channel_prompts() -> dict[int, str]:
    if os.path.exists(CHANNEL_PROMPTS_FILE):
        with open(CHANNEL_PROMPTS_FILE) as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def save_channel_prompts(prompts: dict[int, str]):
    with open(CHANNEL_PROMPTS_FILE, "w") as f:
        json.dump({str(k): v for k, v in prompts.items()}, f, indent=2)


channel_prompts = load_channel_prompts()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def check_rate_limit(user_id: int) -> bool:
    """Returns True if the user is rate limited."""
    now = time.monotonic()
    last = user_last_request.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    user_last_request[user_id] = now
    return False


def get_system_prompt(channel_id: int) -> str:
    return channel_prompts.get(channel_id, SYSTEM_PROMPT)


async def keep_typing(channel: discord.abc.Messageable):
    """Re-sends typing indicator every 8s until cancelled."""
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
) -> str:
    """Streams Ollama response, editing placeholder with progressive output. Returns full text."""
    payload = {
        "model": current_model,
        "messages": messages,
        "stream": True,
    }

    full_response = ""
    last_edit = 0.0
    EDIT_INTERVAL = 0.8  # seconds between edits to stay under Discord rate limits

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
    """Edit placeholder with final text, sending overflow as follow-up messages."""
    chunks = [text[i:i + 2000] for i in range(0, max(len(text), 1), 2000)]
    await placeholder.edit(content=chunks[0])
    for chunk in chunks[1:]:
        await channel.send(chunk)


async def respond(
    channel: discord.abc.Messageable,
    user_id: int,
    content: str,
    reply_to: discord.Message,
):
    """Core pipeline: build history, stream response, finalize."""
    channel_id = channel.id
    history = channel_histories[channel_id]
    system_prompt = get_system_prompt(channel_id)

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


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if ACTIVE_CHANNEL_IDS:
        print(f"Listening in channels: {ACTIVE_CHANNEL_IDS}")
    else:
        print("Listening in all channels")


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    if ACTIVE_CHANNEL_IDS and message.channel.id not in ACTIVE_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions

    if not (is_dm or is_mentioned):
        await bot.process_commands(message)
        return

    content = message.content.replace(f"<@{bot.user.id}>", "").strip()
    if not content:
        await message.reply("Yes?")
        return

    if check_rate_limit(message.author.id):
        await message.reply("⚠️ Slow down! Please wait a moment before sending another message.")
        return

    await respond(message.channel, message.author.id, content, message)
    await bot.process_commands(message)


@bot.tree.command(name="ask", description="Ask the bot a question")
@app_commands.describe(question="Your question")
async def slash_ask(interaction: discord.Interaction, question: str):
    if ACTIVE_CHANNEL_IDS and interaction.channel_id not in ACTIVE_CHANNEL_IDS:
        await interaction.response.send_message("I'm not active in this channel.", ephemeral=True)
        return

    if check_rate_limit(interaction.user.id):
        await interaction.response.send_message(
            "⚠️ Slow down! Please wait a moment.", ephemeral=True
        )
        return

    await interaction.response.defer()

    channel_id = interaction.channel_id
    history = channel_histories[channel_id]
    system_prompt = get_system_prompt(channel_id)

    history.append({"role": "user", "content": question})
    messages = [{"role": "system", "content": system_prompt}] + list(history)

    placeholder = await interaction.followup.send("...")
    typing_task = asyncio.create_task(keep_typing(interaction.channel))

    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(session, messages, placeholder)

        history.append({"role": "assistant", "content": full_response})
        await finalize(placeholder, interaction.channel, full_response)

    except aiohttp.ClientError as e:
        history.pop()
        await placeholder.edit(content=f"⚠️ Could not reach Ollama: `{e}`")
    except Exception as e:
        history.pop()
        await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
    finally:
        typing_task.cancel()


@bot.command(name="model")
@commands.has_permissions(administrator=True)
async def cmd_model(ctx: commands.Context, model_name: str = None):
    global current_model
    if model_name is None:
        await ctx.send(f"Current model: `{current_model}`")
        return
    current_model = model_name
    await ctx.send(f"Model switched to `{model_name}`")


@bot.command(name="setprompt")
@commands.has_permissions(administrator=True)
async def cmd_setprompt(ctx: commands.Context, *, prompt: str):
    channel_prompts[ctx.channel.id] = prompt
    save_channel_prompts(channel_prompts)
    await ctx.send("System prompt updated for this channel.")


@bot.command(name="clearprompt")
@commands.has_permissions(administrator=True)
async def cmd_clearprompt(ctx: commands.Context):
    channel_prompts.pop(ctx.channel.id, None)
    save_channel_prompts(channel_prompts)
    await ctx.send("Channel prompt cleared. Using default system prompt.")


@bot.command(name="clearhistory")
async def cmd_clearhistory(ctx: commands.Context):
    channel_histories[ctx.channel.id].clear()
    await ctx.send("Conversation history cleared for this channel.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(DISCORD_TOKEN)
