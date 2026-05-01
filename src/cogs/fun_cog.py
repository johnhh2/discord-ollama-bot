import asyncio
import json
import os
import random
import time
import datetime
import logging
import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import commands, tasks
from discord import ui

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_ORANGE, C_BLUE, C_PURPLE, C_GREY,
    mocking_font, curse_font, parse_amount, send_ephemeral, resolve_role,
    fetch_member, toggle_member_role, shop_charge, _render_race,
    _delete_after, _edit_board, get_memory_mb, format_uptime, get_version,
    get_system_prompt, _log_audit, log_bot_permission_error,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, get_guild_house_balance,
    add_guild_house, drain_bot_balance_into_lottery, announce_new_lottery,
    is_insured, get_guild_ask_model, get_guild_roleplay_model,
    get_guild_coding_model, _ct_now, _ct_today, do_daily_reset, _ensure_user,
)
from src.permissions import (
    is_admin, is_server_admin, can_manage_settings, check_rate_limit,
    check_channel, check_game_channel, check_ai_channel, check_puzzle_channel,
    check_chess_channel, _wrong_channel_reply,
)
from src.persistence import (
    _load_json, _save_json, save_economy, save_insurance, save_guild_settings,
    save_bot_settings, save_bot_admins, save_godmode_users, save_bot_roles,
    save_chess_games, save_ragebait, save_mock, save_rigged_slots,
    save_gambler_streak, save_roleplay_state, save_fanfic_histories,
    save_quote_log, save_saved_quotes, save_tax, save_curse, save_lottery,
    load_lottery, load_saved_quotes, get_guild_cfg,
)
from src.ai import (
    enforce_cost, insufficient_funds, check_ollama_connected, keep_typing,
    stream_ollama, finalize, _execute_ollama_stream, respond, respond_roleplay,
    ASK_SYSTEM_PROMPT, FANFIC_SYSTEM_PROMPT, FEATURE_COSTS, _norm_puzzle_answer,
)
from src.config import (
    OLLAMA_MODEL, OLLAMA_BASE_URL, SYSTEM_PROMPT, HISTORY_LIMIT,
    RATE_LIMIT_SECONDS, RULE34_API_KEY, RULE34_USER_ID, RACE_TRACK_LEN,
    ACTIVE_CHANNEL_IDS, DISCORD_TOKEN, RESTART_MSG_FILE, EPHEMERAL_MSG_FILE,
    SLOT_REEL, SLOT_JACKPOT_SEED, SLOT_JACKPOT_CONTRIB, SLOT_HOUSE_CHANCE,
    SLOT_MIN_BET, SLOT_MULT_JACKPOT, SLOT_MULT_3BAR, SLOT_MULT_3BELL,
    SLOT_MULT_3LEMON, SLOT_MULT_3CHERRY, SLOT_MULT_2CHERRY, SLOT_MULT_1CHERRY,
    SLOT_JACKPOT_BONUS_MIN_BET, SLOT_JACKPOT_BONUS_MAX_BET, SLOT_JACKPOT_BONUS_MAX_MULT,
    HANGMAN_MAX_WRONG, HANGMAN_BASE_REWARD, HANGMAN_LENGTH_OFFSET,
    HANGMAN_LENGTH_MULT, HANGMAN_UNIQUE_MULT, HANGMAN_RARE_MULT, HANGMAN_ULTRA_RARE_MULT,
    BLACKJACK_NATURAL_MULT, SCRATCH_SYMBOLS, SCRATCHOFF_MAX_DAILY, SCRATCHOFF_PAYOUTS,
    SHOP_NICKNAME_SELF_COST, SHOP_NICKNAME_REMOVE_COST, SHOP_NICKNAME_OTHER_COST,
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_REMOVE_COST, SHOP_ROLE_MOVE_COST,
    SHOP_ROLECOLOR_COST, SHOP_CHANNEL_COST, SHOP_INSURANCE_COST, SHOP_TAX_COST,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_TAX_PER_MESSAGE,
    SHOP_TAX_DURATION_SECS, SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD, DAILY_RESET_HOUR, INITIAL_BOT_ADMIN_ID,
)
from src import state


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
        return [p for p in data if isinstance(p, dict) and p.get("file_url")
                and not p["file_url"].lower().split("?")[0].endswith((".mp4", ".webm"))]

    # Try a random page first (pages 0–19 = up to 2000 posts), fall back to page 0
    rand_pid = random.randint(0, 19)
    if rand_pid > 0:
        posts = await _fetch_pid(rand_pid)
        if posts:
            return posts
    return await _fetch_pid(0)




class FunCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="rule34", aliases=["r34"])
    async def cmd_rule34(self, ctx: commands.Context, *, tags: str = ""):
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
        tag_parts = [w for w in tags.strip().split()]
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
                search_tags = "+".join(tag_parts) if tag_parts else "solo"
                posts = _filter_banned(await _r34_fetch(session, search_tags + ban_query))
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
        tags_str = ", ".join(post.get("tags", "").split())
        embed.set_footer(text=f"Score: {post.get('score', '?')} | Rating: {post.get('rating', '?')} | Tags: {tags_str}")
        msg = await ctx.send(embed=embed)
        _r34_last_msg[(ctx.channel.id, ctx.author.id)] = msg


    @commands.command(name="ew")
    async def cmd_ew(self, ctx: commands.Context):
        key = (ctx.channel.id, ctx.author.id)
        msg = _r34_last_msg.pop(key, None)
        if msg is None:
            return
        try:
            await msg.delete()
        except discord.NotFound:
            pass


    @commands.command(name="quote")
    async def cmd_quote(self, ctx: commands.Context):
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

            if replied_msg.author == self.bot.user:
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


    @commands.command(name="searchquote", aliases=["quotesearch"])
    async def cmd_searchquote(self, ctx: commands.Context):
        """Find a funny and controversial message from recent chat history.

        Usage:
        - !searchquote — search current channel
        - !searchquote #channel — search specific channel
        - !searchquote @user — search quotes from user in current channel
        - !searchquote #channel @user — search quotes from user in specific channel
        """
        await ctx.typing()

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
                if msg.author == self.bot.user or msg.content.startswith("!") or "http" in msg.content.lower():
                    continue
                if len(msg.content) < 10 or len(msg.content) > 500:
                    continue
                # Filter by user if specified
                if target_user and msg.author.id != target_user.id:
                    continue
                # Skip if already in recent quotes log
                if msg.content in state.quote_log:
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

            # Split messages: 100 with high-energy language, 900 random
            spicy_keywords = {"fuck", "ass", "bitch"}
            spicy_msgs = [m for m in all_messages if any(kw in m["content"].lower() for kw in spicy_keywords)]
            regular_msgs = [m for m in all_messages if m not in spicy_msgs]

            # Sample: up to 100 from spicy, up to 900 from regular
            spicy_sample = spicy_msgs[:100]
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
    - Random absurd/funny/goofy statements without context (bad if not funny)
    - Generic statements about being angry/sad (bad)
    - Bland or neutral messages (bad)

    Example: "I don't even know" = 2/10 (bland)
    Example: "I swear on my life if he does that again I'm going to lose my mind" = 8/10 (bold, emotional, relatable)
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
                state.quote_log.append(selected['content'])
                save_quote_log(state.quote_log)

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


    @commands.command(name="dog")
    async def cmd_dog(self, ctx: commands.Context):
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


    @commands.command(name="cat")
    async def cmd_cat(self, ctx: commands.Context):
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




async def setup(bot):
    await bot.add_cog(FunCog(bot))
