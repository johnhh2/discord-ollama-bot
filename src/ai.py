import asyncio
import json
import time

import aiohttp
import discord

from src import state
from src.config import OLLAMA_BASE_URL, OLLAMA_MODEL
from src.helpers import emb, C_RED, _log_audit
from src.economy import (
    deduct_balance, get_balance,
    get_guild_ask_model, get_guild_roleplay_model,
)
from src.persistence import save_ai_threads


# Global semaphore — only one Ollama request runs at a time to avoid GPU overload.
ollama_semaphore = asyncio.Semaphore(1)

# Per-response token cap (~1500 words) and wall-clock timeout for any single
# Ollama call. Both bound how long one user can hold the semaphore.
OLLAMA_NUM_PREDICT = 2048
OLLAMA_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)

# Per-user input-token budget. Bucket holds at most TOKEN_BUCKET_MAX tokens
# and refills continuously at 512 tokens / 60 s = ~8.53 tokens/sec. Token
# count is approximated from prompt char length (~4 chars/token).
TOKEN_BUCKET_MAX = 2048
TOKEN_BUCKET_REFILL_PER_SEC = 512 / 60
_user_token_buckets: dict = {}  # user_id -> (tokens_remaining: float, last_update: float)


def _estimate_tokens(messages: list) -> int:
    total_chars = sum(len(m.get("content", "")) for m in messages)
    return max(1, total_chars // 4)


async def check_token_budget_or_notify(ctx, prompt_text: str = "") -> bool:
    """Pre-flight token-budget check for AI commands.

    Sends the rate-limit embed and returns False when the user's bucket
    can't even cover what they typed (so we don't waste effort deducting
    the coin cost or spinning up a Discord thread that's about to fail).
    Returns True when the call should proceed; the authoritative spend
    still happens inside `stream_ollama`. user_id=None / godmode pass.
    """
    if ctx.author.id in state.godmode_users:
        return True
    cost = max(1, len(prompt_text) // 4)
    available = _peek_token_budget(ctx.author.id)
    if available >= cost:
        return True
    needed = min(cost, TOKEN_BUCKET_MAX) - available
    wait_s = needed / TOKEN_BUCKET_REFILL_PER_SEC
    await ctx.send(embed=emb(
        "⏳ AI Rate Limit",
        f"You've used your AI token budget. Try again in **{wait_s:.0f}s**.\n"
        f"Budget refills at 512 tokens / minute (max {TOKEN_BUCKET_MAX}).",
        C_RED,
    ))
    return False


def _peek_token_budget(user_id: int) -> float:
    """Return the user's current token balance without spending. Used by
    the `!ai` status display so users can see what they have left."""
    if user_id in state.godmode_users:
        return float(TOKEN_BUCKET_MAX)
    now = time.monotonic()
    tokens, last = _user_token_buckets.get(user_id, (float(TOKEN_BUCKET_MAX), now))
    return min(float(TOKEN_BUCKET_MAX), tokens + (now - last) * TOKEN_BUCKET_REFILL_PER_SEC)


def _check_token_budget(user_id: int, cost: int) -> float | None:
    """Refill the user's bucket and try to spend `cost` tokens.

    Returns None if the spend was allowed. Otherwise returns the number of
    seconds the user must wait until the bucket has accumulated enough
    tokens to cover `cost`. Bot-admin godmode users bypass the limit.
    """
    if user_id in state.godmode_users:
        return None
    now = time.monotonic()
    tokens, last = _user_token_buckets.get(user_id, (float(TOKEN_BUCKET_MAX), now))
    tokens = min(float(TOKEN_BUCKET_MAX), tokens + (now - last) * TOKEN_BUCKET_REFILL_PER_SEC)
    if tokens >= cost:
        _user_token_buckets[user_id] = (tokens - cost, now)
        return None
    _user_token_buckets[user_id] = (tokens, now)
    needed = min(cost, TOKEN_BUCKET_MAX) - tokens
    return needed / TOKEN_BUCKET_REFILL_PER_SEC


ASK_SYSTEM_PROMPT = (
    "You are a knowledgeable and helpful assistant. "
    "Answer questions clearly, accurately, and concisely. "
    "Use markdown formatting where it improves readability (e.g. bullet points, code blocks). "
    "If you are uncertain about something, say so. "
    "Do not make up information."
)

STORY_SYSTEM_PROMPT = (
    "You are a creative fiction writer. "
    "When given a topic, characters, or scenario, write an original, self-contained short story "
    "(roughly 400-700 words) with vivid descriptive language, distinct character voices, "
    "and strong narrative momentum. "
    "The story should feel complete — a beginning that hooks the reader, a middle that escalates, "
    "and a satisfying conclusion. "
    "Do not summarize what you are about to write — just write the story."
)

FEATURE_COSTS: dict = {
    "ask": 200,
    "story": 500,
    "continue": 10,
    "roleplay": 500,
    "rpg": 500,
}

_FEATURE_LABELS: dict = {
    "ask": "Asking",
    "story": "Writing a story",
    "continue": "Continuing",
    "roleplay": "Starting a roleplay",
    "rpg": "Starting an RPG adventure",
}


async def enforce_cost(ctx, feature: str) -> bool:
    """Deduct the coin cost for *feature*. Returns True if the user can proceed."""
    uid = ctx.author.id
    cost = 0 if uid in state.godmode_users else FEATURE_COSTS.get(feature, 0)
    if cost == 0:
        return True
    if not await deduct_balance(uid, cost):
        label = _FEATURE_LABELS.get(feature, feature.title())
        await ctx.send(embed=emb(
            "💸 Insufficient Funds",
            f"{label} costs **{cost:,} 🪙**. Balance: {await get_balance(uid):,} 🪙",
            C_RED,
        ))
        return False
    return True


async def insufficient_funds(ctx_or_send, uid: int, *, label: str = "") -> None:
    """Send a standard Insufficient Funds embed."""
    from discord.ext import commands
    desc = f"{label + ' ' if label else ''}Balance: {await get_balance(uid):,} 🪙"
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
    user_id: int = None,
) -> str:
    if not state.bot_settings.get("ai_enabled", True):
        await placeholder.edit(
            content="",
            embed=emb("🤖 AI Offline", "Passive AI responses are currently disabled.", C_RED)
        )
        return ""
    if user_id is not None:
        wait_s = _check_token_budget(user_id, _estimate_tokens(messages))
        if wait_s is not None:
            try:
                await placeholder.edit(
                    content="",
                    embed=emb(
                        "⏳ AI Rate Limit",
                        f"You've used your AI token budget. Try again in **{wait_s:.0f}s**.\n"
                        f"Budget refills at 512 tokens / minute (max {TOKEN_BUCKET_MAX}).",
                        C_RED,
                    ),
                )
            except Exception:
                pass
            return ""
    if model:
        used_model = model
    elif guild_id:
        used_model = get_guild_ask_model(guild_id)
    else:
        used_model = OLLAMA_MODEL
    payload = {
        "model": used_model,
        "messages": messages,
        "stream": True,
        "options": {"num_predict": OLLAMA_NUM_PREDICT},
    }

    full_response = ""
    last_edit = 0.0
    EDIT_INTERVAL = 0.8

    if ollama_semaphore.locked():
        try:
            await placeholder.edit(content="⏳ Another AI request is running. You're next...")
        except Exception:
            pass

    truncated = False
    async with ollama_semaphore:
        try:
            async with session.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json=payload,
                timeout=OLLAMA_REQUEST_TIMEOUT,
            ) as resp:
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
        except asyncio.TimeoutError:
            truncated = True

    if truncated:
        full_response += "\n\n⏱️ *Response cut off after 2 minutes.*"

    return full_response


