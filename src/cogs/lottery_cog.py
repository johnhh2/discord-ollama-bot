import random
import datetime

import discord
from discord.ext import commands, tasks

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE, C_GREY, announce_record, parse_int_amount,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, drain_bot_balance_into_lottery, announce_new_lottery,
    _ensure_user, _ct_now, _ct_today, lottery_week_key, record_gambling_event, add_guild_house,
)
from src.persistence import (
    save_lottery,
    load_lottery, try_set_record, log_notable_event,
)
from src.guild_config import get_guild_cfg


class LotteryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lottery_scheduler.start()

    def cog_unload(self):
        self.lottery_scheduler.cancel()

    @tasks.loop(minutes=1)
    async def lottery_scheduler(self):
        """Check every minute if it's Saturday 6pm CST for lottery tasks."""
        now = _ct_now()
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

            lottery = await load_lottery(guild.id)
            current_week = lottery_week_key(now)

            # 6pm: draw winner and reset lottery
            if now.hour >= 18 and lottery.get("last_drawn_week") != current_week:
                pool = lottery.get("prize_pool", 0)
                players = lottery.get("players", {})

                if players and pool > 0:
                    player_ids = list(players.keys())
                    weights = [players[pid] for pid in player_ids]
                    winner_id = random.choices(player_ids, weights=weights, k=1)[0]
                    winner = await self.bot.fetch_user(int(winner_id))
                    new_bal_record = await add_balance(int(winner_id), pool, guild_id=guild.id, holder_name=winner.display_name)
                    await record_gambling_event(guild.id, int(winner_id), gained=pool)
                    new_lottery_record = await try_set_record(guild.id, "lottery", pool, int(winner_id), winner.display_name)
                    # Log every lottery win for !recap — a non-record win
                    # never reaches announce_record, so log it here directly.
                    try:
                        await log_notable_event(
                            guild.id, _ct_today(), "lottery_win", None,
                            winner.display_name, pool,
                        )
                    except Exception:
                        pass

                    embed = discord.Embed(title="🎰 Lottery Results", color=C_GOLD)
                    embed.description = (
                        f"**Winner:** {winner.mention}\n"
                        f"**Prize:** {pool:,} 🪙\n"
                        f"**Players:** {len(players)}\n"
                        f"**Tickets Sold:** {sum(players.values())}"
                    )
                    await channel.send(embed=embed, silent=False)

                    # Ping Gamblers role if enabled
                    if cfg.get("gambler_role_enabled", False):
                        gamblers_role = discord.utils.get(guild.roles, name="Gamblers")
                        if gamblers_role:
                            await channel.send(f"{gamblers_role.mention} 🎰 The lottery was just won!")

                    if new_lottery_record:
                        await announce_record(channel, "lottery", winner.display_name, pool)
                    if new_bal_record:
                        await announce_record(channel, "highest_balance", winner.display_name, await get_balance(int(winner_id)))

                lottery = {"prize_pool": 2000, "players": {}, "last_drawn_week": current_week, "last_posted_week": 0}
                await drain_bot_balance_into_lottery(lottery, guild.id)
                await save_lottery(guild.id, lottery)

            # 7pm: announce new lottery
            if now.hour >= 19 and lottery.get("last_posted_week") != current_week:
                lottery["last_posted_week"] = current_week
                await save_lottery(guild.id, lottery)
                await announce_new_lottery(channel, lottery["prize_pool"], now)

    @commands.command(name="lottery")
    async def cmd_lottery(self, ctx: commands.Context, n: str = None):
        uid = ctx.author.id
        await _ensure_user(uid)

        # Check if lottery channel is configured
        if ctx.guild is None:
            await ctx.send(embed=emb("🎰 Lottery", "Lottery only works in servers.", C_RED))
            return

        cfg = get_guild_cfg(ctx.guild.id)
        lottery_channel_id = cfg.get("lottery_channel")
        if not lottery_channel_id:
            await ctx.send(embed=emb("🎰 Lottery Disabled", "Lottery channel not configured.", C_GREY))
            return

        lottery = await load_lottery(ctx.guild.id)

        # Check if we're in the 6-7pm window (draw done, new lottery not yet announced)
        now_cst = _ct_now()
        current_week = lottery_week_key(now_cst)
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

            total_tickets = sum(players_dict.values())
            info = f"**Prize Pool:** {pool:,} 🪙 (+1,000 🪙 per player)\n"
            info += f"**Players:** {len(players_dict)}\n"
            info += "**Ticket Cost:** 10 🪙 for 1 🎟️\n\n"
            info += f"**Your Tickets:** {user_tickets:,} / {total_tickets:,} total\n"
            info += "Use `!lottery <n>` to buy more tickets"

            await ctx.send(embed=emb(f"🎰 Current Lottery • ends <t:{timestamp}:R>", info, C_PURPLE))
            return

        # Block purchases in the 1-hour window before the draw (5-6pm CT Saturday)
        if now_cst.weekday() == 5 and now_cst.hour == 17:
            await ctx.send(embed=emb("🔒 Lottery Locked", "Ticket sales are closed for the final hour before the draw. Check back after 6pm CT!", C_RED))
            return

        tickets = parse_int_amount(n)
        if tickets is None or tickets <= 0:
            await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a positive number.", C_RED))
            return

        TICKET_CAP = 5000
        current_tickets = int(lottery.get("players", {}).get(str(uid), 0))
        if current_tickets + tickets > TICKET_CAP:
            remaining = TICKET_CAP - current_tickets
            await ctx.send(embed=emb(
                "🎟️ Ticket Cap Reached",
                f"Each player can hold at most **{TICKET_CAP:,}** 🎟️ per lottery.\n"
                f"You have **{current_tickets:,}**; you can buy up to **{remaining:,}** more.",
                C_RED,
            ))
            return

        cost = tickets * 10
        if not await deduct_balance(uid, cost):
            await ctx.send(embed=emb("💸 Insufficient Funds", f"Need {cost:,} 🪙. Balance: {await get_balance(uid):,} 🪙", C_RED))
            return
        await record_gambling_event(ctx.guild.id, uid, lost=cost)

        # Add to lottery
        players = lottery.setdefault("players", {})
        was_new_player = str(uid) not in players

        players[str(uid)] = players.get(str(uid), 0) + tickets
        lottery.setdefault("prize_pool", 0)
        lottery["prize_pool"] += tickets * 7
        if was_new_player:
            lottery["prize_pool"] += 1000

        await add_guild_house(ctx.guild.id, tickets * 3)

        await save_lottery(ctx.guild.id, lottery)

        bonus_msg = "(+1,000 bonus as new player)" if was_new_player else ""

        # Calculate when lottery ends
        days_until_saturday = (5 - now_cst.weekday()) % 7
        next_saturday = now_cst + datetime.timedelta(days=days_until_saturday)
        next_saturday = next_saturday.replace(hour=18, minute=0, second=0, microsecond=0)
        if next_saturday <= now_cst:
            next_saturday += datetime.timedelta(weeks=1)
        timestamp = int(next_saturday.timestamp())

        total_tickets = sum(players.values())
        embed_msg = emb(
            "🎰 Tickets Purchased",
            f"**{ctx.author.display_name}** bought **{tickets:,}** 🎟️ for **{cost:,} 🪙**\n\n"
            f"**Prize Pool:** {lottery['prize_pool']:,} 🪙 {bonus_msg}\n"
            f"**Your Tickets:** {players[str(uid)]:,} / {total_tickets:,} total\n"
            f"**Ends:** <t:{timestamp}:R>",
            C_GREEN
        )
        await ctx.send(embed=embed_msg)



async def setup(bot):
    await bot.add_cog(LotteryCog(bot))

