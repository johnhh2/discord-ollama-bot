import asyncio
import json
import time
import logging

import aiohttp
import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_BLUE, mocking_font, curse_font, fetch_member, _delete_after,
    _effect_expired,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, is_insured, _ct_today, _ensure_user,
)
from src.permissions import (
    _wrong_channel_reply,
    gate_channel_ids,
    get_command_perm,
    is_silenced,
)
from src.persistence import (
    init_db_state, save_economy, save_ragebait, save_mock, save_tax, save_curse, save_spellcheck, load_restart_msg, clear_restart_msg, load_and_clear_ephemeral_msgs,
    insert_issue,
)
from src.guild_config import get_guild_cfg
from src.ai import (
    check_ollama_connected, keep_typing,
    stream_ollama, finalize, respond, ollama_complete,
    _norm_puzzle_answer,
)
from src.config import (
    ACTIVE_CHANNEL_IDS, SHOP_TAX_PER_MESSAGE,
    SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD,
)
from src import state
from src.games.blackjack import blackjack_hit, blackjack_stand, blackjack_double
from src.games.hangman import _process_hangman_guess
from src.leveling import grant_xp as _grant_xp
from src.streaks import bump_streak


async def _log_admin_command(bot, ctx: commands.Context):
    """Post a log embed to the global admin-log channel for successful runs of
    bot_admin / server_admin commands. No-op when the channel isn't configured.
    Errors are handled separately by `_log_command_error`.
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
    desc_lines = [
        f"**User:** {ctx.author.display_name} (`{ctx.author.id}`)",
        f"**Guild:** {guild_name} (`{guild_id_str}`)",
        f"**Channel:** {ctx.channel.mention if hasattr(ctx.channel, 'mention') else ctx.channel}",
        f"**Tier:** {perm.get('tier')}",
        f"**Command:** `{ctx.message.content[:300]}`",
    ]
    try:
        await channel.send(embed=emb("🛡️ Admin Command", "\n".join(desc_lines), C_BLUE))
    except (discord.Forbidden, discord.HTTPException):
        pass


_ERROR_REPORT_REACTIONS: tuple[str, ...] = ("❌", "⚙️", "✅", "🛑", "\U0001F507")  # ❌ ⚙️ ✅ 🛑 🔇


def _build_error_mute_key(ctx: commands.Context, error: Exception) -> str:
    """Compose the (command, type, message) mute key used by error_mutes.

    The full exception message is included so a small wording change re-files
    a fresh report — that's deliberate (per user preference at design time).
    """
    cmd_name = ctx.command.qualified_name if ctx.command is not None else "—"
    return f"{cmd_name}:{type(error).__name__}:{error}"[:255]


async def _log_command_error(bot, ctx: commands.Context, error: Exception):
    """File an auto-bug-report for a command exception.

    Replaces the older "post to error_log_channel" path: command errors now
    route to `internal_issue_channel` so the same admin reaction-triage flow
    (❌ 🚧 ✅) applies, plus a 🔇 mute reaction unique to error reports.

    No-op when:
    - `internal_issue_channel` isn't configured (nowhere to post)
    - the (command, type, message) key is already in `state.error_mutes`
      (an admin previously muted this exact error)
    """
    chan_id = state.bot_settings.get("internal_issue_channel")
    if not chan_id:
        return

    mute_key = _build_error_mute_key(ctx, error)
    if mute_key in state.error_mutes:
        return

    try:
        channel = bot.get_channel(int(chan_id)) or await bot.fetch_channel(int(chan_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, aiohttp.ClientError, OSError):
        return
    if channel is None:
        return

    guild_name = ctx.guild.name if ctx.guild else "DM"
    guild_id_str = str(ctx.guild.id) if ctx.guild else "—"
    tier = "—"
    cmd_name = "—"
    if ctx.command is not None:
        tier = get_command_perm(ctx.command.qualified_name).get("tier", "—")
        cmd_name = ctx.command.qualified_name
    chan_ref = ctx.channel.mention if hasattr(ctx.channel, "mention") else str(ctx.channel)
    err_line = f"**Error:** `{type(error).__name__}: {error}`"[:1000]
    src_guild_for_link = ctx.guild.id if ctx.guild else "@me"
    source_link = (
        f"[Jump to message]("
        f"https://discord.com/channels/{src_guild_for_link}/{ctx.channel.id}/{ctx.message.id})"
    )
    desc_lines = [
        f"**Time:** <t:{int(time.time())}:f>",
        f"**User:** {ctx.author.display_name} (`{ctx.author.id}`)",
        f"**Guild:** {guild_name} (`{guild_id_str}`)",
        f"**Channel:** {chan_ref}",
        f"**Tier:** {tier}",
        f"**Command:** `{ctx.message.content[:300]}`",
        f"**Source command:** {source_link}",
        err_line,
    ]

    history_lines = await _collect_recent_history(ctx)
    if history_lines:
        log_block = "\n".join(history_lines)
        if len(log_block) > 1500:
            log_block = log_block[-1500:]
        desc_lines.append(f"\n**Last 5 messages:**\n```\n{log_block}\n```")

    # Seeded status footer — matches utility_cog._issue_status_footer for
    # 'not_started'. Inlined to avoid an events-on-cog import.
    desc_lines.append("\n**Status:** Not started")

    try:
        report_msg = await channel.send(embed=emb("⚠️ Command Error", "\n".join(desc_lines), C_RED))
    except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError, OSError):
        return

    try:
        await insert_issue(
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=report_msg.channel.id,
            message_id=report_msg.id,
            reporter_id=ctx.author.id,
            report=f"{cmd_name}: {type(error).__name__}: {error}"[:1500],
            kind="error",
            mute_key=mute_key,
            source_channel_id=ctx.channel.id if hasattr(ctx.channel, "id") else None,
            source_message_id=ctx.message.id if getattr(ctx, "message", None) else None,
        )
    except Exception as e:
        logging.error(f"[error-report] failed to persist issue row: {e}", exc_info=True)

    for emoji in _ERROR_REPORT_REACTIONS:
        try:
            await report_msg.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError, OSError):
            pass


async def _collect_recent_history(ctx: commands.Context) -> list[str]:
    """Best-effort: return up to the last 5 messages in `ctx.channel` (oldest
    first), excluding the failing invocation itself. Empty list on any error.
    """
    history_lines: list[str] = []
    try:
        async for msg in ctx.channel.history(limit=6):
            if msg.id == ctx.message.id:
                continue
            if len(history_lines) >= 5:
                break
            text = _short_msg_text(msg)
            history_lines.append(f"[{msg.author.display_name}]: {text[:200]}")
        history_lines.reverse()
    except (discord.Forbidden, discord.HTTPException, AttributeError, aiohttp.ClientError, OSError):
        # OSError/aiohttp.ClientError cover transient DNS/connection failures
        # (e.g. ClientConnectorDNSError during a name-resolution blip) so a
        # network hiccup while building the report can't crash on_command_error.
        return []
    return history_lines


def _short_msg_text(msg) -> str:
    """Compact a message down to its content or first embed line for the
    'Last 5 messages' block. Mirrors what utility_cog._msg_text does, kept
    local to avoid a cog-on-events import cycle.
    """
    if getattr(msg, "content", None):
        return msg.content
    embeds = getattr(msg, "embeds", None) or []
    for e in embeds:
        if getattr(e, "title", None):
            return str(e.title)
        if getattr(e, "description", None):
            return str(e.description)
    return ""


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


async def _auto_daily(author, channel) -> tuple[int, int]:
    """Award daily coins (plus any pending property revenue) on first
    interaction of the day. Sends a short message if awarded.

    Takes (author, channel) rather than a Message so the dailies-channel
    reaction claim (src/cogs/dailies_cog.py) can reuse it.

    Returns (total_awarded, property_portion) — (0, 0) when the daily was
    already claimed today. The dailies reaction claim uses these to size its
    flip/slots gamble: the property portion only joins the stake for users
    who opted in with `!daily property`, and then rides as the gamble's
    record_exclude so property income can't inflate the flip/slots records.
    """
    from src.properties import bank_property_revenue
    uid = author.id
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
        return 0, 0
    is_new = user_data.get("last_daily", 0.0) == 0.0
    # Claim the day synchronously BEFORE the awaits (same fix as cmd_daily):
    # add_balance yields, so two qualifying messages in quick succession
    # would otherwise both pass the gate and double-award.
    user_data["daily_date"] = today
    if is_new:
        user_data["last_daily"] = time.time()
    # Premiums charged by the 5am sweep since the user's last claim — read +
    # reset inside the same synchronous claim window.
    ins_paid = int(user_data.get("ins_paid_since_claim", 0) or 0)
    ins_lapsed = int(user_data.get("ins_lapsed_since_claim", 0) or 0)
    user_data["ins_paid_since_claim"] = 0
    user_data["ins_lapsed_since_claim"] = 0
    try:
        await add_balance(uid, DAILY_REWARD)
    except Exception:
        user_data["daily_date"] = stored  # roll back the claim on failure
        user_data["ins_paid_since_claim"] = ins_paid
        user_data["ins_lapsed_since_claim"] = ins_lapsed
        raise
    # Property revenue rides the daily claim — inside the same claimed day,
    # so it pays at most once per gameplay-day. Banks and stamps atomically;
    # rolls itself back if the balance write fails.
    prop_rev = await bank_property_revenue(uid)
    await save_economy(uid=uid)
    greeting = f"Welcome, **{author.display_name}**! 🎉 Here are your first" if is_new else "Daily coins ready!"
    prop_str = f" + **{prop_rev:,} 🪙** property revenue" if prop_rev else ""
    ins_str = f"\n🛡️ Insurance paid since your last claim: **{ins_paid:,} 🪙**" if ins_paid else ""
    lapse_str = (
        f"\n⚠️ {ins_lapsed} insurance renewal{'s' if ins_lapsed != 1 else ''} couldn't be paid — "
        "coverage lapsed those days." if ins_lapsed else ""
    )
    prop_note = ""
    if prop_rev:
        prop_note = (
            "\n*Property revenue joins your dailies 🪙/🎰 stake — `!daily property` to leave it out.*"
            if user_data.get("daily_gamble_property", False)
            else "\n*Property revenue isn't part of the dailies 🪙/🎰 stake — `!daily property` to include it.*"
        )
    await channel.send(embed=emb(
        "🪙 Daily Reward",
        f"{greeting} **{DAILY_REWARD:,} 🪙**{prop_str} added. Balance: {await get_balance(uid):,} 🪙{ins_str}{lapse_str}{prop_note}",
        C_GREEN,
    ), silent=True)
    return DAILY_REWARD + prop_rev, prop_rev


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
    placeholder = await message.reply("...", silent=True)
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

        # Shield init from on_ready cancellation: if this listener task
        # is killed mid-await (gateway hiccup, container signal), the
        # inner `async with with_cursor()` raises GeneratorExit during
        # cleanup, init_done never gets set, and every subsequent
        # on_message hits its 60s timeout and silently drops — bot looks
        # unresponsive until manual restart. Shield wraps the coro in a
        # Task that keeps running even if the outer await is cancelled.
        await asyncio.shield(init_db_state())

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

        # Resume any chess games where it's the bot's turn at restart —
        # otherwise they'd freeze waiting for the human to make another
        # move that triggers _play_bot_reply.
        chess_cog = self.bot.get_cog("ChessCog")
        if chess_cog is not None:
            try:
                await chess_cog.resume_pending_bot_turns()
            except Exception as e:
                logging.error(f"chess: resume_pending_bot_turns failed: {e}", exc_info=True)

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
            ), silent=True)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def bot_check(self, ctx: commands.Context) -> bool:
        """Global command-channel whitelist/blacklist gate.

        `bot_check` is a discord.py Cog special method registered as a
        bot-wide check for every command. (This was previously declared as a
        @Cog.listener(), which never fires — listeners are dispatched by
        gateway event name — so the whitelist/blacklist was silently
        unenforced.) Returning False raises CheckFailure; on_command_error
        formats the wrong-channel reply.
        """
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

        # A thread inherits its parent channel's status (gate_channel_ids).
        channel_ids = gate_channel_ids(ctx.channel)

        # Check blacklist first (deny)
        command_blacklist = cfg.get("command_blacklist", [])
        if any(cid in command_blacklist for cid in channel_ids):
            return False

        # Check whitelist (allow only if specified)
        command_whitelist = cfg.get("command_whitelist", [])
        if command_whitelist and not any(cid in command_whitelist for cid in channel_ids):
            return False

        return True

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            logging.debug(f"[debug] {error}")
            return
        # A command-local `@cmd.error` handler that fully dealt with the error
        # (sent the user a friendly message) sets `error.handled = True`.
        # discord.py still dispatches this global listener afterwards, so honor
        # the flag and skip the audit log / "⚠️ Command Error" report for those.
        if getattr(error, "handled", False):
            return
        from src.level_unlocks import LevelLocked
        from src.permissions import PermissionDenied
        from src.gambling.session import GamblingThreadOnly
        if isinstance(error, (LevelLocked, PermissionDenied, GamblingThreadOnly)):
            return  # gate already sent its own message (or is hidden-silent)
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
        # Bad/missing/extra arguments to a typed parameter (e.g. `!scratch help`
        # failing to convert "help" to int). discord.py raises BadArgument /
        # MissingRequiredArgument / TooManyArguments — all subclasses of
        # UserInputError. Show the command's usage instead of routing this to the
        # admin "⚠️ Command Error" bug-report path and re-raising.
        if isinstance(error, commands.UserInputError):
            if ctx.command is not None:
                prefix = ctx.clean_prefix or "!"
                # Echo the command name as the user typed it (alias-aware):
                # showing the canonical name for an alias invocation reads as
                # "that command doesn't exist, use this one instead".
                parents = getattr(ctx, "invoked_parents", None) or []
                typed = " ".join([*parents, getattr(ctx, "invoked_with", None) or ""]).strip() \
                    or ctx.command.qualified_name
                usage = f"{prefix}{typed} {ctx.command.signature}".rstrip()
                await ctx.send(f"❌ Usage: `{usage}`")
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
        # Error reporting is best-effort: a transient network failure (DNS,
        # connection reset) while posting the bug report must not produce a
        # second, misleading on_command_error traceback that buries the real
        # error. Swallow anything here; the original `error` is re-raised below.
        try:
            await _log_command_error(self.bot, ctx, error)
        except Exception:
            logging.warning("[error-report] failed to file command-error report", exc_info=True)
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
        if not ctx.author.bot:
            # Any successful command (gambling included) extends the daily
            # streak; no-op after the day's first command or dailies click.
            await bump_streak(
                ctx.author.id, ctx.author.display_name,
                ctx.guild.id if ctx.guild else None, ctx.channel, _ct_today(),
            )
        if ctx.guild and not ctx.author.bot:
            xp, leveled_up = await _grant_xp(ctx.author.id, "cmd", guild_id=ctx.guild.id)
            # _announce_levelup grants the coin reward and itself skips the
            # announcement when no level-up channel is configured — gating the
            # call on the channel here silently withheld the reward.
            if leveled_up:
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
        #
        # Bounded wait: if init_db_state was cancelled mid-flight (e.g. a
        # gateway hiccup re-dispatched on_ready and aborted the in-flight
        # task), init_done stays unset forever and every message hangs here.
        # 60s is well past a healthy boot's init time; if we're past that,
        # something is wrong and silently dropping the message is better
        # than blocking the listener task indefinitely. _ensure_user and
        # grant_xp keep their unbounded waits — there the cost of proceeding
        # on empty state is data corruption, not a dropped command.
        import src.persistence as _pkg
        try:
            await asyncio.wait_for(_pkg.init_done.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            logging.error("[on_message] init_done not set after 60s; dropping message")
            return

        # Bot-side blocklist: silently drop everything from banned users —
        # no AI, no commands, no XP/economy/tax/curse side effects, no
        # stats counted. Mirrors the hidden-permission denial pattern.
        #
        # is_silenced (not an inline dict lookup) so DMs are covered: a DM has
        # no guild to scope the per-guild blocklist to, and the economy has no
        # guild dimension, so a banned user could otherwise farm coins in a DM
        # and spend them in the server that banned them.
        if is_silenced(message.author.id, message.guild.id if message.guild else None):
            return

        state.stats_messages_seen += 1
        state.stats_messages_today += 1

        # Side-effect handlers — each runs on every non-command message from a
        # human, mutating its own state slice. Order matters for things like
        # the tax/curse/mock/ragebait quartet: they're independent but run in
        # the canonical order to keep ordering stable. Each is isolated: one
        # handler blowing up (DB hiccup, Discord 500) must not silently abort
        # the rest of the chain — including process_commands at the end.
        #
        # Bots are excluded from the whole chain, not just from XP. This bot's
        # own messages already returned above, but process_commands is
        # deliberately open to *other* bots, so their messages reach here: a
        # mocked bot and this bot would echo each other indefinitely, and
        # `!shop tax @somebot` billed an account that can't notice or object.
        # The interceptors and AI routing below stay open to bots as before.
        for handler in () if message.author.bot else (
            self._handle_msg_xp,
            self._handle_ragebait,
            self._handle_mock,
            self._handle_tax,
            self._handle_curse,
            self._handle_spellcheck,
            self._handle_auto_daily,
        ):
            try:
                await handler(message)
            except Exception:
                logging.exception("[on_message] %s failed", handler.__name__)

        # Interceptors — return True if they consumed the message and the rest
        # of the chain (including AI routing) should be skipped. An interceptor
        # exception counts as "not consumed" so the command chain still runs.
        for interceptor in (
            self._handle_blackjack_input,
            self._handle_puzzle_answer,
            self._handle_hangman_guess,
        ):
            try:
                if await interceptor(message):
                    return
            except Exception:
                logging.exception("[on_message] %s failed", interceptor.__name__)

        # Final stage: AI routing. Always ends with self.bot.process_commands.
        await self._handle_ai_routing(message)

    # ── Side-effect handlers ──────────────────────────────────────────────────

    async def _handle_msg_xp(self, message: discord.Message):
        """Award XP for non-command messages; announce level-ups."""
        if message.author.bot:
            # Consistent with cmd XP (on_command_completion) and voice XP,
            # which both exclude bots — a companion bot shouldn't level up.
            return
        if not (message.guild and not message.content.startswith("!")):
            return
        uid = message.author.id
        _, leveled_up = await _grant_xp(uid, "msg", guild_id=message.guild.id)
        # See on_command_completion: the call must not be gated on the channel.
        if leveled_up and isinstance(message.author, discord.Member):
            cog = self.bot.cogs.get("LevelingCog")
            if cog:
                asyncio.create_task(cog._announce_levelup(message.author, message.guild.id))

    async def _handle_ragebait(self, message: discord.Message):
        """Fire passive AI ragebait if the author is targeted in this server."""
        if message.guild is None:
            return
        uid = message.author.id
        key = (message.guild.id, uid)
        if not (key in state.active_ragebaits and not message.content.startswith("!")):
            return
        if await is_insured(uid,"ragebait"):
            return
        if not await check_ollama_connected():
            return
        # Re-fetch after the awaits above: a concurrent message may have
        # consumed the last charge and deleted the key while we yielded.
        rage = state.active_ragebaits.get(key)
        if rage is None:
            return
        rage["history"].append(f"[{message.author.display_name}]: {message.content[:200]}")
        rage["remaining"] -= 1
        if rage["remaining"] <= 0:
            del state.active_ragebaits[key]
        await save_ragebait()
        asyncio.create_task(_passive_ragebait(message, list(rage["history"])))

    async def _handle_mock(self, message: discord.Message):
        """Repeat the message in mocking font if the author is being mocked."""
        if message.guild is None:
            return
        uid = message.author.id
        key = (message.guild.id, uid)
        if not (key in state.active_mocks and not message.content.startswith("!")):
            return
        if await is_insured(uid,"mock"):
            return
        # Consume the charge synchronously BEFORE the send: the send yields,
        # so two concurrent messages on the last charge would otherwise both
        # decrement and the second del would KeyError (killing the chain).
        mock = state.active_mocks.get(key)
        if mock is None:
            return
        mock["remaining"] -= 1
        if mock["remaining"] <= 0:
            del state.active_mocks[key]
        await save_mock()
        await message.channel.send(mocking_font(message.content))

    async def _handle_tax(self, message: discord.Message):
        """Deduct per-message tax from users with an active tax on them."""
        if message.guild is None:
            return
        uid = message.author.id
        key = (message.guild.id, uid)
        if not (key in state.active_taxes and not message.content.startswith("!")):
            return
        tax_data = state.active_taxes[key]
        if _effect_expired(tax_data):
            del state.active_taxes[key]
            await save_tax(state.active_taxes)
            return
        if await is_insured(uid,"tax"):
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
            f"**{SHOP_TAX_PER_MESSAGE:,} 🪙** {tax_label} tax to **{master_name}**",
            silent=True,
        )

    async def _handle_curse(self, message: discord.Message):
        """Replay cursed users' messages in curse font."""
        if message.guild is None:
            return
        uid = message.author.id
        key = (message.guild.id, uid)
        if not (key in state.active_curses and not message.content.startswith("!")):
            return
        # Consume the charge synchronously before the send (see _handle_mock).
        curse = state.active_curses[key]
        curse["remaining"] -= 1
        if curse["remaining"] <= 0:
            del state.active_curses[key]
        await save_curse(state.active_curses)
        await message.channel.send(curse_font(message.content))

    async def _handle_spellcheck(self, message: discord.Message):
        """Reply with an AI-corrected version of a spellchecked user's message.

        Fires on every non-command message from a user with an active
        spellcheck, except in blacklisted channels (or, if a whitelist is set,
        channels not in it). Posts ``<corrected sentence> *`` only when the AI
        finds spelling/grammar errors; clean messages are left alone.
        """
        if message.guild is None:
            return
        uid = message.author.id
        key = (message.guild.id, uid)
        sc = state.active_spellchecks.get(key)
        if not (sc and not message.content.startswith("!")):
            return

        # Expire by explicit expires_at (set on purchase / by admin grant).
        if _effect_expired(sc):
            del state.active_spellchecks[key]
            await save_spellcheck()
            return

        if await is_insured(uid,"spellcheck"):
            return

        # Channel gating: blacklist denies, a whitelist (if set) allows only
        # its members. Mirrors the global command-channel check.
        cfg = get_guild_cfg(message.guild.id)
        if message.channel.id in cfg.get("command_blacklist", []):
            return
        whitelist = cfg.get("command_whitelist", [])
        if whitelist and message.channel.id not in whitelist:
            return

        content = message.content.strip()
        if not content:
            return

        system_prompt = (
            "You are a conservative spelling and grammar checker for casual chat "
            "messages. You will be given one message. Only fix mistakes that are "
            "UNAMBIGUOUS errors no matter the context — a clearly misspelled common "
            "word (e.g. 'teh' -> 'the', 'recieve' -> 'receive') or a clear grammar "
            "mistake (e.g. 'i has' -> 'i have', 'should of' -> 'should have').\n\n"
            "Do NOT change anything that could be intentional or that you simply "
            "don't recognize. Leave these EXACTLY as written: proper nouns and the "
            "names of people, places, games, shows, songs, brands, characters, and "
            "usernames; slang, abbreviations, internet shorthand (lol, idk, tbh, "
            "gonna, imma); deliberate stylization, emoji, emoticons, and casing; "
            "and any unfamiliar word that might be a real name or term. When you are "
            "not sure whether something is an error, treat it as correct.\n\n"
            "If — and only if — the message contains at least one unambiguous error, "
            "reply with ONLY the corrected message: no quotes, no explanation, no "
            "preamble, and change nothing except the actual errors. Otherwise reply "
            "with exactly the single word: CORRECT."
        )
        async def _run_spellcheck():
            corrected = (await ollama_complete([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ])).strip()
            # Treat any "CORRECT"-ish no-op signal as "leave it alone" (the model
            # sometimes adds punctuation, e.g. "CORRECT.").
            if not corrected or corrected.strip(" .!").upper() == "CORRECT":
                return
            # The model occasionally echoes the input instead of "CORRECT" — skip if
            # nothing meaningful changed (ignore surrounding whitespace).
            if corrected == content or corrected.strip() == content.strip():
                return
            try:
                await message.channel.send(f"{corrected} *")
            except discord.HTTPException:
                pass

        # Fire-and-forget: awaiting the full Ollama round-trip here would
        # delay the blackjack/puzzle/hangman interceptors and
        # process_commands for every message from a spellchecked user.
        asyncio.create_task(_run_spellcheck())

    async def _handle_auto_daily(self, message: discord.Message):
        """Auto-claim daily reward on the first qualifying interaction each day."""
        if message.author.bot:
            # No free daily for companion bots (consistent with the XP
            # exclusions); a bot user can still claim explicitly via !daily.
            return
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
            await _auto_daily(message.author, message.channel)

    # ── Interceptors (return True if message consumed) ────────────────────────

    async def _handle_blackjack_input(self, message: discord.Message) -> bool:
        """Intercept hit/stand/double from a player with an active blackjack game."""
        uid = message.author.id
        content_lower = message.content.strip().lower()
        if content_lower not in ("!hit", "!stand", "!double", "hit", "stand", "double"):
            return False
        game = state.active_blackjack_games.get(uid)
        if game is None or game.get("channel_id") != message.channel.id:
            return False
        if game.get("pending"):
            return False  # claimed but bet not charged yet — not playable
        # The same actions the Hit/Stand buttons run; each retires the
        # buttons under the previous turn so the typed word doesn't leave a
        # live set behind.
        if content_lower in ("!hit", "hit"):
            await blackjack_hit(message.author, message.channel, message.guild)
        elif content_lower in ("!double", "double"):
            await blackjack_double(message.author, message.channel, message.guild)
        else:
            await blackjack_stand(message.author, message.channel, message.guild)
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
        ), silent=True)
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
        # Accept both mention forms: <@id> and the legacy nickname form <@!id>
        # (some clients/bots still send the latter).
        _mention_forms = (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>")
        is_mentioned = self.bot.user in message.mentions and message.content.strip().startswith(_mention_forms)
        ai_thread = state.ai_threads.get(message.channel.id)
        in_ai_thread = ai_thread is not None

        # Ragebait/mock take precedence over normal mentions (in their server)
        if message.guild is not None:
            ekey = (message.guild.id, uid)
            if ekey in state.active_ragebaits or ekey in state.active_mocks:
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

        content = (
            message.content
            .replace(f"<@!{self.bot.user.id}>", "")
            .replace(f"<@{self.bot.user.id}>", "")
            .strip()
        )
        if not content:
            await message.reply("Yes?")
            await self.bot.process_commands(message)
            return

        # "@Bot !give @user 1" is a command, not an AI prompt. The dispatcher
        # only sees the "!" prefix at the very start of the raw content, so a
        # mention-prefixed command never reached it — the LLM answered in
        # prose instead (typically coaching the user toward the canonical
        # command name). Same for a "!command" DM, where the LLM used to
        # answer on top of the command. Strip the mention and dispatch;
        # an unresolvable "!word" still falls through to the AI.
        if content.startswith("!"):
            message.content = content
            ctx = await self.bot.get_context(message)
            if ctx.command is not None:
                await self.bot.invoke(ctx)
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