async def finalize(placeholder: discord.Message, channel: discord.abc.Messageable, text: str):
    chunks = [text[i:i + 2000] for i in range(0, max(len(text), 1), 2000)]
    await placeholder.edit(content=chunks[0])
    for chunk in chunks[1:]:
        await channel.send(chunk)


async def _execute_ollama_stream(
    channel, reply_to, messages, history,
    guild_id=None, model=None, placeholder=None, user_id=None
):
    if placeholder is None:
        placeholder = await reply_to.reply("...")
    typing_task = asyncio.create_task(keep_typing(channel))
    author = f"{reply_to.author.display_name} ({reply_to.author.id})"
    command = reply_to.content[:100]
    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(
                session, messages, placeholder,
                guild_id=guild_id, model=model, user_id=user_id,
            )
        if not full_response:
            # AI disabled or per-user token-budget denial — placeholder
            # already shows the explanation. Drop the unanswered user turn.
            if history and history[-1].get("role") == "user":
                history.pop()
            return
        history.append({"role": "assistant", "content": full_response})
        state.stats_ai_responses_today += 1
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
    from src.helpers import get_system_prompt
    channel_id = channel.id

    # AI thread session (ask, story, roleplay, rpg) — shared per-thread history
    ai_thread = state.ai_threads.get(channel_id)
    if ai_thread is not None:
        history = ai_thread["history"]
        sp = system_prompt or ai_thread.get("system_prompt") or get_system_prompt(channel_id)
        kind = ai_thread["kind"]
        model = (
            get_guild_roleplay_model(guild_id) if guild_id and kind in ("roleplay", "rpg") else None
        )
    else:
        history = state.channel_histories[channel_id]
        sp = system_prompt or get_system_prompt(channel_id)
        model = None

    formatted_content = f"{author_name}: {content}" if author_name else content
    history.append({"role": "user", "content": formatted_content})
    messages = [{"role": "system", "content": sp}] + list(history)

    placeholder = await channel.send("...") if isinstance(channel, discord.Thread) else None
    await _execute_ollama_stream(
        channel, reply_to, messages, history,
        model=model, guild_id=guild_id, placeholder=placeholder,
        user_id=user_id,
    )
    if ai_thread is not None:
        await save_ai_threads()


def _norm_puzzle_answer(s: str) -> str:
    """Normalize a puzzle answer for comparison: lowercase, collapse whitespace."""
    return " ".join(s.lower().split())
