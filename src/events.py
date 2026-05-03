import asyncio
import json
import time
import logging

import aiohttp
import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_BLUE, mocking_font, curse_font, fetch_member, _delete_after,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, is_insured, _ct_today, _ensure_user,
)
from src.permissions import (
    check_rate_limit,
    _wrong_channel_reply,
)
from src.persistence import (
    init_db_state, save_economy, save_ragebait, save_mock, save_tax, save_curse, get_guild_cfg,
    load_restart_msg, clear_restart_msg, load_and_clear_ephemeral_msgs,
)
from src.ai import (
    check_ollama_connected, keep_typing,
    stream_ollama, finalize, respond,
    _norm_puzzle_answer,
)
from src.config import (
    ACTIVE_CHANNEL_IDS, SHOP_TAX_PER_MESSAGE,
    SHOP_TAX_DURATION_SECS, SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD,
)
from src import state
from src.games.blackjack import draw_card, hand_value, build_blackjack_display, _blackjack_stand
from src.games.hangman import _process_hangman_guess
from src.leveling import grant_xp as _grant_xp


async def _roast_soundboard_spam(bot, guild_id: int, user_id: int):
    """Generate a roast for soundboard spam using the ragebait system."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    member = await fetch_member(guild, user_id)
    if member is None:
        return

    roast_system = (
        "You are an expert at crafting witty, cutting roasts. Your goal is to roast someone "
        "for spamming soundboard sounds in a voice channel. "
        "Rules: be specific to the target by referring to them by name, be witty and sarcastic rather than just mean, "
        "make fun of them for the spam/spam in general, keep it under 150 characters, "
        "and make it feel natural — like something a friend would say. "
        "Output only the roast with no preamble, explanation, or quotation marks."
    )
    prompt = (
        f"Write a witty roast for {member.display_name} for spamming soundboard sounds in voice chat. "
        "Be sarcastic and funny. Do not use @ symbols."
    )

    try:
        # Create a fake channel/message context for the streaming function
        voice_channel = member.voice.channel if member.voice else None
        if voice_channel is None:
            return

        # Find the first AI channel in the guild to send the roast to
        cfg = get_guild_cfg(guild_id)
        ai_channels = cfg.get("ai_channels", [])
        text_channel = None
        if ai_channels:
            for ch_id in ai_channels:
                ch = guild.get_channel(ch_id)
                if ch and ch.permissions_for(guild.me).send_messages:
                    text_channel = ch
                    break
        if text_channel is None:
            return

        placeholder = await text_channel.send("...")
        typing_task = asyncio.create_task(keep_typing(text_channel))
        try:
            async with aiohttp.ClientSession() as session:
                full_response = await stream_ollama(session, [
                    {"role": "system", "content": roast_system},
                    {"role": "user", "content": prompt},
                ], placeholder)
            await finalize(placeholder, text_channel, f"{member.mention} {full_response}")
        except Exception as e:
            await placeholder.edit(content=f"⚠️ {e}")
        finally:
            typing_task.cancel()
    except Exception:
        pass


async def _handle_soundboard_ratelimit(bot, guild_id: int, user_id: int):
    """Check if user exceeded soundboard rate limit; kick if so."""
    now = time.monotonic()
    key = (guild_id, user_id)
    timestamps = state._soundboard_timestamps.setdefault(key, [])
    cutoff = now - SOUNDBOARD_WINDOW_SECS
    state._soundboard_timestamps[key] = [t for t in timestamps if t >= cutoff]
    timestamps = state._soundboard_timestamps[key]
    timestamps.append(now)
    if len(timestamps) <= SOUNDBOARD_MAX_SOUNDS:
        return
    # Threshold exceeded — clear so the same burst doesn't re-trigger
    state._soundboard_timestamps[key] = []
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    member = await fetch_member(guild, user_id)
    if member is None or member.voice is None:
        return

    # Generate roast
    asyncio.create_task(_roast_soundboard_spam(bot, guild_id, user_id))

    # Kick from voice channel
    try:
        await member.move_to(None)  # kick from voice channel
    except (discord.Forbidden, Exception):
        return


async def _auto_daily(message: discord.Message):
    """Award daily coins on first interaction of the day. Sends a short message if awarded."""
    uid = message.author.id
    await _ensure_user(uid)
    today = _ct_today()
    user_data = state.economy["users"][str(uid)]
    if user_data.get("daily_date") == today:
        return
    is_new = user_data.get("last_daily", 0.0) == 0.0
    await add_balance(uid, DAILY_REWARD)
    user_data["daily_date"] = today
    if is_new:
        user_data["last_daily"] = time.time()
    await save_economy(uid=uid)
    greeting = f"Welcome, **{message.author.display_name}**! 🎉 Here are your first" if is_new else "Daily coins ready!"
    await message.channel.send(embed=emb(
        "🪙 Daily Reward",
        f"{greeting} **{DAILY_REWARD:,} 🪙** added. Balance: {await get_balance(uid):,} 🪙",
        C_GREEN,
    ))


async def _passive_ragebait(message: discord.Message, history: list[str]):
    context = "\n".join(history)
    ragebait_system = (
        "You are an expert at crafting ragebait — messages specifically engineered to provoke "
        "an emotional reaction. Your goal is to write something that will genuinely irritate, "
        "annoy, or get under the skin of the target. "
        "Rules: be specific to the target by referring to them by name (no @ symbols), be witty and cutting rather than just insulting, "
        "use irony or condescension where effective, keep it under 200 characters, "
        "and make it feel natural — like something a person would actually say. "
        "Output only the ragebait message with no preamble, explanation, or quotation marks."
    )
    prompt = (
        f"Write a ragebait reply aimed at {message.author.display_name} based on what they just said. "
        f"Their recent messages for context:\n{context}\n"
        "Make it personal, pointed, and reactive to what they actually wrote. Do not use @ symbols."
    )
    placeholder = await message.reply("...")
    typing_task = asyncio.create_task(keep_typing(message.channel))
    try:
        async with aiohttp.ClientSession() as session:
            full_response = await stream_ollama(session, [
                {"role": "system", "content": ragebait_system},
                {"role": "user", "content": prompt},
            ], placeholder)
        await finalize(placeholder, message.channel, f"{message.author.mention} {full_response}")
    except Exception as e:
        await placeholder.edit(content=f"⚠️ {e}")
    finally:
        typing_task.cancel()



class EventsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        logging.info(f"Logged in as {self.bot.user} ({self.bot.user.id})")

        await init_db_state()

        # Edit the restart confirmation message if one was saved
        restart_data = await load_restart_msg()
        if restart_data:
            try:
                channel = await self.bot.fetch_channel(restart_data["channel_id"])
                msg = await channel.fetch_message(restart_data["message_id"])
                await msg.edit(embed=emb("✅ Restarted", "Bot has restarted.", C_GREEN))
            except Exception:
                pass
            await clear_restart_msg()

        # Delete all ephemeral messages that survived the restart
        records = await load_and_clear_ephemeral_msgs()
        for record in records:
            try:
                channel = await self.bot.fetch_channel(record["channel_id"])
                msg = await channel.fetch_message(record["message_id"])
                await msg.delete()
            except Exception:
                pass

    @commands.Cog.listener()
    async def global_command_channel_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return True
        if ctx.command and ctx.command.name in ("settings", "clear"):
            return True  # always allow !settings and !clear in any channel

        cfg = get_guild_cfg(ctx.guild.id)

        # Allow searchquote if bypass is enabled
        if ctx.command and ctx.command.name == "searchquote":
            if cfg.get("quote_bypass_restrictions", False):
                return True

        # !quote (save/display) always allowed in any channel
        if ctx.command and ctx.command.name == "quote":
            return True

        # Check blacklist first (deny)
        command_blacklist = cfg.get("command_blacklist", [])
        if ctx.channel.id in command_blacklist:
            return False

        # Check whitelist (allow only if specified)
        command_whitelist = cfg.get("command_whitelist", [])
        if command_whitelist and ctx.channel.id not in command_whitelist:
            return False

        return True

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            logging.debug(f"[debug] {error}")
            return
        from src.level_unlocks import LevelLocked
        if isinstance(error, LevelLocked):
            return  # gate already sent its own message
        if isinstance(error, commands.CheckFailure):
            cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
            command_whitelist = cfg.get("command_whitelist", [])
            if command_whitelist:
                names = " ".join(f"<#{cid}>" for cid in command_whitelist)
                msg = f"Commands are only allowed in: {names}"
            else:
                msg = "Commands are not allowed in this channel."
            await _wrong_channel_reply(ctx, msg)
            return
        state.audit_log.append({
            "time": time.time(),
            "user": f"{ctx.author.display_name} ({ctx.author.id})",
            "command": ctx.message.content[:100],
            "error": f"{type(error).__name__}: {error}",
        })
        raise error

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        state.stats_commands_ran += 1
        state.stats_commands_today += 1
        if ctx.guild and not ctx.author.bot:
            xp, leveled_up = await _grant_xp(ctx.author.id, "cmd", guild_id=ctx.guild.id)
            if leveled_up and get_guild_cfg(ctx.guild.id).get("levelup_channel"):
                cog = self.bot.cogs.get("LevelingCog")
                if cog and isinstance(ctx.author, discord.Member):
                    asyncio.create_task(cog._announce_levelup(ctx.author, ctx.guild.id))

    @commands.Cog.listener()
    async def on_socket_raw_receive(self, msg):
        """Handle raw Discord gateway events; intercept VOICE_CHANNEL_EFFECT_SEND for soundboard rate-limiting."""
        try:
            data = json.loads(msg.decode("utf-8") if isinstance(msg, bytes) else msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if data.get("t") != "VOICE_CHANNEL_EFFECT_SEND":
            return
        d = data.get("d", {})
        if "sound_id" not in d:   # emoji reactions have no sound_id
            return
        try:
            guild_id = int(d["guild_id"])
            user_id  = int(d["user_id"])
        except (KeyError, ValueError, TypeError):
            return
        cfg = get_guild_cfg(guild_id)
        if user_id not in cfg.get("soundboard_ratelimit", []):
            return
        await _handle_soundboard_ratelimit(self.bot, guild_id, user_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author == self.bot.user:
            return

        uid = message.author.id
        content_lower = message.content.strip().lower()

        state.stats_messages_seen += 1
        state.stats_messages_today += 1

        # Message XP (non-command messages only)
        if message.guild and not message.content.startswith("!"):
            xp, leveled_up = await _grant_xp(uid, "msg", guild_id=message.guild.id)
            if leveled_up and isinstance(message.author, discord.Member) and get_guild_cfg(message.guild.id).get("levelup_channel"):
                cog = self.bot.cogs.get("LevelingCog")
                if cog:
                    asyncio.create_task(cog._announce_levelup(message.author, message.guild.id))

        # Passive ragebait: track targeted users and fire at 50% chance
        _rage_channel = state.active_ragebaits.get(uid, {}).get("channel_id")
        if uid in state.active_ragebaits and not message.content.startswith("!") and (_rage_channel is None or message.channel.id == _rage_channel):
            # Only proceed if AI is online
            if await check_ollama_connected():
                rage = state.active_ragebaits[uid]
                rage["history"].append(f"[{message.author.display_name}]: {message.content[:200]}")
                rage["remaining"] -= 1
                if rage["remaining"] <= 0:
                    del state.active_ragebaits[uid]
                await save_ragebait()

                asyncio.create_task(_passive_ragebait(message, list(rage["history"])))

        # Mock: track mocked users and repeat their messages in mocking font
        _mock_channel = state.active_mocks.get(uid, {}).get("channel_id")
        if uid in state.active_mocks and not message.content.startswith("!") and (_mock_channel is None or message.channel.id == _mock_channel):
            mock = state.active_mocks[uid]
            mocked = mocking_font(message.content)
            await message.channel.send(mocked)
            mock["remaining"] -= 1
            if mock["remaining"] <= 0:
                del state.active_mocks[uid]
            await save_mock()

        # Tax: deduct coins from users who have an active tax on them
        _tax_channel = state.active_taxes.get(uid, {}).get("channel_id")
        if uid in state.active_taxes and not message.content.startswith("!") and (_tax_channel is None or message.channel.id == _tax_channel):
            tax_data = state.active_taxes[uid]
            tax_type = tax_data.get("type", "tax")

            if "activated_at" in tax_data and time.time() - tax_data["activated_at"] > SHOP_TAX_DURATION_SECS:
                del state.active_taxes[uid]
                await save_tax(state.active_taxes)
            elif await is_insured(uid, "tax"):
                pass
            else:
                tax_master_id = tax_data["master"]
                tax_label = tax_type.capitalize()
                tax_emoji = tax_data.get("emoji", "💰")
                if await deduct_balance(uid, SHOP_TAX_PER_MESSAGE):
                    await add_balance(tax_master_id, SHOP_TAX_PER_MESSAGE)
                    tax_master = await fetch_member(message.guild, tax_master_id)
                    if tax_master:
                        master_name = tax_master.display_name
                    else:
                        try:
                            user = await self.bot.fetch_user(tax_master_id)
                            master_name = user.display_name
                        except (discord.NotFound, discord.HTTPException):
                            master_name = str(tax_master_id)
                    await message.channel.send(f"{tax_emoji} **{message.author.display_name}** paid a **{SHOP_TAX_PER_MESSAGE:,} 🪙** {tax_label} tax to **{master_name}**")

        # Curse: corrupt cursed users' messages
        if uid in state.active_curses and not message.content.startswith("!"):
            curse = state.active_curses[uid]
            cursed = curse_font(message.content)
            await message.channel.send(cursed)
            curse["remaining"] -= 1
            if curse["remaining"] <= 0:
                del state.active_curses[uid]
            await save_curse(state.active_curses)

        # Auto-award daily on any bot interaction (skip blacklisted channels)
        _is_dm = isinstance(message.channel, discord.DMChannel)
        _blacklisted = (
            not _is_dm
            and message.guild
            and message.channel.id in get_guild_cfg(message.guild.id).get("command_blacklist", [])
        )
        if not _blacklisted and (
            message.content.startswith("!")
            or self.bot.user in message.mentions
            or _is_dm
            or message.channel.id in state.active_hangman_games
            or uid in state.active_blackjack_games
        ):
            await _auto_daily(message)

        # Intercept hit / stand for active blackjack (with or without ! prefix)
        if content_lower in ("!hit", "!stand", "hit", "stand") and uid in state.active_blackjack_games and state.active_blackjack_games[uid].get("channel_id") == message.channel.id:
            game = state.active_blackjack_games[uid]
            if content_lower in ("!hit", "hit"):
                card = draw_card(game["deck"])
                game["player_hand"].append(card)
                pval = hand_value(game["player_hand"])
                display = build_blackjack_display(
                    game["player_hand"], game["dealer_hand"], pval, hide_dealer=True,
                    username=message.author.display_name,
                )
                if pval > 21:
                    del state.active_blackjack_games[uid]
                    await message.channel.send(embed=emb(
                        "💥 Bust!",
                        display + f"\n\n**{message.author.display_name}** loses **{game['amount']:,} 🪙**. Balance: {await get_balance(uid):,} 🪙",
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

        # Intercept puzzle answers (must run before channel/AI guards)
        cid = message.channel.id
        if cid in state.active_puzzles and not message.content.startswith("!"):
            puzzle = state.active_puzzles[cid]
            invited = puzzle.get("invited_ids")
            if not invited or uid in invited:
                guess = message.content.strip()
                expected = puzzle["answer"]
                if _norm_puzzle_answer(guess) == _norm_puzzle_answer(expected):
                    reward = puzzle["reward"]
                    del state.active_puzzles[cid]
                    await add_balance(uid, reward)
                    await message.channel.send(embed=emb(
                        "✅ Correct!",
                        f"{message.author.mention} got it!\n**Answer:** `{expected}`\n+**{reward:,} 🪙** (Balance: {await get_balance(uid):,} 🪙)",
                        C_GREEN,
                    ))
                    return

        # Intercept free-text hangman guesses (no prefix needed when game is active)
        # Only single-letter guesses via free-text; full words require !guess command
        cid = message.channel.id
        if cid in state.active_hangman_games and not message.content.startswith("!"):
            guess = message.content.lower().strip()
            if guess and guess.isalpha() and len(guess) == 1:
                asyncio.create_task(_delete_after(message))
                await _process_hangman_guess(message.channel, uid, cid, guess, message.author.display_name)
                return

        # AI enabled guard
        if not state.bot_settings.get("ai_enabled", True):
            await self.bot.process_commands(message)
            return

        # Channel guard
        if ACTIVE_CHANNEL_IDS and message.channel.id not in ACTIVE_CHANNEL_IDS:
            await self.bot.process_commands(message)
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        # Only respond to mentions if the message starts with the mention
        is_mentioned = self.bot.user in message.mentions and message.content.strip().startswith(f"<@{self.bot.user.id}>")
        ai_thread = state.ai_threads.get(message.channel.id)
        in_ai_thread = ai_thread is not None

        # Ragebait and mock take precedence over normal mentions (only in the purchase channel)
        _rage_in_channel = uid in state.active_ragebaits and (_rage_channel is None or message.channel.id == _rage_channel)
        _mock_in_channel = uid in state.active_mocks and (_mock_channel is None or message.channel.id == _mock_channel)
        if _rage_in_channel or _mock_in_channel:
            await self.bot.process_commands(message)
            return

        # Check channel restrictions for mentions (AI channels and blacklist)
        if is_mentioned and not is_dm and message.guild:
            cfg = get_guild_cfg(message.guild.id)
            ai_channels = cfg.get("ai_channels", [])
            command_blacklist = cfg.get("command_blacklist", [])

            # Determine if channel is allowed for AI
            channel_allowed = True
            if ai_channels:
                # If AI channels configured, must be in one of them
                channel_allowed = message.channel.id in ai_channels
            elif message.channel.id in command_blacklist:
                # If no AI channels but channel is blacklisted, not allowed
                channel_allowed = False

            if not channel_allowed:
                if ai_channels:
                    names = " ".join(f"<#{cid}>" for cid in ai_channels)
                else:
                    # Show where AI IS allowed (inverse of blacklist)
                    all_channels = [ch.id for ch in message.guild.text_channels if ch.id not in command_blacklist]
                    names = " ".join(f"<#{cid}>" for cid in all_channels) if all_channels else "no channels"
                await _wrong_channel_reply(message, f"AI commands are only allowed in: {names}")
                await self.bot.process_commands(message)
                return

        if not (is_dm or is_mentioned or in_ai_thread):
            await self.bot.process_commands(message)
            return

        # Skip bare commands inside AI threads (let process_commands handle them)
        if in_ai_thread and not is_mentioned and not is_dm and message.content.startswith("!"):
            await self.bot.process_commands(message)
            return

        content = message.content.replace(f"<@{self.bot.user.id}>", "").strip()
        if not content:
            await message.reply("Yes?")
            await self.bot.process_commands(message)
            return

        if check_rate_limit(uid):
            await message.reply("⚠️ Slow down! Please wait a moment before sending another message.")
            await self.bot.process_commands(message)
            return

        if ai_thread is not None:
            # Gate AI thread to invited participants only
            if uid not in ai_thread["invited_ids"]:
                await self.bot.process_commands(message)
                return
            guild_id = message.guild.id if message.guild else None
            await respond(message.channel, uid, content, message, guild_id=guild_id, author_name=message.author.display_name)
        else:
            guild_id = message.guild.id if message.guild else None
            await respond(message.channel, uid, content, message, guild_id=guild_id, author_name=message.author.display_name)

        await self.bot.process_commands(message)




async def setup(bot):
    await bot.add_cog(EventsCog(bot))
