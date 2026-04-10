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
    save_quote_log, save_saved_quotes, save_simp, save_curse, save_lottery,
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
    SHOP_ROLECOLOR_COST, SHOP_CHANNEL_COST, SHOP_INSURANCE_COST, SHOP_SIMP_COST,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST,
    SHOP_INSURANCE_DURATION_SECS, SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES,
    SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_SIMP_TAX_PER_MESSAGE,
    SHOP_CONCUBINE_DURATION_SECS, SOUNDBOARD_WINDOW_SECS, SOUNDBOARD_MAX_SOUNDS,
    DAILY_REWARD, DAILY_RESET_HOUR, INITIAL_BOT_ADMIN_ID,
)
from src import state


# ─────────────────────────────────────────────────────────────────────────────
# Gambler role helpers
# ─────────────────────────────────────────────────────────────────────────────

async def get_or_create_gamblers_role(guild: discord.Guild) -> discord.Role | None:
    """Return the 'Gamblers' role, creating it if it doesn't exist."""
    role = discord.utils.get(guild.roles, name="Gamblers")
    if role is None:
        try:
            role = await guild.create_role(name="Gamblers", reason="Auto-created for gambler role tracking")
        except Exception:
            return None
    return role


async def maybe_assign_gambler_role(guild: discord.Guild, member: discord.Member, channel: discord.abc.Messageable):
    """Assign the Gamblers role if the user used all 3 scratchoffs 2 days in a row."""
    cfg = get_guild_cfg(guild.id)
    if not cfg.get("gambler_role_enabled", False):
        return

    uid_key = str(member.id)
    today_ct = _ct_today()
    yesterday = (datetime.date.fromisoformat(today_ct) - datetime.timedelta(days=1)).isoformat()

    last_full_day = state.gambler_streak.get(uid_key)
    if last_full_day == yesterday:
        role = await get_or_create_gamblers_role(guild)
        if role and role not in member.roles:
            if await toggle_member_role(member, role, True, reason="Used all 3 scratchoffs 2 days in a row"):
                await channel.send(
                    f"🎲 {member.mention} You've been automatically added to the **Gamblers** role for using all 3 scratchoffs 2 days in a row! "
                    f"You'll be pinged whenever a progressive jackpot is won. "
                    f"Use `!gambler-role off` to opt out."
                )


CACTPOT_PAYOUTS = {
    6: 10000, 7: 36, 8: 720, 9: 360, 10: 80, 11: 252, 12: 108, 13: 72, 14: 54, 15: 180,
    16: 72, 17: 180, 18: 119, 19: 36, 20: 306, 21: 1080, 22: 144, 23: 1800, 24: 3600
}

class MiniCactpotGame:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.grid = list(range(1, 10))
        random.shuffle(self.grid)
        self.revealed = set()
        # Reveal one random cell initially
        self.revealed.add(random.randint(0, 8))
        self.selections = []
        self.selected_line = None

    def get_grid_display(self):
        """Return a 3x3 grid display with numbers or letters A-I"""
        letters = ["A", "B", "C", "D", "E", "F", "G", "H", "I"]
        lines = []
        for row in range(3):
            row_str = ""
            for col in range(3):
                idx = row * 3 + col
                if idx in self.revealed:
                    row_str += str(self.grid[idx]).rjust(2) + " "
                else:
                    row_str += f" {letters[idx]} "
            lines.append(row_str)
        return "\n".join(lines)

    def get_line_sum(self, line_type: str, line_idx: int) -> int:
        """Get sum of a line. Types: row, col, diag1, diag2"""
        cells = []
        if line_type == "row":
            cells = [line_idx * 3, line_idx * 3 + 1, line_idx * 3 + 2]
        elif line_type == "col":
            cells = [line_idx, line_idx + 3, line_idx + 6]
        elif line_type == "diag1":
            cells = [0, 4, 8]
        elif line_type == "diag2":
            cells = [2, 4, 6]
        return sum(self.grid[i] for i in cells)

    def calculate_payout(self, line_type: str, line_idx: int) -> int:
        total = self.get_line_sum(line_type, line_idx)
        return CACTPOT_PAYOUTS.get(total, 0)



class ScratchoffCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="scratchoff", aliases=["scratch"])
    async def cmd_scratchoff(self, ctx: commands.Context, count: int = 1):
        if await check_game_channel(ctx, "Gambling"):
            return

        count = max(1, min(3, count))

        uid = ctx.author.id
        _ensure_user(uid)

        today = _ct_today()
        user = state.economy["users"][str(uid)]
        if user.get("scratch_date") != today:
            user["scratch_date"] = today
            user["scratch_used"] = 0

        remaining = 3 - user["scratch_used"]
        if remaining <= 0:
            save_economy()
            await ctx.send(embed=emb("🎰 Daily Limit", f"**{ctx.author.display_name}** has used all **3** daily scratchoffs.\nCome back tomorrow!", C_GOLD))
            return

        count = min(count, remaining)

        # Generate daily goal seeded by date (same for everyone)
        seed_val = hash(today) % (2**31)
        random.seed(seed_val)
        goal = random.choices(SCRATCH_SYMBOLS, k=4)
        random.seed()

        goal_str = " ".join(goal)

        show_hint = not user.get("scratchoff_seen_rewards", False)
        if show_hint:
            user["scratchoff_seen_rewards"] = True

        for _ in range(count):
            card = random.choices(SCRATCH_SYMBOLS, k=4)
            matches = sum(c == g for c, g in zip(card, goal))

            payout = 0
            match_text = ""
            if matches == 0:
                match_text = "❌ No matches."
            elif matches == 1:
                payout = 100
                match_text = f"⭐ 1 Match! **{ctx.author.display_name}** won 100 🪙!"
            elif matches == 2:
                payout = 1000
                match_text = f"🎉 2 Matches! **{ctx.author.display_name}** won 1,000 🪙!"
            elif matches == 3:
                payout = 10000
                match_text = f"🏆 3 Matches! **{ctx.author.display_name}** won 10,000 🪙!"
            elif matches == 4:
                payout = 100000
                match_text = f"💎 4 Matches! **{ctx.author.display_name}** won 100,000 🪙!"

            add_balance(uid, payout)
            user["scratch_used"] += 1
            save_economy()

            # Track full-day scratchoff streak for Gamblers role
            if user["scratch_used"] >= 3 and ctx.guild:
                state.gambler_streak[str(uid)] = today
                save_gambler_streak()
                await maybe_assign_gambler_role(ctx.guild, ctx.author, ctx.channel)

            card_str = " ".join(card)
            attempts_left = 3 - user["scratch_used"]

            embed = discord.Embed(title="🎫 Scratchoff", color=C_GREEN if payout > 0 else C_RED)
            embed.description = f"Daily Goal: {goal_str}\nYour Card:  {card_str}\n\n{match_text}\n\nAttempts left: {attempts_left}/3"

            if show_hint:
                embed.add_field(name="📊 Payout Info", value="Use `!scratchoffrewards` to see all payouts!", inline=False)
                show_hint = False

            await ctx.send(embed=embed)

    @commands.command(name="scratches")
    async def cmd_scratches(self, ctx: commands.Context):
        await ctx.invoke(self.cmd_scratchoff, count=3)

    @commands.command(name="streak")
    async def cmd_streak(self, ctx: commands.Context):
        uid = ctx.author.id
        _ensure_user(uid)

        today_ct = _ct_today()
        yesterday = (datetime.date.fromisoformat(today_ct) - datetime.timedelta(days=1)).isoformat()

        user = state.economy["users"][str(uid)]
        scratch_used = user.get("scratch_used", 0) if user.get("scratch_date") == today_ct else 0
        last_full_day = state.gambler_streak.get(str(uid))

        filled_today = last_full_day == today_ct
        filled_yesterday = last_full_day == yesterday

        if filled_today:
            streak_text = "🔥 **2+ days** — you filled today and at least yesterday!"
            color = C_GREEN
        elif filled_yesterday:
            streak_text = f"⏳ **1 day** — you filled yesterday. Use all 3 today to extend your streak! ({scratch_used}/3 used today)"
            color = C_GOLD
        elif last_full_day:
            streak_text = f"❌ **Streak broken** — last full day was `{last_full_day}`. Use all 3 today to start a new streak! ({scratch_used}/3 used today)"
            color = C_RED
        else:
            streak_text = f"❌ **No streak yet** — use all 3 scratchoffs in a day to start one! ({scratch_used}/3 used today)"
            color = C_GREY

        cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
        role_enabled = cfg.get("gambler_role_enabled", False)
        role_line = "\n\nFill all 3 **2 days in a row** to auto-join the **Gamblers** role." if role_enabled else ""

        await ctx.send(embed=emb("🎫 Scratchoff Streak", streak_text + role_line, color))

    @commands.command(name="scratchoffrewards", aliases=["scratchrewards", "scratchoffreward", "scratchreward"])
    async def cmd_scratchoff_rewards(self, ctx: commands.Context):
        embed = discord.Embed(title="🎫 Scratchoff Payouts", color=C_PURPLE)
        embed.description = "**Scratchoff** — Match symbols to your daily goal"

        table = "```\nMatches  Payout\n─────────────────\n"
        payouts = [
            ("0", "0 🪙"),
            ("1", "100 🪙"),
            ("2", "1,000 🪙"),
            ("3", "10,000 🪙"),
            ("4", "100,000 🪙"),
        ]

        for matches, payout in payouts:
            table += f"{matches}        {payout}\n"

        table += "─────────────────```"

        embed.add_field(name="Limit", value="**3 per day**", inline=False)
        await ctx.send(embed=embed)

    def eval_slots(reels: list[str], bet: int) -> tuple[str, int]:
        """Returns (result_label, multiplier). Caller applies multiplier to bet."""
        a, b, c = reels
        cherry = "🍒"

        # Priority: evaluate highest payout first
        if a == b == c:
            sym = a
            if sym == "7️⃣":
                # Jackpot requires minimum bet
                if bet < SLOT_MIN_BET:
                    return ("nothing", 0)
                return ("jackpot", SLOT_MULT_JACKPOT)
            if sym == "🎰":
                return ("3bar", SLOT_MULT_3BAR)
            if sym == "🔔":
                return ("3bell", SLOT_MULT_3BELL)
            if sym == "🍋":
                return ("3lemon", SLOT_MULT_3LEMON)
            if sym == cherry:
                return ("3cherry", SLOT_MULT_3CHERRY)

        # Cherry retention (only checked when no 3-of-a-kind)
        cherry_count = reels.count(cherry)
        if cherry_count >= 2:
            return ("2cherry", SLOT_MULT_2CHERRY)
        if cherry_count == 1:
            return ("1cherry", SLOT_MULT_1CHERRY)

        return ("nothing", 0)



async def setup(bot):
    await bot.add_cog(ScratchoffCog(bot))


@tasks.loop(minutes=1)
async def scratchoff_scheduler():
    """Reset daily scratchoff counts at 5am CT every day."""
    now_ct = _ct_now()
    if now_ct.hour != 5 or now_ct.minute != 0:
        return
    today = now_ct.date().isoformat()
    if state.economy.get("last_daily_reset") != today:
        do_daily_reset()


