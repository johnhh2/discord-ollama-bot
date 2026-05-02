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
    load_lottery, load_saved_quotes, get_guild_cfg, try_set_record,
)
from src.ai import (
    enforce_cost, insufficient_funds, check_ollama_connected, keep_typing,
    stream_ollama, finalize, _execute_ollama_stream, respond, respond_roleplay,
    ASK_SYSTEM_PROMPT, FANFIC_SYSTEM_PROMPT, FEATURE_COSTS, _norm_puzzle_answer,
)
from src.config import (
    OLLAMA_MODEL, OLLAMA_BASE_URL, SYSTEM_PROMPT, HISTORY_LIMIT,
    RATE_LIMIT_SECONDS, RACE_TRACK_LEN,
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



class LotteryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lottery_scheduler.start()

    def cog_unload(self):
        self.lottery_scheduler.cancel()

    @tasks.loop(minutes=1)
    async def lottery_scheduler(self):
        """Check every minute if it's Saturday 6pm CST for lottery tasks."""
        ct = ZoneInfo("America/Chicago")
        now = datetime.datetime.now(datetime.timezone.utc).astimezone(ct)
        is_saturday = now.weekday() == 5

        if not is_saturday:
            return

        for guild in self.bot.guilds:
            cfg = get_guild_cfg(guild.id)
            lottery_channel_id = cfg.get("lottery_channel")
            if not lottery_channel_id:
                continue

            try:
                channel = await self.bot.fetch_channel(lottery_channel_id)
            except Exception:
                continue

            lottery = load_lottery(guild.id)
            current_week = now.isocalendar()[1]

            # 6pm: draw winner and reset lottery
            if now.hour >= 18 and lottery.get("last_drawn_week") != current_week:
                pool = lottery.get("prize_pool", 0)
                players = lottery.get("players", {})

                if players and pool > 0:
                    player_ids = list(players.keys())
                    weights = [players[pid] for pid in player_ids]
                    winner_id = random.choices(player_ids, weights=weights, k=1)[0]
                    winner = await self.bot.fetch_user(int(winner_id))
                    add_balance(int(winner_id), pool, guild_id=guild.id, holder_name=winner.display_name)
                    try_set_record(guild.id, "lottery", pool, int(winner_id), winner.display_name)

                    embed = discord.Embed(title="🎰 Lottery Results", color=C_GOLD)
                    embed.description = (
                        f"**Winner:** {winner.mention}\n"
                        f"**Prize:** {pool:,} 🪙\n"
                        f"**Players:** {len(players)}\n"
                        f"**Tickets Sold:** {sum(players.values())}"
                    )
                    await channel.send(embed=embed)

                lottery = {"prize_pool": 2000, "players": {}, "last_drawn_week": current_week, "last_posted_week": 0}
                drain_bot_balance_into_lottery(lottery, guild.id)
                save_lottery(guild.id, lottery)

            # 7pm: announce new lottery
            if now.hour >= 19 and lottery.get("last_posted_week") != current_week:
                lottery["last_posted_week"] = current_week
                save_lottery(guild.id, lottery)
                await announce_new_lottery(channel, lottery["prize_pool"], now)

    @commands.command(name="lottery")
    async def cmd_lottery(self, ctx: commands.Context, n: str = None):
        uid = ctx.author.id
        _ensure_user(uid)

        # Check if lottery channel is configured
        if ctx.guild is None:
            await ctx.send(embed=emb("🎰 Lottery", "Lottery only works in servers.", C_RED))
            return

        cfg = get_guild_cfg(ctx.guild.id)
        lottery_channel_id = cfg.get("lottery_channel")
        if not lottery_channel_id:
            await ctx.send(embed=emb("🎰 Lottery Disabled", "Lottery channel not configured.", C_GREY))
            return

        lottery = load_lottery(ctx.guild.id)

        # Check if we're in the 6-7pm window (draw done, new lottery not yet announced)
        ct = ZoneInfo("America/Chicago")
        now_cst = datetime.datetime.now(datetime.timezone.utc).astimezone(ct)
        current_week = now_cst.isocalendar()[1]
        in_transition = (
            now_cst.weekday() == 5
            and now_cst.hour >= 18
            and now_cst.hour < 19
            and lottery.get("last_posted_week") != current_week
        )

        if in_transition:
            # Next lottery starts at 7pm today
            next_lottery_start = now_cst.replace(hour=19, minute=0, second=0, microsecond=0)
            ts = int(next_lottery_start.timestamp())
            await ctx.send(embed=emb("🎰 Lottery", f"The next lottery is starting soon!\n\n**Opens:** <t:{ts}:R>", C_GREY))
            return

        if n is None:
            # Show lottery info
            pool = lottery.get("prize_pool", 0)
            players_dict = lottery.get("players", {})
            user_tickets = int(players_dict.get(str(uid), 0))

            # Calculate next Saturday 6pm CT (handles CST/CDT automatically)
            days_until_saturday = (5 - now_cst.weekday()) % 7
            next_saturday = now_cst + datetime.timedelta(days=days_until_saturday)
            next_saturday = next_saturday.replace(hour=18, minute=0, second=0, microsecond=0)
            if next_saturday <= now_cst:
                next_saturday += datetime.timedelta(weeks=1)
            timestamp = int(next_saturday.timestamp())

            info = f"**Prize Pool:** {pool:,} 🪙 (+1,000 🪙 per player)\n"
            info += f"**Players:** {len(players_dict)}\n"
            info += f"**Ticket Cost:** 10 🪙 for 1 🎟️\n\n"
            info += f"**Your Tickets:** {user_tickets}\n"
            info += f"Use `!lottery <n>` to buy more tickets"

            await ctx.send(embed=emb(f"🎰 Current Lottery • ends <t:{timestamp}:R>", info, C_PURPLE))
            return

        # Block purchases in the 1-hour window before the draw (5-6pm CT Saturday)
        if now_cst.weekday() == 5 and now_cst.hour == 17:
            await ctx.send(embed=emb("🔒 Lottery Locked", "Ticket sales are closed for the final hour before the draw. Check back after 6pm CT!", C_RED))
            return

        try:
            tickets = int(n)
            assert tickets > 0
        except (ValueError, AssertionError):
            await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a positive number.", C_RED))
            return

        cost = tickets * 10
        if not deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"Need {cost:,} 🪙. Balance: {get_balance(uid):,} 🪙", C_RED))
            return

        # Add to lottery
        players = lottery.setdefault("players", {})
        was_new_player = str(uid) not in players

        players[str(uid)] = players.get(str(uid), 0) + tickets
        lottery.setdefault("prize_pool", 0)
        lottery["prize_pool"] += tickets * 7
        if was_new_player:
            lottery["prize_pool"] += 1000

        save_lottery(ctx.guild.id, lottery)

        bonus_msg = "(+1,000 bonus as new player)" if was_new_player else ""

        # Calculate when lottery ends
        days_until_saturday = (5 - now_cst.weekday()) % 7
        next_saturday = now_cst + datetime.timedelta(days=days_until_saturday)
        next_saturday = next_saturday.replace(hour=18, minute=0, second=0, microsecond=0)
        if next_saturday <= now_cst:
            next_saturday += datetime.timedelta(weeks=1)
        timestamp = int(next_saturday.timestamp())

        embed_msg = emb(
            "🎰 Tickets Purchased",
            f"**{ctx.author.display_name}** bought **{tickets:,}** 🎟️ for **{cost:,} 🪙**\n\n"
            f"**Prize Pool:** {lottery['prize_pool']:,} 🪙 {bonus_msg}\n"
            f"**Your Tickets:** {players[str(uid)]}\n"
            f"**Total Players:** {len(players)}\n"
            f"**Ends:** <t:{timestamp}:R>",
            C_GREEN
        )
        await ctx.send(embed=embed_msg)



async def setup(bot):
    await bot.add_cog(LotteryCog(bot))

