import asyncio
import json
import os
import random
import sys
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
    get_system_prompt, _log_audit, log_bot_permission_error, MemberConverter,
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
    check_chess_channel, _wrong_channel_reply, check_command_permission,
)
from src.persistence import (
    save_economy, save_insurance, save_guild_settings,
    save_bot_settings, save_godmode_users, save_bot_roles,
    save_chess_games, save_ragebait, save_mock, save_rigged_slots,
    save_gambler_streak, save_roleplay_state, save_fanfic_histories,
    save_quote_log, save_saved_quotes, save_tax, save_curse, save_lottery,
    load_lottery, load_saved_quotes, get_guild_cfg, save_command_perms,
    save_restart_msg,
)
from src.ai import (
    enforce_cost, insufficient_funds, check_ollama_connected, keep_typing,
    stream_ollama, finalize, _execute_ollama_stream, respond, respond_roleplay,
    ASK_SYSTEM_PROMPT, FANFIC_SYSTEM_PROMPT, FEATURE_COSTS, _norm_puzzle_answer,
)
from src.config import (
    OLLAMA_MODEL, OLLAMA_BASE_URL, SYSTEM_PROMPT, HISTORY_LIMIT,
    RATE_LIMIT_SECONDS, RACE_TRACK_LEN,
    ACTIVE_CHANNEL_IDS, DISCORD_TOKEN,
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
    DAILY_REWARD, DAILY_RESET_HOUR, INITIAL_BOT_ADMIN_IDS,
)
from src import state



