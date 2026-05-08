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
    _wrong_channel_reply,
    get_command_perm,
)
from src.persistence import (
    init_db_state, save_economy, save_ragebait, save_mock, save_tax, save_curse, load_restart_msg, clear_restart_msg, load_and_clear_ephemeral_msgs
)
from src.guild_config import get_guild_cfg
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


async def _log_admin_command(bot, ctx: commands.Context, error: Exception | None = None):
    """Post a log embed to the global admin-log channel if the just-run command
    was gated to bot_admin or server_admin. No-op when the channel isn't configured.
    """
    if ctx.command is None:
        return
    perm = get_command_perm(ctx.command.qualified_name)
    if perm.get("tier") not in ("bot_admin", "server_admin"):
        return
    chan_id = state.bot_settings.get("admin_log_channel")
    if not chan_id:
        return
    try:
        channel = bot.get_channel(int(chan_id)) or await bot.fetch_channel(int(chan_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        return
    guild_name = ctx.guild.name if ctx.guild else "DM"
    guild_id_str = str(ctx.guild.id) if ctx.guild else "—"
    title = "🛡️ Admin Command Error" if error else "🛡️ Admin Command"
    color = C_RED if error else C_BLUE
    desc_lines = [
        f"**User:** {ctx.author.display_name} (`{ctx.author.id}`)",
        f"**Guild:** {guild_name} (`{guild_id_str}`)",
        f"**Channel:** {ctx.channel.mention if hasattr(ctx.channel, 'mention') else ctx.channel}",
        f"**Tier:** {perm.get('tier')}",
        f"**Command:** `{ctx.message.content[:300]}`",
    ]
    if error is not None:
        desc_lines.append(f"**Error:** `{type(error).__name__}: {error}`"[:1000])
    try:
        await channel.send(embed=emb(title, "\n".join(desc_lines), color))
    except (discord.Forbidden, discord.HTTPException):
        pass


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


# {(guild_id, user_id): [monotonic timestamps]} — only consumed by
# _handle_soundboard_ratelimit; in-memory only, never persisted.
_SOUNDBOARD_TIMESTAMPS: dict[tuple[int, int], list[float]] = {}


async def _handle_soundboard_ratelimit(bot, guild_id: int, user_id: int):
    """Check if user exceeded soundboard rate limit; kick if so."""
    now = time.monotonic()
    key = (guild_id, user_id)
    timestamps = _SOUNDBOARD_TIMESTAMPS.setdefault(key, [])
    cutoff = now - SOUNDBOARD_WINDOW_SECS
    _SOUNDBOARD_TIMESTAMPS[key] = [t for t in timestamps if t >= cutoff]
    timestamps = _SOUNDBOARD_TIMESTAMPS[key]
    timestamps.append(now)
    if len(timestamps) <= SOUNDBOARD_MAX_SOUNDS:
        return
    # Threshold exceeded — clear so the same burst doesn't re-trigger
    _SOUNDBOARD_TIMESTAMPS[key] = []
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
    stored = user_data.get("daily_date")
    if stored != today:
        logging.info(
            "[auto_daily] uid=%s firing: stored=%r (type=%s) today=%r (type=%s)",
            uid, stored, type(stored).__name__, today, type(today).__name__,
        )
    if stored == today:
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
    async def on_guild_join(self, guild: discord.Guild):
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None,
            )
        if channel is None:
            return
        try:
            await channel.send(embed=emb(
                "👋 Hello!",
                f"Thanks for adding me to **{guild.name}**! Run `!help` to see what I can do.",
                C_BLUE,
            ))
        except (discord.Forbidden, discord.HTTPException):
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
        if ctx.command is not None:
            from src.metrics import command_invocations
            command_invocations.labels(command=ctx.command.qualified_name, outcome="error").inc()
        await _log_admin_command(self.bot, ctx, error=error)
        raise error

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        await _log_admin_command(self.bot, ctx)
        state.stats_commands_ran += 1
        state.stats_commands_today += 1
        cog = ctx.command.cog
        bucket = type(cog).__name__ if cog else "Uncategorized"
        state.stats_commands_today_by_cog[bucket] = (
            state.stats_commands_today_by_cog.get(bucket, 0) + 1
        )
        from src.metrics import command_invocations
        command_invocations.labels(command=ctx.command.qualified_name, outcome="ok").inc()
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

        # Block until init_db_state has loaded state from the DB. Without
        # this, a fast command after restart hits _ensure_user against an
        # empty state.economy["users"], which overwrites the user's real
        # row with {balance: 0, daily_date: None}.
        import src.persistence as _pkg
        await _pkg.init_done.wait()

        state.stats_messages_seen += 1
        state.stats_messages_today += 1

        # Side-effect handlers — each runs unconditionally on every non-command
        # message, mutating its own state slice. Order matters for things like
        # the tax/curse/mock/ragebait quartet: they're independent but run in
        # the canonical order to keep ordering stable.
        await self._handle_msg_xp(message)
        await self._handle_ragebait(message)
        await self._handle_mock(message)
        await self._handle_tax(message)
        await self._handle_curse(message)
        await self._handle_auto_daily(message)

        # Interceptors — return True if they consumed the message and the rest
        # of the chain (including AI routing) should be skipped.
        if await self._handle_blackjack_input(message):
            return
        if await self._handle_puzzle_answer(message):
            return
        if await self._handle_hangman_guess(message):
            return

        # Final stage: AI routing. Always ends with self.bot.process_commands.
        await self._handle_ai_routing(message)

    # ── Side-effect handlers ──────────────────────────────────────────────────

    async def _handle_msg_xp(self, message: discord.Message):
        """Award XP for non-command messages; announce level-ups."""
        if not (message.guild and not message.content.startswith("!")):
            return
        uid = message.author.id
        _, leveled_up = await _grant_xp(uid, "msg", guild_id=message.guild.id)
        if leveled_up and isinstance(message.author, discord.Member) and get_guild_cfg(message.guild.id).get("levelup_channel"):
            cog = self.bot.cogs.get("LevelingCog")
            if cog:
                asyncio.create_task(cog._announce_levelup(message.author, message.guild.id))

    async def _handle_ragebait(self, message: discord.Message):
        """Fire passive AI ragebait if the author is targeted in this channel."""
        uid = message.author.id
        rage_channel = state.active_ragebaits.get(uid, {}).get("channel_id")
        if not (uid in state.active_ragebaits
                and not message.content.startswith("!")
                and (rage_channel is None or message.channel.id == rage_channel)):
            return
        if not await check_ollama_connected():
            return
        rage = state.active_ragebaits[uid]
        rage["history"].append(f"[{message.author.display_name}]: {message.content[:200]}")
        rage["remaining"] -= 1
        if rage["remaining"] <= 0:
            del state.active_ragebaits[uid]
        await save_ragebait()
        asyncio.create_task(_passive_ragebait(message, list(rage["history"])))

    async def _handle_mock(self, message: discord.Message):
        """Repeat the message in mocking font if the author is being mocked."""
        uid = message.author.id
        mock_channel = state.active_mocks.get(uid, {}).get("channel_id")
        if not (uid in state.active_mocks
                and not message.content.startswith("!")
                and (mock_channel is None or message.channel.id == mock_channel)):
            return
        mock = state.active_mocks[uid]
        await message.channel.send(mocking_font(message.content))
        mock["remaining"] -= 1
        if mock["remaining"] <= 0:
            del state.active_mocks[uid]
        await save_mock()

    async def _handle_tax(self, message: discord.Message):
        """Deduct per-message tax from users with an active tax on them."""
        uid = message.author.id
        tax_channel = state.active_taxes.get(uid, {}).get("channel_id")
        if not (uid in state.active_taxes
                and not message.content.startswith("!")
                and (tax_channel is None or message.channel.id == tax_channel)):
            return
        tax_data = state.active_taxes[uid]
        if "activated_at" in tax_data and time.time() - tax_data["activated_at"] > SHOP_TAX_DURATION_SECS:
            del state.active_taxes[uid]
            await save_tax(state.active_taxes)
            return
        if await is_insured(uid, "tax"):
            return
        tax_master_id = tax_data["master"]
        tax_label = tax_data.get("type", "tax").capitalize()
        tax_emoji = tax_data.get("emoji", "💰")
        if not await deduct_balance(uid, SHOP_TAX_PER_MESSAGE):
            return
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
        await message.channel.send(
            f"{tax_emoji} **{message.author.display_name}** paid a "
            f"**{SHOP_TAX_PER_MESSAGE:,} 🪙** {tax_label} tax to **{master_name}**"
        )

    async def _handle_curse(self, message: discord.Message):
        """Replay cursed users' messages in curse font."""
        uid = message.author.id
        if not (uid in state.active_curses and not message.content.startswith("!")):
            return
        curse = state.active_curses[uid]
        await message.channel.send(curse_font(message.content))
        curse["remaining"] -= 1
        if curse["remaining"] <= 0:
            del state.active_curses[uid]
        await save_curse(state.active_curses)

    async def _handle_auto_daily(self, message: discord.Message):
        """Auto-claim daily reward on the first qualifying interaction each day."""
        uid = message.author.id
        is_dm = isinstance(message.channel, discord.DMChannel)
        if (not is_dm
                and message.guild
                and message.channel.id in get_guild_cfg(message.guild.id).get("command_blacklist", [])):
            return
        triggers = (
            message.content.startswith("!")
            or self.bot.user in message.mentions
            or is_dm
            or message.channel.id in state.active_hangman_games
            or uid in state.active_blackjack_games
        )
        if triggers:
            await _auto_daily(message)

    # ── Interceptors (return True if message consumed) ────────────────────────

    async def _handle_blackjack_input(self, message: discord.Message) -> bool:
        """Intercept hit/stand from a player with an active blackjack game."""
        uid = message.author.id
        content_lower = message.content.strip().lower()
        if content_lower not in ("!hit", "!stand", "hit", "stand"):
            return False
        if uid not in state.active_blackjack_games:
            return False
        game = state.active_blackjack_games[uid]
        if game.get("channel_id") != message.channel.id:
            return False
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
                    "🃏 Blackjack", display + "\n\n`!hit` to draw or `!stand` to hold.", C_BLUE,
                ))
        else:
            await _blackjack_stand(message, uid, game)
        return True

    async def _handle_puzzle_answer(self, message: discord.Message) -> bool:
        """Intercept correct puzzle answers in a channel with an active puzzle."""
        uid = message.author.id
        cid = message.channel.id
        if cid not in state.active_puzzles or message.content.startswith("!"):
            return False
        puzzle = state.active_puzzles[cid]
        invited = puzzle.get("invited_ids")
        if invited and uid not in invited:
            return False
        if _norm_puzzle_answer(message.content.strip()) != _norm_puzzle_answer(puzzle["answer"]):
            return False
        reward = puzzle["reward"]
        expected = puzzle["answer"]
        del state.active_puzzles[cid]
        await add_balance(uid, reward)
        await message.channel.send(embed=emb(
            "✅ Correct!",
            f"{message.author.mention} got it!\n**Answer:** `{expected}`\n+**{reward:,} 🪙** (Balance: {await get_balance(uid):,} 🪙)",
            C_GREEN,
        ))
        return True

    async def _handle_hangman_guess(self, message: discord.Message) -> bool:
        """Intercept single-letter free-text hangman guesses."""
        uid = message.author.id
        cid = message.channel.id
        if cid not in state.active_hangman_games or message.content.startswith("!"):
            return False
        guess = message.content.lower().strip()
        if not (guess and guess.isalpha() and len(guess) == 1):
            return False
        asyncio.create_task(_delete_after(message))
        await _process_hangman_guess(message.channel, uid, cid, guess, message.author.display_name)
        return True

    # ── AI routing (final stage) ──────────────────────────────────────────────

    async def _handle_ai_routing(self, message: discord.Message):
        """Route mentions / DMs / AI-thread messages to the LLM, otherwise
        delegate to process_commands. Always ends with process_commands."""
        uid = message.author.id

        # Bot-wide AI off switch
        if not state.bot_settings.get("ai_enabled", True):
            await self.bot.process_commands(message)
            return

        # Channel allow-list (env-configured)
        if ACTIVE_CHANNEL_IDS and message.channel.id not in ACTIVE_CHANNEL_IDS:
            await self.bot.process_commands(message)
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = self.bot.user in message.mentions and message.content.strip().startswith(f"<@{self.bot.user.id}>")
        ai_thread = state.ai_threads.get(message.channel.id)
        in_ai_thread = ai_thread is not None

        # Ragebait/mock take precedence over normal mentions (in their channel)
        rage_channel = state.active_ragebaits.get(uid, {}).get("channel_id")
        mock_channel = state.active_mocks.get(uid, {}).get("channel_id")
        rage_in_channel = uid in state.active_ragebaits and (rage_channel is None or message.channel.id == rage_channel)
        mock_in_channel = uid in state.active_mocks and (mock_channel is None or message.channel.id == mock_channel)
        if rage_in_channel or mock_in_channel:
            await self.bot.process_commands(message)
            return

        # Per-guild AI channel restrictions for @mention
        if is_mentioned and not is_dm and message.guild:
            cfg = get_guild_cfg(message.guild.id)
            ai_channels = cfg.get("ai_channels", [])
            command_blacklist = cfg.get("command_blacklist", [])
            channel_allowed = True
            if ai_channels:
                channel_allowed = message.channel.id in ai_channels
            elif message.channel.id in command_blacklist:
                channel_allowed = False
            if not channel_allowed:
                if ai_channels:
                    names = " ".join(f"<#{cid}>" for cid in ai_channels)
                else:
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

        # AI thread: only invited participants get a response
        if ai_thread is not None and uid not in ai_thread["invited_ids"]:
            await self.bot.process_commands(message)
            return

        guild_id = message.guild.id if message.guild else None
        await respond(message.channel, uid, content, message, guild_id=guild_id, author_name=message.author.display_name)
        await self.bot.process_commands(message)




async def setup(bot):
    await bot.add_cog(EventsCog(bot))
