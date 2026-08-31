import asyncio
import datetime
import logging
import random
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE, C_GREY, announce_record,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, get_total_balance, drain_bot_balance_into_lottery, announce_new_lottery,
    _ensure_user, _ct_now, _ct_today, lottery_month_key, lottery_week_key, next_lottery_draw_dt,
    next_daily_reset_ts, record_gambling_event, add_guild_house,
)
from src.persistence import (
    save_lottery,
    load_lottery, try_set_record, log_notable_event,
)
# Attribute access (not `from`-imported) so the conftest stubs on the
# persistence package reach the calls here.
import src.persistence as persistence
from src.config import LOTTERY_SEED_POOL
from src.guild_config import get_guild_cfg
from src.confirm_view import confirm_purchase
from src import state, status_manager


# One purchasable ticket per user per server per gameplay-day — sold from the
# dailies-channel 🎟️ button or the confirm prompt on !lottery. No bulk
# buying, no discounts.
DAILY_TICKET_PRICE = 1000
# Of each 1,000 🪙 ticket: 700 to the prize pool, 300 to the guild house
# (same 70/30 split the old 10-coin tickets had).
TICKET_POOL_SHARE = 700
TICKET_HOUSE_SHARE = DAILY_TICKET_PRICE - TICKET_POOL_SHARE
NEW_PLAYER_POOL_BONUS = 1000
# Free weekly chess-win tickets, granted as a cumulative weekly ceiling (per
# server, like the daily): any chess win — PvP included — is worth 1 ticket,
# beating a 600+ Elo bot 2, a 1100+ bot 3. Each win only tops the winner up
# to its ceiling: beating 100 Elo then 600 Elo pays 1 + 1, not 1 + 2, and
# nothing ever exceeds CHESS_TICKET_WEEKLY_CAP in one week.
CHESS_TICKET_BOT_TIERS = ((1100, 3), (600, 2))
CHESS_TICKET_WEEKLY_CAP = 3


def chess_ticket_ceiling(bot_elo: "int | None") -> int:
    """Weekly ticket ceiling a chess win at this strength tops the winner up
    to. `bot_elo` is None for PvP wins."""
    if bot_elo is not None:
        for threshold, ceiling in CHESS_TICKET_BOT_TIERS:
            if bot_elo >= threshold:
                return ceiling
    return 1
# The reworked ticket economy starts with the September 2026 lottery. The
# August 2026 pot still holds thousands of old 10-coin bulk tickets, so
# selling a 1,000 🪙 ticket (or granting a free chess one) into it would be a
# rip-off — no tickets of any kind until the 9/1/2026 6pm CT draw resets the
# pool. This gate (and its three call sites) can be deleted once that date
# has passed.
TICKET_SALES_START_CT = datetime.datetime(2026, 9, 1, 18, 0, tzinfo=ZoneInfo("America/Chicago"))


def _grant_row(guild_id: int, uid: int) -> dict:
    """The user's ticket-grant gate row for this guild (created empty)."""
    return state.lottery_ticket_grants.setdefault(
        (guild_id, uid),
        {"daily_day": None, "chess_week": None, "chess_tickets": 0},
    )


async def _persist_grant_row(guild_id: int, uid: int, row: dict) -> None:
    try:
        await persistence.save_lottery_ticket_grant(guild_id, uid, row)
    except Exception:
        logging.exception("[lottery] failed to persist ticket grant for %s/%s", guild_id, uid)


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


def _sales_locked(now_cst) -> bool:
    """Ticket sales close for the final hour before the draw (5-6pm CT on the 1st)."""
    return now_cst.day == 1 and now_cst.hour == 17