class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="godmode")
    async def cmd_godmode(self, ctx: commands.Context, user: MemberConverter = None):
        if not await check_command_permission(ctx):
            return

        target_user = user if user else ctx.author
        if target_user.id in state.godmode_users:
            state.godmode_users.remove(target_user.id)
            status = "disabled"
        else:
            state.godmode_users.add(target_user.id)
            status = "enabled"

        await save_godmode_users()
        await ctx.send(embed=emb("👑 Godmode", f"Godmode **{status}** for {target_user.mention}.", C_GOLD))


    @commands.command(name="adminragebait")
    async def cmd_adminragebait(self, ctx: commands.Context, target: MemberConverter = None, n: str = None):
        if not await check_command_permission(ctx):
            return

        if target is None:
            await ctx.send(embed=emb("❌ Missing User", "Usage: `!adminragebait @user [n]` or `!adminragebait <userid> [n]`", C_RED))
            return

        # Parse optional message count (default 5)
        try:
            count = int(n) if n else 5
            if count <= 0:
                await ctx.send(embed=emb("❌ Invalid Count", "Please provide a positive number.", C_RED))
                return
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Count", f"Could not parse `{n}` as a number.", C_RED))
            return

        state.active_ragebaits[target.id] = {"remaining": count, "history": []}
        await save_ragebait()
        await ctx.send(embed=emb(
            "🎭 Ragebait Activated",
            f"Ragebait enabled for user `{target.id}` (next **{count}** message(s))",
            C_PURPLE,
        ))


    @commands.command(name="model")
    async def cmd_model(self, ctx: commands.Context, model_name: str = None):
        if not await check_command_permission(ctx):
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if model_name is None:
            current = cfg.get("ask_model", OLLAMA_MODEL)
            await ctx.send(embed=emb("⚙️ Model", f"Current model: `{current}`", C_GREY))
            return
        cfg["ask_model"] = model_name
        await save_guild_settings()
        await ctx.send(embed=emb("⚙️ Model", f"Switched to `{model_name}`", C_GREY))


    @commands.command(name="roleplaymodel")
    async def cmd_roleplaymodel(self, ctx: commands.Context, model_name: str = None):
        if not await check_command_permission(ctx):
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if model_name is None:
            current = cfg.get("roleplay_model", OLLAMA_MODEL)
            await ctx.send(embed=emb("⚙️ Roleplay Model", f"Current roleplay model: `{current}`", C_GREY))
            return
        cfg["roleplay_model"] = model_name
        await save_guild_settings()
        await ctx.send(embed=emb("⚙️ Roleplay Model", f"Switched to `{model_name}`", C_GREY))


    @commands.command(name="codingmodel")
    async def cmd_codingmodel(self, ctx: commands.Context, model_name: str = None):
        if not await check_command_permission(ctx):
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Error", "This command only works in servers.", C_RED))
            return
        cfg = get_guild_cfg(ctx.guild.id)
        if model_name is None:
            current = cfg.get("coding_model", OLLAMA_MODEL)
            await ctx.send(embed=emb("⚙️ Coding Model", f"Current coding puzzle model: `{current}`", C_GREY))
            return
        cfg["coding_model"] = model_name
        await save_guild_settings()
        await ctx.send(embed=emb("⚙️ Coding Model", f"Switched to `{model_name}`", C_GREY))


    @commands.command(name="vramtext")
    async def cmd_vramtext(self, ctx: commands.Context, *, text: str = None):
        if not await check_command_permission(ctx):
            return
        if text is None:
            await ctx.send(embed=emb("⚙️ vRAM Text", state.bot_settings.get("vram_text", "16GB"), C_GREY))
            return
        state.bot_settings["vram_text"] = text
        await save_bot_settings()
        await ctx.send(embed=emb("⚙️ vRAM Text", f"Set to: {text}", C_GREY))


    @commands.command(name="setprompt")
    async def cmd_setprompt(self, ctx: commands.Context, *, prompt: str):
        if not await check_command_permission(ctx):
            return
        channel_prompts[ctx.channel.id] = prompt
        await save_channel_prompts(channel_prompts)
        await ctx.send(embed=emb("⚙️ Prompt Updated", "System prompt updated for this channel.", C_GREY))


    @commands.command(name="clearprompt")
    async def cmd_clearprompt(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        channel_prompts.pop(ctx.channel.id, None)
        await save_channel_prompts(channel_prompts)
        await ctx.send(embed=emb("⚙️ Prompt Cleared", "Using default system prompt.", C_GREY))


    @commands.command(name="reverse")
    async def cmd_reverse(self, ctx: commands.Context):
        uid = ctx.author.id
        if uid in state.active_roleplays and state.active_roleplays[uid].get("channel_id") == ctx.channel.id:
            history_key = state.active_roleplays[uid].get("history_owner", uid)
            history = state.roleplay_histories.get(history_key, [])
        else:
            history = state.channel_histories[ctx.channel.id]
        if not history:
            await ctx.reply(embed=emb("", "No AI response to reverse.", C_RED))
            return
        # Pop assistant message if present
        if history[-1]["role"] == "assistant":
            history.pop()
        # Pop the paired user message
        if history and history[-1]["role"] == "user":
            history.pop()
        else:
            await ctx.reply(embed=emb("", "No AI response to reverse.", C_RED))
            return
        # Scan recent messages to find and delete the last bot response and the
        # user message that preceded it.
        recent = [m async for m in ctx.channel.history(limit=100)]
        bot_msg = None
        user_msg = None
        for i, msg in enumerate(recent):
            if msg.author == self.bot.user and msg.id != ctx.message.id:
                bot_msg = msg
                # Look further back for the invoker's message
                for msg2 in recent[i + 1:]:
                    if msg2.author.id == ctx.author.id:
                        user_msg = msg2
                        break
                break
        if bot_msg:
            try:
                await bot_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        if user_msg:
            try:
                await user_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        confirm = await ctx.reply(embed=emb("", "Last AI response removed from history.", C_GREEN))
        asyncio.create_task(_delete_after(confirm, delay=10.0))


    @commands.command(name="event")
    async def cmd_event(self, ctx: commands.Context, amount: str = None, duration: str = None):
        if not await check_command_permission(ctx):
            return
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            logging.warning(f"[event] No permission to delete command message in {ctx.channel}")
        except Exception as e:
            logging.warning(f"[event] Failed to delete command message: {e}")
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

        duration_str = ""
        if duration_hours:
            expires_at = int(time.time() + duration_hours * 3600)
            duration_str = f" (expires <t:{expires_at}:R>)"
        event_msg = await target_channel.send(embed=emb(
            "🎉 Coin Event!",
            f"React with 🪙 to receive **{amount:,} 🪙**!{duration_str}",
            C_GOLD,
        ))
        await event_msg.add_reaction("🪙")
        state.active_events[event_msg.id] = {"amount": amount, "rewarded": set()}

        if target_channel != ctx.channel:
            await ctx.send(embed=emb("✅ Event Started", f"Event posted in {target_channel.mention}.", C_GREEN))

        if duration_hours:
            async def _close_event():
                await asyncio.sleep(duration_hours * 3600)
                if event_msg.id in state.active_events:
                    del state.active_events[event_msg.id]
                    await event_msg.edit(embed=emb(
                        "🎉 Event Ended",
                        f"This event has ended. **{amount:,} 🪙** per reaction was given out.",
                        C_GREY,
                    ))
            asyncio.create_task(_close_event())


    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if reaction.message.id not in state.active_events:
            return
        if str(reaction.emoji) != "🪙":
            return
        event = state.active_events[reaction.message.id]
        if user.id in event["rewarded"]:
            return
        try:
            event["rewarded"].add(user.id)
            await add_balance(user.id, event["amount"])
        except Exception as e:
            logging.error(f"[event] Error rewarding {user.id}: {e}")
            event["rewarded"].discard(user.id)


    @commands.command(name="admingive", aliases=["adminpay"])
    async def cmd_give(self, ctx: commands.Context, target: MemberConverter = None, amount: str = None):
        if not await check_command_permission(ctx):
            return
        if target is None or amount is None:
            await ctx.send(embed=emb("⚙️ Give", "Usage: `!give @user <amount>`", C_GREY))
            return
        try:
            amount = int(amount)
            assert amount != 0
            if amount < 0:
                if self.bot.user and target.id == self.bot.user.id:
                    amount = max(amount, -1 * get_guild_house_balance(ctx.guild.id if ctx.guild else 0))
                else:
                    amount = max(amount, -1 * await get_balance(target.id))
        except (ValueError, AssertionError):
            await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a non-zero whole number.", C_RED))
            return
        if self.bot.user and target.id == self.bot.user.id and ctx.guild:
            await add_guild_house(ctx.guild.id, amount)
            action = "given to" if amount > 0 else "removed from"
            await ctx.send(embed=emb(
                "💸 Give",
                f"**{abs(amount):,} 🪙** {action} the **house pot** for this server. "
                f"House pot: {get_guild_house_balance(ctx.guild.id):,} 🪙",
                C_GOLD,
            ))
        else:
            await add_balance(target.id, amount)
            action = "given" if amount > 0 else "removed"
            await ctx.send(embed=emb(
                "💸 Give",
                f"**{abs(amount):,} 🪙** {action} {'to' if amount > 0 else 'from'} **{target.display_name}**. "
                f"New balance: {await get_balance(target.id):,} 🪙",
                C_GOLD,
            ))


    @commands.command(name="say")
    async def cmd_say(self, ctx: commands.Context, *, text: str = None):
        if not await check_command_permission(ctx):
            return
        if text is None:
            await ctx.send(embed=emb("🔊 Say", "Usage: `!say <text>`", C_GREY))
            return
        # Try to delete the command message (fail silently)
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass
        # Send the message
        await ctx.send(text)


    @commands.command(name="botinvitelink", aliases=["botinvite"])
    async def cmd_botinvite(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return

        invite_url = "https://discord.com/oauth2/authorize?client_id=1489403251303518322&permissions=6192724835560529&integration_type=0&scope=bot"

        # Create a view with a button
        _bot = self.bot
        class InviteView(ui.View):
            @ui.button(label="Get Bot Invitation Link", style=discord.ButtonStyle.primary)
            async def copy_button(self, interaction: discord.Interaction, button: ui.Button):
                # Verify the user clicking the button is an admin
                user_ctx = await _bot.get_context(interaction.message)
                user_ctx.author = interaction.user
                if not is_admin(user_ctx):
                    await interaction.response.send_message("❌ You don't have permission to view this link.", ephemeral=True)
                    return
                await interaction.response.send_message(f"```\n{invite_url}\n```", ephemeral=True)

        embed = discord.Embed(
            title="🤖 Bot Invite Link",
            description="Click the button below to get a copy of the bot invite URL",
            color=discord.Color(0x9932CC)
        )
        embed.add_field(name="Client ID", value="1489403251303518322", inline=False)
        embed.add_field(name="Permissions", value="6192724835560529", inline=False)

        await ctx.send(embed=embed, view=InviteView())


    @commands.command(name="invitelink")
    async def cmd_invite(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command only works in servers.", C_RED))
            return

        try:
            # Try to get vanity URL first (if server has one)
            if ctx.guild.vanity_url:
                invite_url = str(ctx.guild.vanity_url)
            else:
                # Create an invite link
                invite = await ctx.channel.create_invite(max_age=0, max_uses=0)
                invite_url = invite.url

            # Create a view with a button
            class ServerInviteView(ui.View):
                @ui.button(label="Get Server Invitation Link", style=discord.ButtonStyle.primary)
                async def copy_button(self, interaction: discord.Interaction, button: ui.Button):
                    await interaction.response.send_message(f"```\n{invite_url}\n```", ephemeral=True)

            embed = discord.Embed(
                title=f"📩 Invite to {ctx.guild.name}",
                description="Click the button below to get a copy of the server invite URL",
                color=discord.Color(0x9932CC)
            )
            if ctx.guild.icon:
                embed.set_thumbnail(url=ctx.guild.icon.url)

            await ctx.send(embed=embed, view=ServerInviteView())
        except discord.Forbidden:
            log_bot_permission_error(ctx, "create invites")
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to create invites in this channel.", C_RED))
        except Exception as e:
            await ctx.send(embed=emb("❌ Error", f"Failed to generate invite: {str(e)}", C_RED))


    @commands.command(name="restart")
    async def cmd_restart(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        msg = await ctx.send(embed=emb("🔄 Restarting", "Bot is restarting...", C_GOLD))
        await save_restart_msg(msg.channel.id, msg.id)
        await self.bot.close()
        os._exit(0)


    @commands.command(name="setperm")
    async def cmd_setperm(self, ctx: commands.Context, command_name: str = None, tier: str = None, hidden: str = "false"):
        if not await check_command_permission(ctx):
            return
        valid_tiers = ("everyone", "server_admin", "bot_admin")
        if command_name is None or tier is None:
            await ctx.send(embed=emb("⚙️ setperm", "Usage: `!setperm <command> <everyone|server_admin|bot_admin> [true|false]`", C_GOLD))
            return
        if tier not in valid_tiers:
            await ctx.send(embed=emb("❌ Invalid Tier", f"Tier must be one of: {', '.join(valid_tiers)}", C_RED))
            return
        hidden_bool = hidden.lower() in ("true", "1", "yes")
        state.command_perms[command_name] = {"tier": tier, "hidden": hidden_bool}
        await save_command_perms()
        await ctx.send(embed=emb(
            "✅ Permission Updated",
            f"`!{command_name}` → tier: **{tier}**, hidden: **{hidden_bool}**",
            C_GREEN,
        ))


    @commands.command(name="adminunlock")
    async def cmd_adminunlock(self, ctx: commands.Context, target: str = None):
        if not await check_command_permission(ctx):
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "This command can only be used in a server.", C_RED))
            return
        if not target:
            await ctx.send(embed=emb("⚙️ adminunlock", "Usage: `!adminunlock <#channel|@role|id>`", C_GOLD))
            return

        ch_match = re.match(r"<#(\d+)>", target)
        role_match = re.match(r"<@&(\d+)>", target)

        if ch_match:
            channel_id = int(ch_match.group(1))
            ch = ctx.guild.get_channel(channel_id)
            if ch is None:
                await ctx.send(embed=emb("❌ Not Found", "Could not find that channel.", C_RED))
                return
            if channel_id not in state.locked_channels:
                await ctx.send(embed=emb("❌ Not Locked", f"{ch.mention} is not locked.", C_RED))
                return
            del state.locked_channels[channel_id]
            cfg = get_guild_cfg(ctx.guild.id)
            cfg.get("locked_channels", {}).pop(str(channel_id), None)
            await save_guild_settings()
            await ctx.send(embed=emb("🔓 Channel Unlocked", f"{ch.mention} has been force-unlocked by an admin.", C_GREEN))
            return

        if role_match:
            role_id = int(role_match.group(1))
            r = ctx.guild.get_role(role_id)
            if r is None:
                await ctx.send(embed=emb("❌ Not Found", "Could not find that role.", C_RED))
                return
            if role_id not in state.locked_roles:
                await ctx.send(embed=emb("❌ Not Locked", f"**{r.name}** is not locked.", C_RED))
                return
            del state.locked_roles[role_id]
            cfg = get_guild_cfg(ctx.guild.id)
            cfg.get("locked_roles", {}).pop(str(role_id), None)
            await save_guild_settings()
            await ctx.send(embed=emb("🔓 Role Unlocked", f"**{r.name}** has been force-unlocked by an admin.", C_GREEN))
            return

        if target.isdigit():
            obj_id = int(target)
            if obj_id in state.locked_channels:
                ch = ctx.guild.get_channel(obj_id)
                del state.locked_channels[obj_id]
                cfg = get_guild_cfg(ctx.guild.id)
                cfg.get("locked_channels", {}).pop(str(obj_id), None)
                await save_guild_settings()
                label = ch.mention if ch else f"channel `{obj_id}`"
                await ctx.send(embed=emb("🔓 Channel Unlocked", f"{label} has been force-unlocked by an admin.", C_GREEN))
                return
            if obj_id in state.locked_roles:
                r = ctx.guild.get_role(obj_id)
                del state.locked_roles[obj_id]
                cfg = get_guild_cfg(ctx.guild.id)
                cfg.get("locked_roles", {}).pop(str(obj_id), None)
                await save_guild_settings()
                label = f"**{r.name}**" if r else f"role `{obj_id}`"
                await ctx.send(embed=emb("🔓 Role Unlocked", f"{label} has been force-unlocked by an admin.", C_GREEN))
                return
            await ctx.send(embed=emb("❌ Not Locked", f"No locked channel or role with ID `{obj_id}`.", C_RED))
            return

        await ctx.send(embed=emb("❌ Invalid Target", "Please supply a `#channel` mention, `@role` mention, or a numeric ID.", C_RED))


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
