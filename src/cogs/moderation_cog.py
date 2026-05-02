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
    check_chess_channel, _wrong_channel_reply, check_command_permission,
)
from src.persistence import (
    _load_json, save_economy, save_insurance, save_guild_settings,
    save_bot_settings, save_godmode_users, save_bot_roles,
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



class ModerationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="audit")
    async def cmd_audit(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        if not state.audit_log:
            await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "No failed attempts recorded.", C_GREY))
            return
        recent = list(state.audit_log)[-5:]
        lines = []
        for e in reversed(recent):
            ts = time.strftime("%H:%M:%S", time.localtime(e["time"]))
            lines.append(f"**{ts}** — {e['user']}\n`{e['command']}`\n_{e['error']}_")
        await send_ephemeral(ctx, embed=emb("🔍 Audit Log", "\n\n".join(lines), C_GOLD))


    @commands.command(name="clearbot")
    async def cmd_clear(self, ctx: commands.Context, n: int = 50):
        if not await check_command_permission(ctx):
            return
        deleted = 0
        async for message in ctx.channel.history(limit=500):
            if deleted >= n:
                break
            if message.author == self.bot.user:
                await message.delete()
                deleted += 1
        confirm = await ctx.send(embed=emb(
            "🗑️ Cleared",
            f"Deleted {deleted} bot message{'s' if deleted != 1 else ''}.",
            C_GREY,
        ))
        await asyncio.sleep(5)
        await confirm.delete()


    @commands.command(name="clearall")
    async def cmd_clearall(self, ctx: commands.Context, n: str = None):
        if not await check_command_permission(ctx):
            return

        if n is None:
            await ctx.send(embed=emb("❌ Missing Argument", "Usage: `!clearall <n>` — Delete last n messages", C_RED))
            return

        try:
            n = int(n) + 1
            if n <= 1:
                await ctx.send(embed=emb("❌ Invalid Number", "Please provide a positive integer.", C_RED))
                return
            if n > 101:
                await ctx.send(embed=emb("❌ Too Many", "Maximum 100 messages at a time.", C_RED))
                return
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Input", "Please provide a valid number.", C_RED))
            return

        try:
            messages = []
            async for message in ctx.channel.history(limit=n):
                messages.append(message)

            if not messages:
                await ctx.send(embed=emb("❌ No Messages", "No messages found to delete.", C_RED))
                return

            await ctx.channel.delete_messages(messages)
            confirm = await ctx.send(embed=emb(
                "🗑️ Cleared",
                f"Deleted {len(messages)-1} message{'s' if len(messages) != 1 else ''}.",
                C_GREY,
            ))
            await asyncio.sleep(5)
            await confirm.delete()
        except discord.Forbidden:
            await ctx.send(embed=emb("❌ No Permission", "I don't have permission to delete messages.", C_RED))
        except Exception as e:
            await ctx.send(embed=emb("❌ Error", f"Failed to delete messages: {str(e)}", C_RED))


    @commands.command(name="saved", aliases=["persistent", "saves"])
    async def cmd_saved(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return

        embed = discord.Embed(title="💾 Saved Data", color=C_GOLD)

        # Insurance data
        embed.add_field(
            name="🛡️ Insurance",
            value=f"**{len(state.insurance)}** users with active state.insurance",
            inline=False
        )

        # Mock data
        embed.add_field(
            name="🎭 Mock",
            value=f"**{len(state.active_mocks)}** users being mocked",
            inline=False
        )

        # Ragebait data
        embed.add_field(
            name="🎯 Ragebait",
            value=f"**{len(state.active_ragebaits)}** users with ragebait active",
            inline=False
        )

        # Tax data
        embed.add_field(
            name="🍆 Tax",
            value=f"**{len(state.active_taxes)}** users with active tax",
            inline=False
        )

        # Curse data
        embed.add_field(
            name="🔮 Curse",
            value=f"**{len(state.active_curses)}** users with curse active",
            inline=False
        )

        # Godmode users
        embed.add_field(
            name="👑 Godmode",
            value=f"**{len(state.godmode_users)}** users with godmode",
            inline=False
        )

        # Slot jackpot
        embed.add_field(
            name="💰 Slot Jackpot",
            value=f"**{state.slot_jackpot:,} 🪙** in jackpot",
            inline=False
        )

        # Chess games
        embed.add_field(
            name="♟️ Chess Games",
            value=f"**{len(state.active_chess_games)}** active correspondence chess games",
            inline=False
        )

        # Quote log
        _all_saved = await load_saved_quotes()
        _guild_id = str(ctx.guild.id) if ctx.guild else "dm"
        saved_quotes_count = len(_all_saved.get(_guild_id, []))
        embed.add_field(
            name="📜 Quotes",
            value=f"**{saved_quotes_count}** saved quotes (this server) | **{len(state.quote_log)}** in searchquote log (max 10)",
            inline=False
        )

        # Economy stats
        total_users = len(state.economy.get("users", {}))
        total_balance = sum(u.get("balance", 0) for u in state.economy.get("users", {}).values())
        embed.add_field(
            name="🪙 Economy",
            value=f"**{total_users}** users with **{total_balance:,} 🪙** total balance",
            inline=False
        )

        await send_ephemeral(ctx, embed=embed)



async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