def _sales_not_started(now_cst) -> bool:
    """True while the one-time TICKET_SALES_START_CT launch gate is closed."""
    return now_cst < TICKET_SALES_START_CT


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
                lottery = {"prize_pool": LOTTERY_SEED_POOL, "players": {}, "last_drawn_week": current_month, "last_posted_week": 0}
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
                    await channel.send(
                        f"{gamblers_role.mention} 🎰 The lottery was just won!",
                        allowed_mentions=discord.AllowedMentions(roles=[gamblers_role]),
                    )

            # notify=True: the draw is scheduled — the winner isn't watching,
            # so their record ping must actually notify.
            if new_lottery_record:
                await announce_record(channel, "lottery", winner.display_name, pool, holder_id=int(winner_id), notify=True)
            if new_bal_record:
                await announce_record(channel, "highest_balance", winner.display_name, await get_total_balance(int(winner_id)), holder_id=int(winner_id), notify=True)

    async def _execute_purchase(self, guild_id: int, uid: int, tickets: int, cost: int) -> dict:
        """Add `tickets` to the guild's lottery for `uid`, charging `cost`
        (0 for the free chess-win tickets).

        Runs inside the guild lock: save_lottery rewrites the whole snapshot,
        so an unlocked concurrent purchase would erase this buyer's tickets
        (while keeping their coins), and a save racing the 1st-of-month draw
        would resurrect the paid-out pool.

        The once-a-day / once-a-week gates live with the CALLERS, which must
        claim them synchronously before awaiting (see CLAUDE.md concurrency
        rules) and roll them back if this returns an error.

        Returns {"error": "funds", "cost": n} on failure, else the purchase
        details for the caller to render.
        """
        async with self._lock(guild_id):
            # Load inside the lock — any earlier snapshot may be stale.
            lottery = await load_lottery(guild_id)
            players = lottery.setdefault("players", {})

            if cost > 0:
                if not await deduct_balance(uid, cost):
                    return {"error": "funds", "cost": cost}
                await record_gambling_event(guild_id, uid, lost=cost)

            was_new_player = str(uid) not in players
            players[str(uid)] = players.get(str(uid), 0) + tickets
            lottery.setdefault("prize_pool", 0)
            if cost > 0:
                lottery["prize_pool"] += TICKET_POOL_SHARE * tickets
                await add_guild_house(guild_id, TICKET_HOUSE_SHARE * tickets)
            if was_new_player:
                lottery["prize_pool"] += NEW_PLAYER_POOL_BONUS

            await save_lottery(guild_id, lottery)
            if cost > 0:
                await record_tickets_purchased(tickets)

        return {
            "tickets": tickets,
            "cost": cost,
            "was_new_player": was_new_player,
            "prize_pool": lottery["prize_pool"],
            "user_tickets": players[str(uid)],
            "total_tickets": sum(players.values()),
        }

    async def buy_daily_ticket(self, member, channel, guild, *, silent: bool = False) -> None:
        """Buy `member`'s once-a-day 1,000 🪙 ticket for this guild's lottery.

        Backs both purchase paths: the dailies-channel 🎟️ reaction
        (src/cogs/dailies_cog.py, silent=True — the sweeper cleans the
        results up after 5 minutes) and the !lottery confirm prompt.
        """
        uid = member.id
        await _ensure_user(uid)

        cfg = get_guild_cfg(guild.id)
        if not cfg.get("lottery_channel"):
            await channel.send(embed=emb("🎰 Lottery Disabled", "Lottery channel not configured.", C_GREY), silent=silent)
            return

        now_cst = _ct_now()
        if _sales_not_started(now_cst):
            ts = int(TICKET_SALES_START_CT.timestamp())
            await channel.send(embed=emb(
                "🔒 Ticket Sales Paused",
                "The lottery switched to a new ticket system, and the current pot "
                "still holds the old cheap bulk tickets — no fair selling "
                f"{DAILY_TICKET_PRICE:,} 🪙 tickets into it.\n"
                f"Sales reopen when the next lottery starts, after the draw <t:{ts}:R>.",
                C_GREY,
            ), silent=silent)
            return
        if _sales_locked(now_cst):
            await channel.send(embed=emb("🔒 Lottery Locked", "Ticket sales are closed for the final hour before the draw. Check back after 6pm CT!", C_RED), silent=silent)
            return

        # Gate-and-claim runs synchronously (no await between the check and
        # the claim), so a second concurrent invocation bails here instead of
        # buying a second ticket. Rolled back if the charge fails.
        today = _ct_today()
        row = _grant_row(guild.id, uid)
        prior = row.get("daily_day")
        if prior == today:
            await channel.send(embed=emb(
                "🎟️ Daily Ticket Already Bought",
                f"**{member.display_name}**, you already have today's ticket for "
                f"this server. Next one <t:{next_daily_reset_ts()}:R>.",
                C_GREY,
            ), silent=silent)
            return
        row["daily_day"] = today

        result = await self._execute_purchase(guild.id, uid, 1, DAILY_TICKET_PRICE)
        if result.get("error") == "funds":
            row["daily_day"] = prior
            await channel.send(embed=emb(
                "💸 Insufficient Funds",
                f"Today's ticket costs **{DAILY_TICKET_PRICE:,} 🪙** — you have "
                f"**{await get_balance(uid):,} 🪙**.",
                C_RED,
            ), silent=silent)
            return
        await _persist_grant_row(guild.id, uid, row)

        bonus_msg = "(+1,000 bonus as new player)" if result["was_new_player"] else ""
        timestamp = int(next_lottery_draw_dt(now_cst).timestamp())
        await channel.send(embed=emb(
            "🎰 Daily Ticket Purchased",
            f"**{member.display_name}** bought today's 🎟️ for **{DAILY_TICKET_PRICE:,} 🪙**\n\n"
            f"**Prize Pool:** {result['prize_pool']:,} 🪙 {bonus_msg}\n"
            f"**Your Tickets:** {result['user_tickets']:,} / {result['total_tickets']:,} total\n"
            f"**Ends:** <t:{timestamp}:R>",
            C_GREEN,
        ), silent=silent)

    async def award_chess_tickets(self, guild, uid: int, bot_elo: "int | None" = None) -> int:
        """Free weekly lottery tickets for a chess win in `guild`, topping the
        winner up to the win's ceiling (chess_ticket_ceiling: any win 1,
        600+ Elo bot 2, 1100+ bot 3) within the current ISO week. Pass
        bot_elo=None for PvP wins. Returns how many tickets were granted
        (0 when already at the ceiling or lottery disabled).

        Called from the chess endgame path (src/games/chess.py) after a
        human wins a game.
        """
        cfg = get_guild_cfg(guild.id)
        if not cfg.get("lottery_channel"):
            return 0
        # Launch gate: no free tickets into the pre-rework pot either — and
        # the weekly counter stays untouched, so nothing is burned.
        if _sales_not_started(_ct_now()):
            return 0

        # Claim the weekly counter synchronously before any await; roll back
        # if the grant fails so the win isn't burned for nothing.
        week = lottery_week_key()
        row = _grant_row(guild.id, uid)
        prior_week, prior_count = row.get("chess_week"), int(row.get("chess_tickets") or 0)
        if prior_week != week:
            row["chess_week"] = week
            row["chess_tickets"] = 0
        grant = chess_ticket_ceiling(bot_elo) - row["chess_tickets"]
        if grant <= 0:
            return 0
        row["chess_tickets"] += grant

        await _ensure_user(uid)
        result = await self._execute_purchase(guild.id, uid, grant, 0)
        if result.get("error"):
            row["chess_week"], row["chess_tickets"] = prior_week, prior_count
            return 0
        await _persist_grant_row(guild.id, uid, row)
        return grant

    @commands.command(name="lottery")
    async def cmd_lottery(self, ctx: commands.Context):
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

        # Show lottery info
        pool = lottery.get("prize_pool", 0)
        players_dict = lottery.get("players", {})
        user_tickets = int(players_dict.get(str(uid), 0))
        total_tickets = sum(players_dict.values())

        # Next 1st-of-month 6pm CT draw (handles CST/CDT automatically)
        timestamp = int(next_lottery_draw_dt(now_cst).timestamp())

        pre_launch = _sales_not_started(now_cst)
        locked = _sales_locked(now_cst)
        bought_today = _grant_row(ctx.guild.id, uid).get("daily_day") == _ct_today()
        if pre_launch:
            daily_status = (
                "🔒 paused — the current pot predates the new ticket system; "
                f"sales open <t:{int(TICKET_SALES_START_CT.timestamp())}:R> with the next lottery"
            )
        elif locked:
            daily_status = "🔒 sales closed for the final hour before the draw"
        elif bought_today:
            daily_status = f"✅ bought — next one <t:{next_daily_reset_ts()}:R>"
        else:
            daily_status = "available — confirm below, or react 🎟️ in the dailies channel"

        info = f"**Prize Pool:** {pool:,} 🪙 (+1,000 🪙 per player)\n"
        info += f"**Players:** {len(players_dict)}\n"
        info += f"**Your Tickets:** {user_tickets:,} / {total_tickets:,} total\n\n"
        info += f"**Today's ticket** ({DAILY_TICKET_PRICE:,} 🪙, 1 per day per server): {daily_status}\n"
        info += (
            "**Chess bonus:** free weekly 🎟️ for chess wins — any win 1, a 600+ "
            f"Elo bot 2, 1100+ 3 (each win tops you up to its tier, max {CHESS_TICKET_WEEKLY_CAP}/week)"
        )

        await ctx.send(embed=emb(f"🎰 Current Lottery • ends <t:{timestamp}:R>", info, C_PURPLE))

        # Offer today's ticket when it's still unbought.
        if pre_launch or locked or bought_today:
            return
        confirmed = await confirm_purchase(
            ctx,
            title="🎟️ Daily Lottery Ticket",
            description="Buy today's lottery ticket for this server?",
            cost=DAILY_TICKET_PRICE,
            payer=ctx.author,
        )
        if not confirmed:
            return
        # buy_daily_ticket re-checks the gate and the lock window itself —
        # both may have changed during the confirm wait (e.g. a concurrent
        # dailies 🎟️ click).
        await self.buy_daily_ticket(ctx.author, ctx.channel, ctx.guild)


async def setup(bot):
    await bot.add_cog(LotteryCog(bot))
