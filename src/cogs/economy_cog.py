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
    get_system_prompt, _log_audit, log_bot_permission_error, MemberConverter,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, get_guild_house_balance,
    add_guild_house, drain_bot_balance_into_lottery, announce_new_lottery,
    is_insured, get_insurance_expiry, get_guild_ask_model, get_guild_roleplay_model,
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
    load_lottery, load_saved_quotes, get_guild_cfg, load_records,
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



class EconomyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="daily")
    async def cmd_daily(self, ctx: commands.Context):
        uid = ctx.author.id
        _ensure_user(uid)
        today = _ct_today()
        user_data = state.economy["users"][str(uid)]
        if user_data.get("daily_date") == today:
            now_ct = _ct_now()
            next_reset = datetime.datetime.combine(
                now_ct.date() if now_ct.hour < DAILY_RESET_HOUR else now_ct.date() + datetime.timedelta(days=1),
                datetime.time(DAILY_RESET_HOUR, 0),
                tzinfo=ZoneInfo("America/Chicago"),
            )
            remaining = int((next_reset - now_ct).total_seconds())
            hours, rem = divmod(remaining, 3600)
            minutes = rem // 60
            await ctx.send(embed=emb("⏳ Already Claimed", f"**{ctx.author.display_name}** already claimed today. Resets at **{DAILY_RESET_HOUR}am** — come back in **{hours}h {minutes}m**.", C_GOLD))
            return
        add_balance(uid, DAILY_REWARD)
        user_data["daily_date"] = today
        user_data["last_daily"] = time.time()
        save_economy()
        await ctx.send(embed=emb("🪙 Daily Reward", f"**{ctx.author.display_name}** claimed **+{DAILY_REWARD} 🪙**! Balance: **{get_balance(uid)} 🪙**", C_GREEN))


    @commands.command(name="balance", aliases=["bal", "b", "!", "$"])
    async def cmd_balance(self, ctx: commands.Context, target: MemberConverter = None):
        target = target or ctx.author
        if self.bot.user and target.id == self.bot.user.id and ctx.guild:
            bal = get_guild_house_balance(ctx.guild.id)
            await ctx.send(embed=emb("🏦 House Pot", f"**{ctx.guild.name}**: {bal} 🪙", C_GOLD))
        else:
            bal = get_balance(target.id)
            await ctx.send(embed=emb("💰 Balance", f"**{target.display_name}**: {bal} 🪙", C_GREEN))


    @commands.command(name="leaderboard", aliases=["leaderboards", "lb"])
    async def cmd_leaderboard(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Leaderboard is only available in servers.")
            return
        lottery = load_lottery(ctx.guild.id)
        lottery_players = lottery.get("players", {})
        sorted_users = sorted(
            ((k, v) for k, v in state.economy["users"].items() if v["balance"] > 0 or k in lottery_players),
            key=lambda x: x[1]["balance"], reverse=True
        )[:10]
        if not sorted_users:
            await ctx.send(embed=emb("🪙 Leaderboard", "No users yet.", C_GREEN))
            return
        medals = ["🥇", "🥈", "🥉"]

        async def resolve_name(uid_str: str) -> str:
            uid_int = int(uid_str)
            member = await fetch_member(ctx.guild, uid_int)
            if member:
                return member.display_name
            try:
                user = await self.bot.fetch_user(uid_int)
                return user.display_name
            except (discord.NotFound, discord.HTTPException):
                return f"User {uid_str}"

        names = await asyncio.gather(*(resolve_name(uid_str) for uid_str, _ in sorted_users))
        lines = []
        for i, (name, (uid_str, data)) in enumerate(zip(names, sorted_users)):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            tickets = lottery_players.get(uid_str, 0)
            ticket_str = f" • {tickets} 🎟️" if tickets else ""
            lines.append(f"{prefix} **{name}** — {data['balance']} 🪙{ticket_str}")
        lines.append("\n*Also: `!levels` XP · `!lbr` roles*")
        await ctx.send(embed=emb("🪙 Leaderboard", "\n".join(lines), C_GREEN))


    @commands.command(name="steal")
    async def cmd_steal(self, ctx: commands.Context, target: MemberConverter = None):
        TIERS = [
            # (steal_chance, steal_pct, jail_chance, fee, jail_days)
            (0.10, 0.10, 0.25, 1000, 1),
            (0.07, 0.15, 0.35, 1000, 2),
            (0.05, 0.25, 0.50, 1000, 3),
        ]
        TRACK = 20

        if target is None:
            _ensure_user(ctx.author.id)
            jail_until = state.economy["users"][str(ctx.author.id)].get("jail_until", 0)
            if time.time() < jail_until:
                remaining = int(jail_until - time.time())
                hours, rem = divmod(remaining, 3600)
                minutes = rem // 60
                jail_status = f"🚔 **You are in jail!** Released in **{hours}h {minutes}m**."
            else:
                jail_status = "✅ **You are not in jail.**"
            lines = [
                "**Usage:** `!steal @user <tier>`",
                "",
                "**Tiers:**",
                "**1** — 10% steal chance, steal 10% | Jail chance: 25% | Fee if caught: 1,000 🪙 | Jail: 1 day",
                "**2** — 7% steal chance, steal 15%  | Jail chance: 35% | Fee if caught: 1,000 🪙 | Jail: 2 days",
                "**3** — 5% steal chance, steal 25%  | Jail chance: 50% | Fee if caught: 1,000 🪙 | Jail: 3 days",
                "",
                "If you get caught you might be **jailed** (locked out of !steal) or just fined.",
                "",
                jail_status,
            ]
            await ctx.send(embed=emb("🦹 Steal", "\n".join(lines), C_GOLD))
            return

        # Parse tier from the rest of the message, default to 1
        args = ctx.message.content.split()
        tier_str = args[-1] if len(args) >= 3 else None
        if tier_str and tier_str.isdigit() and 1 <= int(tier_str) <= 3:
            tier_num = int(tier_str)
        else:
            tier_num = 1

        thief_id = ctx.author.id
        victim_id = target.id

        if victim_id == thief_id:
            await ctx.send("You can't steal from yourself.")
            return
        if self.bot.user and victim_id == self.bot.user.id:
            await ctx.send("You can't steal from the house.")
            return

        _ensure_user(thief_id)
        _ensure_user(victim_id)

        thief_data = state.economy["users"][str(thief_id)]

        # Check jail
        jail_until = thief_data.get("jail_until", 0)
        if time.time() < jail_until:
            remaining = int(jail_until - time.time())
            hours, rem = divmod(remaining, 3600)
            minutes = rem // 60
            await ctx.send(embed=emb(
                "🚔 You're in Jail",
                f"**{ctx.author.display_name}** is locked up! Released in **{hours}h {minutes}m**.",
                C_RED,
            ))
            return

        steal_chance, steal_pct, jail_chance, fee, jail_days = TIERS[tier_num - 1]
        victim_bal = get_balance(victim_id)
        steal_amount = max(1, int(victim_bal * steal_pct))

        roll = random.random()
        success = roll < steal_chance

        # Animate the chase
        robber_pos = 0
        cop_pos = 0
        msg = None

        def build_frame(robber_p, cop_p, done=False, caught=False):
            robber_icon = "🏃" if not caught else "🤜"
            cop_icon = "👮"
            track_len = TRACK

            # Robber lane
            r_track = "░" * robber_p + robber_icon + "░" * (track_len - robber_p)
            # Cop lane (same lane, cop chases)
            c_track = "░" * cop_p + cop_icon + "░" * (track_len - cop_p)

            lines = [
                f"`{r_track}` 🦹",
                f"`{c_track}` 🚔",
            ]
            if done:
                if caught:
                    lines.append("\n**You got caught!** 🚨")
                else:
                    lines.append("\n**Escaped!** 💨")
            return "\n".join(lines)

        # Number of animation steps
        steps = 8
        embed_color = C_ORANGE

        if success:
            # Robber runs ahead, cop falls behind
            robber_steps = [int(TRACK * (i + 1) / steps) for i in range(steps)]
            cop_steps = [max(0, int(TRACK * (i + 1) / steps) - random.randint(3, 6)) for i in range(steps)]
        else:
            # Robber gets caught mid-track: robber slows, cop catches up at halfway
            half = steps // 2
            robber_steps = [int((TRACK // 2) * (i + 1) / half) if i < half else TRACK // 2 for i in range(steps)]
            cop_steps = [max(0, int((TRACK // 2) * (i + 1) / half) - random.randint(2, 4)) if i < half
                         else min(TRACK // 2, int((TRACK // 2) + (TRACK // 2) * (i - half + 1) / (steps - half)))
                         for i in range(steps)]

        for i in range(steps):
            caught_now = (not success) and (i == steps - 1)
            done_now = i == steps - 1
            frame = build_frame(robber_steps[i], cop_steps[i], done=done_now, caught=caught_now)
            e = emb("🦹 Heist in Progress...", frame, embed_color)
            if msg is None:
                msg = await ctx.send(embed=e)
            else:
                await msg.edit(embed=e)
            await asyncio.sleep(0.6)

        # Resolve outcome
        if success:
            if is_insured(victim_id, "steal"):
                _exp = get_insurance_expiry(victim_id)
                result_embed = emb("🛡️ Protected", f"**{target.display_name}** has insurance — your heist failed! (expires <t:{_exp}:R>)", C_GOLD)
            elif victim_bal < steal_amount:
                steal_amount = victim_bal
                if steal_amount <= 0:
                    result_embed = emb("🦹 Heist Failed", f"**{target.display_name}** is broke — nothing to steal!", C_RED)
                else:
                    deduct_balance(victim_id, steal_amount)
                    add_balance(thief_id, steal_amount)
                    result_embed = emb(
                        "🦹 Successful Heist!",
                        f"**{ctx.author.display_name}** stole **{steal_amount} 🪙** from **{target.display_name}**!\n"
                        f"Your balance: **{get_balance(thief_id)} 🪙**",
                        C_GREEN,
                    )
            else:
                deduct_balance(victim_id, steal_amount)
                add_balance(thief_id, steal_amount)
                result_embed = emb(
                    "🦹 Successful Heist!",
                    f"**{ctx.author.display_name}** stole **{steal_amount} 🪙** from **{target.display_name}**!\n"
                    f"Your balance: **{get_balance(thief_id)} 🪙**",
                    C_GREEN,
                )
        else:
            # Caught — roll for jail vs fine
            jailed = random.random() < jail_chance
            actual_fine = min(fee, get_balance(thief_id))
            if jailed:
                jail_until_ts = time.time() + jail_days * 86400
                thief_data["jail_until"] = jail_until_ts
                save_economy()
                deduct_balance(thief_id, actual_fine)
                result_embed = emb(
                    "🚔 Caught & Jailed!",
                    f"**{ctx.author.display_name}** was caught stealing from **{target.display_name}**!\n"
                    f"Fined **{actual_fine} 🪙** and jailed for **{jail_days} day(s)**.\n"
                    f"Balance: **{get_balance(thief_id)} 🪙**",
                    C_RED,
                )
            else:
                deduct_balance(thief_id, actual_fine)
                result_embed = emb(
                    "🚔 Caught!",
                    f"**{ctx.author.display_name}** was caught stealing from **{target.display_name}**!\n"
                    f"Fined **{actual_fine} 🪙**. You got lucky — no jail time.\n"
                    f"Balance: **{get_balance(thief_id)} 🪙**",
                    C_ORANGE,
                )

        await msg.edit(embed=result_embed)


    @commands.command(name="jail")
    async def cmd_jail(self, ctx: commands.Context, target: MemberConverter = None):
        member = target or ctx.author
        _ensure_user(member.id)
        jail_until = state.economy["users"][str(member.id)].get("jail_until", 0)
        if time.time() < jail_until:
            remaining = int(jail_until - time.time())
            hours, rem = divmod(remaining, 3600)
            minutes = rem // 60
            await ctx.send(embed=emb(
                "🚔 In Jail",
                f"**{member.display_name}** is locked up! Released in **{hours}h {minutes}m**.",
                C_RED,
            ))
        else:
            await ctx.send(embed=emb(
                "✅ Not in Jail",
                f"**{member.display_name}** is a free citizen.",
                C_GREEN,
            ))


    @commands.command(name="records", aliases=["record", "rec"])
    async def cmd_records(self, ctx: commands.Context):
        """Display all-time records for economy and games."""
        if ctx.guild is None:
            await ctx.send(embed=emb("🏆 Records", "Records are only available in servers.", C_RED))
            return

        r = load_records(ctx.guild.id)

        def fmt(cat: str, label: str, extra_fn=None) -> str:
            rec = r.get(cat)
            if not rec:
                return f"**{label}:** *none yet*"
            name = rec.get("holder_name", "?")
            val = rec["value"]
            base = f"**{label}:** {val:,} 🪙 — **{name}**"
            if extra_fn:
                base += extra_fn(rec)
            return base

        # Highest balance: live max across all economy users
        bal_candidates = [
            (uid_str, data["balance"])
            for uid_str, data in state.economy["users"].items()
            if data["balance"] > 0
        ]
        if bal_candidates:
            top_uid_str, top_bal = max(bal_candidates, key=lambda x: x[1])
            top_member = ctx.guild.get_member(int(top_uid_str))
            if top_member:
                top_name = top_member.display_name
            else:
                try:
                    top_user = await self.bot.fetch_user(int(top_uid_str))
                    top_name = top_user.display_name
                except Exception:
                    top_name = top_uid_str
            highest_bal_str = f"**Highest Balance:** {top_bal:,} 🪙 — **{top_name}**"
        else:
            highest_bal_str = "**Highest Balance:** *none yet*"

        # Most hangman wins
        hangman_wins_entries = [(k, v) for k, v in r.items() if k.startswith("hangman_wins_")]
        if hangman_wins_entries:
            _, top_v = max(hangman_wins_entries, key=lambda x: x[1]["value"])
            hm_wins_str = f"**Most Hangmans Completed:** {top_v['value']} — **{top_v['holder_name']}**"
        else:
            hm_wins_str = "**Most Hangmans Completed:** *none yet*"

        lines = [
            highest_bal_str,
            fmt("lottery", "Biggest Lottery Payout"),
            fmt("slots_jackpot", "Biggest Slots Jackpot",
                lambda rec: f"\n  ↳ Symbols: {rec.get('symbols', '?')} • Bet: {rec['bet']:,} 🪙" if rec.get('bet') is not None else ""),
            fmt("slots_non_jackpot", "Biggest Slots Non-Jackpot",
                lambda rec: f"\n  ↳ Symbols: {rec.get('symbols', '?')} • Bet: {rec['bet']:,} 🪙" if rec.get('bet') is not None else ""),
            fmt("blackjack", "Biggest Blackjack Payout",
                lambda rec: f"\n  ↳ Hand: {rec.get('player_hand', '?')} ({rec.get('player_score', '?')}) • Dealer: {rec.get('dealer_score', '?')}"),
            fmt("flip", "Biggest Flip Payout"),
            fmt("hangman_payout", "Biggest Hangman Payout",
                lambda rec: f"\n  ↳ Word: `{rec.get('word', '?')}`"),
            hm_wins_str,
        ]

        embed = discord.Embed(title="🏆 All-Time Records", color=C_GOLD)
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

    @commands.command(name="pay", aliases=["give", "gift", "donate"])
    async def cmd_pay(self, ctx: commands.Context, recipient: MemberConverter = None, amount: str = None):
        if recipient is None or amount is None:
            await ctx.send("Usage: `!pay @user <amount>`")
            return
        if recipient.id == ctx.author.id:
            await ctx.send(f"**{ctx.author.display_name}** can't pay themselves.")
            return
        amount = await parse_amount(ctx, amount)
        if amount is None:
            return
        if not await shop_charge(ctx, ctx.author.id, amount):
            return
        if self.bot.user and recipient.id == self.bot.user.id and ctx.guild:
            add_guild_house(ctx.guild.id, amount)
            await ctx.send(embed=emb(
                "💸 Payment Sent",
                f"**{ctx.author.display_name}** paid **{amount} 🪙** to the house pot.\n"
                f"Your balance: **{get_balance(ctx.author.id)} 🪙**",
                C_GREEN,
            ))
            return
        add_balance(recipient.id, amount)
        await ctx.send(embed=emb(
            "💸 Payment Sent",
            f"**{ctx.author.display_name}** paid **{recipient.display_name}** {amount} 🪙\n"
            f"Your balance: **{get_balance(ctx.author.id)} 🪙**",
            C_GREEN,
        ))



async def setup(bot):
    await bot.add_cog(EconomyCog(bot))
