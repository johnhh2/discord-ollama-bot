import os
import asyncio
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a helpful assistant.")

# Optional: restrict to specific channel IDs
_raw_channels = os.getenv("ACTIVE_CHANNEL_IDS", "")
ACTIVE_CHANNEL_IDS = (
    {int(cid.strip()) for cid in _raw_channels.split(",") if cid.strip()}
    if _raw_channels.strip()
    else set()
)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def query_ollama(session: aiohttp.ClientSession, prompt: str) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    async with session.post(
        f"{OLLAMA_BASE_URL}/api/chat", json=payload
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["message"]["content"]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if ACTIVE_CHANNEL_IDS:
        print(f"Listening in channels: {ACTIVE_CHANNEL_IDS}")
    else:
        print("Listening in all channels")


@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # If channel restrictions are set, only respond in those channels
    if ACTIVE_CHANNEL_IDS and message.channel.id not in ACTIVE_CHANNEL_IDS:
        await bot.process_commands(message)
        return

    # Only respond when mentioned or in DMs
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions

    if not (is_dm or is_mentioned):
        await bot.process_commands(message)
        return

    # Strip the mention from the message content
    content = message.content.replace(f"<@{bot.user.id}>", "").strip()
    if not content:
        await message.reply("Yes?")
        return

    async with message.channel.typing():
        try:
            async with aiohttp.ClientSession() as session:
                response = await query_ollama(session, content)
            # Discord has a 2000 char message limit
            if len(response) > 2000:
                for i in range(0, len(response), 2000):
                    await message.reply(response[i : i + 2000])
            else:
                await message.reply(response)
        except aiohttp.ClientError as e:
            await message.reply(f"⚠️ Could not reach Ollama: `{e}`")
        except Exception as e:
            await message.reply(f"⚠️ Something went wrong: `{e}`")

    await bot.process_commands(message)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(DISCORD_TOKEN)
