import asyncio
import json
import time

import aiohttp
import discord

from src.config import OLLAMA_BASE_URL, OLLAMA_MODEL
from src.helpers import emb, C_RED, _log_audit
from src.economy import (
    deduct_balance, get_balance,
    get_guild_ask_model, get_guild_roleplay_model,
)
from src.persistence import save_roleplay_state


# Global semaphore — only one Ollama request runs at a time to avoid GPU overload.
ollama_semaphore = asyncio.Semaphore(1)


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

FEATURE_COSTS: dict = {
    "ask": 200,
    "fanfic": 500,
    "continue": 10,
    "roleplay": 500,
    "rpg": 500,
}

_FEATURE_LABELS: dict = {
    "ask": "Asking",
    "fanfic": "Fan fiction",
    "continue": "Continuing",
    "roleplay": "Starting a roleplay",
    "rpg": "Starting an RPG adventure",
}


async def enforce_cost(ctx, feature: str) -> bool:
    """Deduct the coin cost for *feature*. Returns True if the user can proceed."""
    from src import state
    uid = ctx.author.id
    cost = 0 if uid in state.godmode_users else FEATURE_COSTS.get(feature, 0)
    if cost == 0:
        return True
    if not deduct_balance(uid, cost):
        label = _FEATURE_LABELS.get(feature, feature.title())
        await ctx.send(embed=emb(
            "💸 Insufficient Funds",
            f"{label} costs **{cost:,} 🪙**. Balance: {get_balance(uid):,} 🪙",
            C_RED,
        ))
        return False
    return True


async def insufficient_funds(ctx_or_send, uid: int, *, label: str = "") -> None:
    """Send a standard Insufficient Funds embed."""
    from discord.ext import commands
    desc = f"{label + ' ' if label else ''}Balance: {get_balance(uid):,} 🪙"
    e = emb("💸 Insufficient Funds", desc, C_RED)
    if callable(ctx_or_send) and not isinstance(ctx_or_send, commands.Context):
        await ctx_or_send(embed=e)
    else:
        await ctx_or_send.send(embed=e)


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


async def keep_typing(channel: discord.abc.Messageable):
    try:
        while True:
            await channel.trigger_typing()
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        pass


async def stream_ollama(
    session: aiohttp.ClientSession,
    messages: list,
    placeholder: discord.Message,
    model: str = None,
    guild_id: int = None,
) -> str:
    from src import state
    if not state.bot_settings.get("ai_enabled", True):
        await placeholder.edit(
            content="",
            embed=emb("🤖 AI Offline", "Passive AI responses are currently disabled.", C_RED)
        )
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


async def _execute_ollama_stream(
    channel, reply_to, messages, history,
    guild_id=None, model=None, placeholder=None
):
    if placeholder is None:
        placeholder = await reply_to.reply("...")
    typing_task = asyncio.create_task(keep_typing(channel))
    author = f"{reply_to.author.display_name} ({reply_to.author.id})"
    command = reply_to.content[:100]
    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(
                session, messages, placeholder, guild_id=guild_id, model=model
            )
        history.append({"role": "assistant", "content": full_response})
        from src import state as _state
        _state.stats_ai_responses_today += 1
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
    from src import state
    from src.helpers import get_system_prompt
    channel_id = channel.id
    history = state.channel_histories[channel_id]
    system_prompt = system_prompt or get_system_prompt(channel_id)

    formatted_content = f"{author_name}: {content}" if author_name else content
    history.append({"role": "user", "content": formatted_content})
    messages = [{"role": "system", "content": system_prompt}] + list(history)

    placeholder = await channel.send("...") if isinstance(channel, discord.Thread) else None
    await _execute_ollama_stream(
        channel, reply_to, messages, history, guild_id=guild_id, placeholder=placeholder
    )


async def respond_roleplay(
    channel: discord.abc.Messageable,
    user_id: int,
    content: str,
    reply_to: discord.Message,
    author_name: str = None,
):
    from src import state
    rp = state.active_roleplays[user_id]
    history_key = rp.get("history_owner", user_id)
    history = state.roleplay_histories.setdefault(history_key, [])

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
    await _execute_ollama_stream(
        channel, reply_to, messages, history, model=model, placeholder=placeholder
    )
    save_roleplay_state()


def _norm_puzzle_answer(s: str) -> str:
    """Normalize a puzzle answer for comparison: lowercase, collapse whitespace."""
    return " ".join(s.lower().split())
