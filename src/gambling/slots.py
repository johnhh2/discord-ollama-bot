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
    load_lottery, load_saved_quotes, get_guild_cfg, save_jackpot, load_jackpot,
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




class SlotsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="slots", aliases=["slot"])
    async def cmd_slots(self, ctx: commands.Context, amount: str = None):
        if await check_game_channel(ctx, "Gambling"):
            return
        uid = ctx.author.id
        _ensure_user(uid)

        # Track first-time usage
        user = state.economy["users"][str(uid)]
        first_time_slots = not user.get("slots_seen_rewards", False)
        if first_time_slots:
            user["slots_seen_rewards"] = True
            save_economy()

        if amount is None:
            embed = discord.Embed(title="🎰 Slots", color=C_GOLD)
            embed.description = f"**Usage:** `!slots <amount>` — Minimum bet: **{SLOT_MIN_BET} 🪙**"
            embed.add_field(name="Jackpot", value=(
                f"**7️⃣7️⃣7️⃣** (Jackpot) — {SLOT_MULT_JACKPOT}x + Progressive Jackpot\n"
                f"The Progressive Jackpot bonus scales to {SLOT_JACKPOT_BONUS_MAX_MULT:.0f}x at bet {SLOT_JACKPOT_BONUS_MAX_BET} 🪙 or above)*"
            ), inline=False)
            embed.add_field(name="Three of a Kind", value=(
                f"**🎰🎰🎰** (3 Slots) — {SLOT_MULT_3BAR}x\n"
                f"**🔔🔔🔔** (3 Bells) — {SLOT_MULT_3BELL}x\n"
                f"**🍋🍋🍋** (3 Lemons) — {SLOT_MULT_3LEMON}x\n"
                f"**🍒🍒🍒** (3 Cherries) — {SLOT_MULT_3CHERRY}x"
            ), inline=False)
            embed.add_field(name="Cherry Bonuses", value=(
                f"🍒 **Two Cherries** — {SLOT_MULT_2CHERRY}x\n"
                f"🍒 **One Cherry** — {SLOT_MULT_1CHERRY}x (Money Back)"
            ), inline=False)
            embed.add_field(name="Other", value=(
                "❌ **No Match** — 0x (Lose bet)\n\n"
                f"**Progressive Jackpot:** Grows by {SLOT_JACKPOT_CONTRIB:.0%} of every bet!\n"
                f"**Current Jackpot: {state.slot_jackpot:,} 🪙**"
            ), inline=False)
            await send_ephemeral(ctx, embed=embed)
            return

        amount = await parse_amount(ctx, amount, error_msg="")  # slots sends its own embed below
        if amount is None:
            await ctx.send(embed=emb("❌ Invalid Bet", "Please provide a positive amount.", C_RED))
            return

        if amount < SLOT_MIN_BET:
            await ctx.send(embed=emb("❌ Minimum Bet", f"Minimum bet is **{SLOT_MIN_BET} 🪙**.", C_RED))
            return

        if not await shop_charge(ctx, uid, amount):
            return

        # Jackpot contribution (2% of every bet, rounded up)
        contrib = max(1, int(amount * SLOT_JACKPOT_CONTRIB))
        state.slot_jackpot += contrib
        save_jackpot(state.slot_jackpot)

        # Spin (or use rigged result)
        if uid in state.rigged_slots:
            state.rigged_slots.discard(uid)
            save_rigged_slots()
            reels = ["7️⃣", "7️⃣", "7️⃣"]
        else:
            if random.random() < SLOT_HOUSE_CHANCE: # 5% back to house
                symbol_types = list(dict.fromkeys(SLOT_REEL))  # unique symbols, preserving order
                reels = random.sample(symbol_types, 3)
            else: # normal
                reels = [random.choice(SLOT_REEL) for _ in range(3)]
        display = " | ".join(reels)
        label, mult = eval_slots(reels, amount)

        # Progressive jackpot: hit 3 sevens
        if label == "jackpot":
            # Calculate bonus multiplier: 1x at SLOT_JACKPOT_BONUS_MIN_BET, scaling to SLOT_JACKPOT_BONUS_MAX_MULT at SLOT_JACKPOT_BONUS_MAX_BET+
            bet_bonus = min(
                SLOT_JACKPOT_BONUS_MAX_MULT,
                1.0 + max(0, amount - SLOT_JACKPOT_BONUS_MIN_BET)
                     / (SLOT_JACKPOT_BONUS_MAX_BET - SLOT_JACKPOT_BONUS_MIN_BET)
                     * (SLOT_JACKPOT_BONUS_MAX_MULT - 1.0)
            )
            prize = int(state.slot_jackpot * bet_bonus)
            state.slot_jackpot = SLOT_JACKPOT_SEED
            save_jackpot(state.slot_jackpot)
            add_balance(uid, prize)
            desc = (f"{display}\n\n🏆 **{ctx.author.display_name} hit the Progressive Jackpot!**\n"
                    f"**Won: {prize:,} 🪙** (Bet: {amount} 🪙 • Multiplier: {bet_bonus:.2f}x) | Balance: {get_balance(uid):,} 🪙\n"
                    f"*(Jackpot reset to {SLOT_JACKPOT_SEED:,} 🪙)*")
            if first_time_slots:
                desc += "\n\n📊 Use `!slotsrewards` to see all payouts!"
            msg = await ctx.send(embed=emb("🎰 PROGRESSIVE JACKPOT!", desc, C_GOLD))
            try:
                await msg.pin()
            except Exception:
                pass
            # Ping Gamblers role if enabled
            if ctx.guild:
                cfg = get_guild_cfg(ctx.guild.id)
                if cfg.get("gambler_role_enabled", False):
                    role = discord.utils.get(ctx.guild.roles, name="Gamblers")
                    if role:
                        await ctx.send(f"{role.mention} 🎰 A progressive jackpot was just won!")
            return

        # Money Back (cherry retention)
        if label == "1cherry":
            add_balance(uid, amount)
            desc = (f"{display}\n\n🍒 **One Cherry — Money Back!**\n"
                    f"**{ctx.author.display_name}** got **{amount} 🪙** back | Balance: {get_balance(uid):,} 🪙\n"
                    f"Progressive Jackpot: **{state.slot_jackpot:,} 🪙**")
            if first_time_slots:
                desc += "\n\n📊 Use `!slotsrewards` to see all payouts!"
            await ctx.send(embed=emb("🎰 Money Back!", desc, C_GOLD))
            return

        if mult == 0:
            desc = (f"{display}\n\n**{ctx.author.display_name}** lost **{amount} 🪙**. Balance: {get_balance(uid):,} 🪙\n"
                    f"Progressive Jackpot: **{state.slot_jackpot:,} 🪙**")
            if first_time_slots:
                desc += "\n\n📊 Use `!slotsrewards` to see all payouts!"
            await ctx.send(embed=emb("🎰 No Win", desc, C_RED))
            return

        winnings = amount * mult
        add_balance(uid, winnings)

        result_labels = {
            "jackpot": f"7️⃣7️⃣7️⃣ — **{mult}x** (min bet 25, bonus scales to 4x at bet 1000+)",
            "3bar":    f"🎰🎰🎰 — **{mult}x**",
            "3bell":   f"🔔🔔🔔 — **{mult}x**",
            "3lemon":  f"🍋🍋🍋 — **{mult}x**",
            "3cherry": f"🍒🍒🍒 — **{mult}x**",
            "2cherry": f"Two Cherries — **{mult}x**",
        }
        desc_line = result_labels.get(label, f"**{mult}x**")

        desc = (f"{display}\n\n{desc_line}\n"
                f"**{ctx.author.display_name}** won **{winnings} 🪙** | Balance: {get_balance(uid):,} 🪙\n"
                f"Progressive Jackpot: **{state.slot_jackpot:,} 🪙**")
        if first_time_slots:
            desc += "\n\n📊 Use `!slotsrewards` to see all payouts!"
        await ctx.send(embed=emb("🎰 Winner!", desc, C_GREEN))


    @commands.command(name="slotsrewards", aliases=["slotrewards", "slotreward"])
    async def cmd_slots_rewards(self, ctx: commands.Context):
        embed = discord.Embed(title="🎰 Slots Payouts", color=C_PURPLE)
        embed.description = "**Spin 3 reels and match symbols for payouts!**\n\n"

        embed.add_field(name="Three of a Kind", value=
            "🌟 **7️⃣7️⃣7️⃣** (Jackpot) — 75x\n"
            "   *(Min bet 25 🪙, bonus scales to 4x at bet 1000+)*\n"
            "🌟 **🎰🎰🎰** (3 Slots) — 15x\n"
            "🌟 **🔔🔔🔔** (3 Bells) — 7x\n"
            "🌟 **🍋🍋🍋** (3 Lemons) — 4x\n"
            "🌟 **🍒🍒🍒** (3 Cherries) — 3x",
            inline=False)

        embed.add_field(name="Cherry Bonuses", value=
            "🍒 **Two Cherries** — 2x\n"
            "🍒 **One Cherry** — 1x (Money Back)",
            inline=False)

        jackpot = load_jackpot()
        embed.add_field(name="Other", value=
            "❌ **No Match** — 0x (Lose bet)\n\n"
            f"**Progressive Jackpot:** Grows by 2% of every bet!\n"
            f"**Current Jackpot: {jackpot:,} 🪙**",
            inline=False)

        await send_ephemeral(ctx, embed=embed)


    @commands.command(name="rig", hidden=True)
    async def cmd_rig(self, ctx: commands.Context, target_input: str = None):
        """Hidden admin-only command: rig the next slots spin to hit 7 7 7."""
        if not is_admin(ctx):
            await ctx.send(embed=emb("❌ No Permission", "Only bot admins can use this command.", C_RED))
            return

        # Determine target user
        uid = None
        target_name = "you"

        if ctx.message.mentions:
            # Priority: use mention if present
            target = ctx.message.mentions[0]
            uid = target.id
            target_name = target.display_name
        elif target_input:
            # Try to parse as user ID
            try:
                uid = int(target_input)
                target_name = f"user {uid}"
            except ValueError:
                await ctx.send(embed=emb("❌ Invalid Input", f"Could not parse `{target_input}` as a user ID.", C_RED))
                return
        else:
            # Default to command author
            uid = ctx.author.id
            target_name = "you"

        state.rigged_slots.add(uid)
        save_rigged_slots()
        await ctx.send(embed=emb(
            "🎰 Slots Rigged",
            f"{target_name.capitalize()}'s next `!slots` spin will hit the **7️⃣7️⃣7️⃣ jackpot**!",
            C_GOLD,
        ))



async def setup(bot):
    await bot.add_cog(SlotsCog(bot))
