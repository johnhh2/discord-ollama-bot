import asyncio
import logging
import random

import discord
from discord.ext import commands, tasks

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE, C_GREY, announce_record, parse_int_amount,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, drain_bot_balance_into_lottery, announce_new_lottery,
    _ensure_user, _ct_now, _ct_today, lottery_month_key, next_lottery_draw_dt,
    record_gambling_event, add_guild_house,
)
from src.persistence import (
    save_lottery,
    load_lottery, try_set_record, log_notable_event,
)
# Attribute access (not `from`-imported) so the conftest stubs on the
# persistence package reach the calls here.
import src.persistence as persistence
from src.guild_config import get_guild_cfg
from src.confirm_view import confirm_purchase
from src import state, status_manager


async def record_tickets_purchased(count: int) -> None:
    """Bump the gameplay-day ticket counter behind the presence status line.

    The in-memory bump is synchronous (no await before it), so concurrent
    purchases can't interleave mid-update; the DB write is an atomic
    per-delta increment, restored into state at boot by init_db_state.
    """
    today = _ct_today()
    if state.lottery_tickets_today.get("date") != today:
        state.lottery_tickets_today["date"] = today
        state.lottery_tickets_today["count"] = 0
    state.lottery_tickets_today["count"] += count
    try:
        await persistence.bump_daily_counter(today, "lottery_tickets", count)
    except Exception:
        logging.exception("[lottery] failed to persist daily ticket counter")


def lottery_status_text() -> "str | None":
    """Presence line for the status manager; None hides it."""
    if state.lottery_tickets_today.get("date") != _ct_today():
        return None
    count = int(state.lottery_tickets_today.get("count") or 0)
    if count <= 0:
        return None
    return f"{count}x 🎟️ sold today"


class LotteryCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        status_manager.register("lottery", lottery_status_text)
        # Per-guild lock serializing every load→mutate→save of the lottery
        # snapshot (purchases and the draw). save_lottery rewrites the whole
        # snapshot, so unserialized concurrent writers erase each other's
        # tickets — and a purchase saved after the draw would restore the
        # stale "undrawn" pool, making the scheduler pay it out again.
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self.lottery_scheduler.start()

    def cog_unload(self):
        status_manager.unregister("lottery")
        self.lottery_scheduler.cancel()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    @tasks.loop(minutes=1)
    async def lottery_scheduler(self):
        """Check every minute if it's the 1st of the month, 6pm CT, for lottery tasks."""
        now = _ct_now()
        if now.day != 1:
            return

        for guild in self.bot.guilds:
            # One guild's Discord/DB hiccup must not kill the loop for good
            # (tasks.loop stops permanently on unhandled non-retry exceptions)
            # nor skip the remaining guilds' draws.
            try:
                await self._run_guild_schedule(guild, now)
            except Exception:
                logging.exception("[lottery] scheduler failed for guild %s", guild.id)

    async def _run_guild_schedule(self, guild, now):
        cfg = get_guild_cfg(guild.id)
        lottery_channel_id = cfg.get("lottery_channel")
        if not lottery_channel_id:
            return

        try:
            channel = await self.bot.fetch_channel(lottery_channel_id)
        except Exception:
            return

        # The last_drawn_week/last_posted_week keys (and DB columns) now hold
        # YYYYMM month keys; the names are kept to avoid a schema migration.
        current_month = lottery_month_key(now)
        pool = 0
        players: dict = {}
        drew = False

        async with self._lock(guild.id):
            lottery = await load_lottery(guild.id)

            # 6pm: draw winner and reset lottery
            if now.hour >= 18 and lottery.get("last_drawn_week") != current_month:
                pool = lottery.get("prize_pool", 0)
                players = lottery.get("players", {})
                drew = True
                # Persist the drawn marker + reset BEFORE paying/announcing:
                # a crash mid-payout must not leave last_drawn_week stale, or
                # the next tick (or reboot) re-draws and pays the pool again.
                lottery = {"prize_pool": 5000, "players": {}, "last_drawn_week": current_month, "last_posted_week": 0}
                await drain_bot_balance_into_lottery(lottery, guild.id)
                await save_lottery(guild.id, lottery)

            # 7pm: announce new lottery
            if now.hour >= 19 and lottery.get("last_posted_week") != current_month:
                lottery["last_posted_week"] = current_month
                await save_lottery(guild.id, lottery)
                await announce_new_lottery(channel, lottery["prize_pool"], now)

        if drew and players and pool > 0:
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
            cfg = get_guild_cfg(guild.id)
            if cfg.get("gambler_role_enabled", False):
                gamblers_role = discord.utils.get(guild.roles, name="Gamblers")
                if gamblers_role:
                    await channel.send(f"{gamblers_role.mention} 🎰 The lottery was just won!")

            if new_lottery_record:
                await announce_record(channel, "lottery", winner.display_name, pool)
            if new_bal_record:
                await announce_record(channel, "highest_balance", winner.display_name, await get_balance(int(winner_id)))

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
        current_month = lottery_month_key(now_cst)
        in_transition = (
            now_cst.day == 1
            and now_cst.hour >= 18
            and now_cst.hour < 19
            and lottery.get("last_posted_week") != current_month
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

            # Next 1st-of-month 6pm CT draw (handles CST/CDT automatically)
            timestamp = int(next_lottery_draw_dt(now_cst).timestamp())

            total_tickets = sum(players_dict.values())
            info = f"**Prize Pool:** {pool:,} 🪙 (+1,000 🪙 per player)\n"
            info += f"**Players:** {len(players_dict)}\n"
            info += "**Ticket Cost:** 10 🪙 for 1 🎟️\n\n"
            info += f"**Your Tickets:** {user_tickets:,} / {total_tickets:,} total\n"
            info += "Use `!lottery <n>` to buy more tickets"

            await ctx.send(embed=emb(f"🎰 Current Lottery • ends <t:{timestamp}:R>", info, C_PURPLE))
            return

        # Block purchases in the 1-hour window before the draw (5-6pm CT on the 1st)
        if now_cst.day == 1 and now_cst.hour == 17:
            await ctx.send(embed=emb("🔒 Lottery Locked", "Ticket sales are closed for the final hour before the draw. Check back after 6pm CT!", C_RED))
            return

        TICKET_CAP = 5000
        players_dict = lottery.get("players", {})
        current_tickets = int(players_dict.get(str(uid), 0))

        if n.lower() == "match":
            # Buy enough tickets to tie the current leader in this guild's lottery.
            other_max = max(
                (int(v) for k, v in players_dict.items() if k != str(uid)),
                default=0,
            )
            if other_max <= current_tickets:
                await ctx.send(embed=emb(
                    "🎟️ Nothing to Match",
                    f"You already have **{current_tickets:,}** 🎟️ — nobody else is ahead of you.",
                    C_GOLD,
                ))
                return
            tickets = other_max - current_tickets
            # Respect the per-player cap even when matching.
            if current_tickets + tickets > TICKET_CAP:
                tickets = TICKET_CAP - current_tickets
            if tickets <= 0:
                await ctx.send(embed=emb(
                    "🎟️ Ticket Cap Reached",
                    f"You're already at the **{TICKET_CAP:,}** 🎟️ cap.",
                    C_RED,
                ))
                return
            cost = tickets * 10
            payer_bal = await get_balance(uid)
            if payer_bal < cost:
                await ctx.send(embed=emb(
                    "💸 Insufficient Funds",
                    f"Matching the leader costs **{cost:,} 🪙** for **{tickets:,}** 🎟️ — you have **{payer_bal:,} 🪙**.",
                    C_RED,
                ))
                return

            confirmed = await confirm_purchase(
                ctx,
                title="🎰 Match Leader",
                description=f"Buy **{tickets:,}** 🎟️ to tie the leader at **{other_max:,}** tickets.",
                cost=cost,
                payer=ctx.author,
            )
            if not confirmed:
                return

            # Re-validate after the confirm window — leader may have bought more,
            # the lock window may have opened, or balance may have dropped.
            now_cst = _ct_now()
            if now_cst.day == 1 and now_cst.hour == 17:
                await ctx.send(embed=emb("🔒 Lottery Locked", "Ticket sales closed during confirmation.", C_RED))
                return
            lottery = await load_lottery(ctx.guild.id)
            players_dict = lottery.get("players", {})
            current_tickets = int(players_dict.get(str(uid), 0))
            if current_tickets + tickets > TICKET_CAP:
                await ctx.send(embed=emb(
                    "🎟️ Ticket Cap Reached",
                    f"Your tickets changed during confirmation — cancelling to avoid going over the **{TICKET_CAP:,}** cap.",
                    C_RED,
                ))
                return
        else:
            tickets = parse_int_amount(n)
            if tickets is None or tickets <= 0:
                await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a positive number.", C_RED))
                return

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

        # Serialize with other purchases and the draw: save_lottery rewrites
        # the whole snapshot, so an unlocked concurrent purchase would erase
        # this buyer's tickets (while keeping their coins), and a save racing
        # the 1st-of-month draw would resurrect the paid-out pool.
        async with self._lock(ctx.guild.id):
            # Re-load inside the lock — the snapshot from earlier in this
            # command may be stale by now.
            lottery = await load_lottery(ctx.guild.id)
            players = lottery.setdefault("players", {})
            current_tickets = int(players.get(str(uid), 0))
            if current_tickets + tickets > TICKET_CAP:
                await ctx.send(embed=emb(
                    "🎟️ Ticket Cap Reached",
                    f"Each player can hold at most **{TICKET_CAP:,}** 🎟️ per lottery.",
                    C_RED,
                ))
                return

            if not await deduct_balance(uid, cost):
                await ctx.send(embed=emb("💸 Insufficient Funds", f"Need {cost:,} 🪙. Balance: {await get_balance(uid):,} 🪙", C_RED))
                return
            await record_gambling_event(ctx.guild.id, uid, lost=cost)

            # Add to lottery
            was_new_player = str(uid) not in players

            players[str(uid)] = players.get(str(uid), 0) + tickets
            lottery.setdefault("prize_pool", 0)
            lottery["prize_pool"] += tickets * 7
            if was_new_player:
                lottery["prize_pool"] += 1000

            await add_guild_house(ctx.guild.id, tickets * 3)

            await save_lottery(ctx.guild.id, lottery)
            await record_tickets_purchased(tickets)

        bonus_msg = "(+1,000 bonus as new player)" if was_new_player else ""

        # Calculate when lottery ends
        timestamp = int(next_lottery_draw_dt(now_cst).timestamp())

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

