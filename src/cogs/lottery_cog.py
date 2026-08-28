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


TICKET_PRICE = 10
DISCOUNT_TICKET_PRICE = 5   # 50% off
DISCOUNT_DAILY_CAP = 100    # first N tickets per user per gameplay-day
TICKET_CAP = 5000           # max tickets one player can hold per lottery


def discount_tickets_remaining(user: dict, today: str) -> int:
    """How many half-price tickets the user has left today.

    Mutates `user` to roll the counter when `lottery_disc_date` is stale
    (mirrors scratchoff_attempts_remaining) and normalizes
    `lottery_disc_used` to an int so callers can safely `+=` it.
    """
    if user.get("lottery_disc_date") != today:
        user["lottery_disc_date"] = today
        user["lottery_disc_used"] = 0
    elif "lottery_disc_used" not in user:
        user["lottery_disc_used"] = 0
    return max(0, DISCOUNT_DAILY_CAP - user["lottery_disc_used"])


def ticket_cost(tickets: int, discount_remaining: int) -> "tuple[int, int]":
    """Return (total cost, how many of `tickets` are billed at half price)."""
    discounted = min(tickets, max(0, discount_remaining))
    return discounted * DISCOUNT_TICKET_PRICE + (tickets - discounted) * TICKET_PRICE, discounted


def max_affordable_tickets(balance: int, discount_remaining: int) -> int:
    """How many tickets `balance` buys when the first `discount_remaining` are half price."""
    disc = max(0, discount_remaining)
    disc_cost = disc * DISCOUNT_TICKET_PRICE
    if balance < disc_cost:
        return balance // DISCOUNT_TICKET_PRICE
    return disc + (balance - disc_cost) // TICKET_PRICE


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
                # Automatch opt-ins last one lottery — the fresh pool starts
                # with no auto-buyers until players re-arm.
                try:
                    await persistence.clear_lottery_automatch(guild.id)
                except Exception:
                    logging.exception("[lottery] failed to clear automatch for guild %s", guild.id)

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
                await announce_record(channel, "highest_balance", winner.display_name, await get_balance(int(winner_id)), holder_id=int(winner_id), notify=True)

    async def _execute_purchase(self, guild_id: int, uid: int, tickets: int) -> dict:
        """Charge `uid` for `tickets` and add them to the guild's lottery.

        Runs inside the guild lock: save_lottery rewrites the whole snapshot,
        so an unlocked concurrent purchase would erase this buyer's tickets
        (while keeping their coins), and a save racing the 1st-of-month draw
        would resurrect the paid-out pool.

        Returns {"error": "cap"} or {"error": "funds", "cost": n} on failure,
        else the purchase details for the caller to render.
        """
        async with self._lock(guild_id):
            # Load inside the lock — any earlier snapshot may be stale.
            lottery = await load_lottery(guild_id)
            players = lottery.setdefault("players", {})
            current_tickets = int(players.get(str(uid), 0))
            if current_tickets + tickets > TICKET_CAP:
                return {"error": "cap"}

            # Claim the discount synchronously before the charge and roll it
            # back if the charge fails — the counter is per-user across
            # guilds, so a purchase in another guild's lock can interleave
            # at any await.
            user = state.economy["users"][str(uid)]
            remaining = discount_tickets_remaining(user, _ct_today())
            cost, discounted = ticket_cost(tickets, remaining)
            user["lottery_disc_used"] += discounted
            if not await deduct_balance(uid, cost):
                user["lottery_disc_used"] -= discounted
                return {"error": "funds", "cost": cost}
            # deduct_balance's save_economy persisted the discount claim.
            await record_gambling_event(guild_id, uid, lost=cost)

            was_new_player = str(uid) not in players
            players[str(uid)] = players.get(str(uid), 0) + tickets
            lottery.setdefault("prize_pool", 0)
            # A full-price ticket (10) splits 7 pool / 3 house; a half-price
            # ticket (5) splits 4 / 1.
            full = tickets - discounted
            lottery["prize_pool"] += full * 7 + discounted * 4
            if was_new_player:
                lottery["prize_pool"] += 1000
            await add_guild_house(guild_id, full * 3 + discounted)

            await save_lottery(guild_id, lottery)
            await record_tickets_purchased(tickets)

        return {
            "tickets": tickets,
            "cost": cost,
            "discounted": discounted,
            "was_new_player": was_new_player,
            "prize_pool": lottery["prize_pool"],
            "user_tickets": players[str(uid)],
            "total_tickets": sum(players.values()),
        }

    async def _process_automatch(self, guild, channel, buyer_uid: int, buyer_total: int) -> None:
        """Auto-buy tickets for automatch opt-ins the buyer just passed.

        Runs after a successful purchase, outside the guild lock — every
        auto-buy goes through _execute_purchase, which re-validates the cap
        and cost under the lock. Automatch only ever raises a user to the
        buyer's total, never past it, so one pass can't trigger another
        (and _execute_purchase never calls back into this method).
        """
        try:
            automatch = await persistence.load_lottery_automatch(guild.id)
        except Exception:
            logging.exception("[lottery] failed to load automatch opt-ins for guild %s", guild.id)
            return
        if not automatch:
            return

        lottery = await load_lottery(guild.id)
        players = lottery.get("players", {})
        lines = []
        for uid_str, max_tickets in automatch.items():
            if uid_str == str(buyer_uid):
                continue
            current = int(players.get(uid_str, 0))
            target = min(buyer_total, max_tickets, TICKET_CAP)
            needed = target - current
            if needed <= 0:
                continue
            uid = int(uid_str)
            await _ensure_user(uid)
            user = state.economy["users"][uid_str]
            remaining = discount_tickets_remaining(user, _ct_today())
            affordable = max_affordable_tickets(await get_balance(uid), remaining)
            tickets = min(needed, affordable)
            if tickets <= 0:
                lines.append(f"<@{uid}> couldn't afford to match ({needed:,} 🎟️ needed)")
                continue
            result = await self._execute_purchase(guild.id, uid, tickets)
            if result.get("error"):
                continue
            short = target - result["user_tickets"]
            suffix = f" ({short:,} short — balance ran out)" if short > 0 else ""
            lines.append(
                f"<@{uid}> auto-bought **{result['tickets']:,}** 🎟️ for "
                f"**{result['cost']:,} 🪙** — now at **{result['user_tickets']:,}**{suffix}"
            )
        if lines:
            await channel.send(embed=emb("🎯 Automatch", "\n".join(lines), C_PURPLE))

    async def _cmd_automatch(self, ctx: commands.Context, amount: "str | None") -> None:
        """`!lottery automatch [<max>|off]` — view, arm, or disarm automatch."""
        uid = ctx.author.id
        guild_id = ctx.guild.id
        automatch = await persistence.load_lottery_automatch(guild_id)
        current = automatch.get(str(uid))

        if amount is None:
            if current:
                desc = (
                    f"Automatch is **on** — when another player buys past your ticket "
                    f"total, you auto-buy 🎟️ to tie them, up to **{current:,}** 🎟️ held.\n"
                    "Lasts until this lottery is drawn. `!lottery automatch off` to disable."
                )
            else:
                desc = (
                    "Automatch is **off**.\n"
                    "`!lottery automatch <max tickets>` — whenever another player buys "
                    "past your ticket total, automatically buy 🎟️ to tie them, never "
                    "holding more than `<max tickets>`."
                )
            await ctx.send(embed=emb("🎯 Lottery Automatch", desc, C_PURPLE))
            return

        if amount.lower() in ("off", "stop", "0"):
            if current is None:
                await ctx.send(embed=emb("🎯 Lottery Automatch", "Automatch is already off.", C_GREY))
                return
            await persistence.delete_lottery_automatch(guild_id, uid)
            await ctx.send(embed=emb(
                "🎯 Automatch Disabled",
                "You'll no longer auto-buy 🎟️ to match other players.",
                C_GREY,
            ))
            return

        max_tickets = parse_int_amount(amount)
        if max_tickets is None or max_tickets <= 0:
            await ctx.send(embed=emb(
                "❌ Invalid Amount",
                "Usage: `!lottery automatch <max tickets>` or `!lottery automatch off`.",
                C_RED,
            ))
            return
        max_tickets = min(max_tickets, TICKET_CAP)
        await persistence.save_lottery_automatch(guild_id, uid, max_tickets)
        await ctx.send(embed=emb(
            "🎯 Automatch Enabled",
            f"When another player buys past your ticket total, you'll automatically "
            f"buy 🎟️ to tie them — holding up to **{max_tickets:,}** 🎟️.\n"
            "Tickets are billed from your balance at the normal (or half-price) rate, "
            "starting with the next purchase anyone makes.\n"
            "Lasts until this lottery is drawn. `!lottery automatch off` to disable.",
            C_GREEN,
        ))

    async def buy_discounted_tickets(self, member, channel, guild) -> None:
        """Buy all of `member`'s remaining half-price tickets for the day.

        Backs the dailies-channel 🎟️ reaction (src/cogs/dailies_cog.py).
        Results are posted to `channel` — the dailies channel, whose sweeper
        cleans them up after 5 minutes.
        """
        uid = member.id
        await _ensure_user(uid)

        cfg = get_guild_cfg(guild.id)
        if not cfg.get("lottery_channel"):
            await channel.send(embed=emb("🎰 Lottery Disabled", "Lottery channel not configured.", C_GREY), silent=True)
            return

        now_cst = _ct_now()
        if now_cst.day == 1 and now_cst.hour == 17:
            await channel.send(embed=emb("🔒 Lottery Locked", "Ticket sales are closed for the final hour before the draw. Check back after 6pm CT!", C_RED), silent=True)
            return

        user = state.economy["users"][str(uid)]
        remaining = discount_tickets_remaining(user, _ct_today())
        if remaining <= 0:
            await channel.send(embed=emb(
                "🎟️ No Half-Price Tickets Left",
                f"**{member.display_name}**, you've already bought all "
                f"**{DISCOUNT_DAILY_CAP:,}** of today's half-price tickets.\n"
                "`!lottery <n>` still works at full price.",
                C_GREY,
            ), silent=True)
            return

        balance = await get_balance(uid)
        tickets = min(remaining, balance // DISCOUNT_TICKET_PRICE)
        if tickets <= 0:
            await channel.send(embed=emb(
                "💸 Insufficient Funds",
                f"A half-price ticket costs **{DISCOUNT_TICKET_PRICE} 🪙** — you have **{balance:,} 🪙**.",
                C_RED,
            ), silent=True)
            return

        result = await self._execute_purchase(guild.id, uid, tickets)
        if result.get("error") == "cap":
            await channel.send(embed=emb(
                "🎟️ Ticket Cap Reached",
                f"Each player can hold at most **{TICKET_CAP:,}** 🎟️ per lottery.",
                C_RED,
            ), silent=True)
            return
        if result.get("error") == "funds":
            await channel.send(embed=emb(
                "💸 Insufficient Funds",
                f"Need {result['cost']:,} 🪙. Balance: {await get_balance(uid):,} 🪙",
                C_RED,
            ), silent=True)
            return

        bonus_msg = "(+1,000 bonus as new player)" if result["was_new_player"] else ""
        timestamp = int(next_lottery_draw_dt(now_cst).timestamp())
        await channel.send(embed=emb(
            "🎰 Tickets Purchased",
            f"**{member.display_name}** bought **{result['tickets']:,}** 🎟️ "
            f"for **{result['cost']:,} 🪙** (all half price)\n\n"
            f"**Prize Pool:** {result['prize_pool']:,} 🪙 {bonus_msg}\n"
            f"**Your Tickets:** {result['user_tickets']:,} / {result['total_tickets']:,} total\n"
            f"**Ends:** <t:{timestamp}:R>",
            C_GREEN,
        ), silent=True)

        await self._process_automatch(guild, channel, uid, result["user_tickets"])

    @commands.command(name="lottery")
    async def cmd_lottery(self, ctx: commands.Context, n: str = None, amount: str = None):
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

        if n is not None and n.lower() == "automatch":
            await self._cmd_automatch(ctx, amount)
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
            remaining_disc = discount_tickets_remaining(
                state.economy["users"][str(uid)], _ct_today()
            )
            info = f"**Prize Pool:** {pool:,} 🪙 (+1,000 🪙 per player)\n"
            info += f"**Players:** {len(players_dict)}\n"
            info += f"**Ticket Cost:** {TICKET_PRICE} 🪙 for 1 🎟️ — your first "
            info += f"{DISCOUNT_DAILY_CAP:,} each day cost {DISCOUNT_TICKET_PRICE} 🪙 "
            info += f"(**{remaining_disc:,}** left today)\n\n"
            info += f"**Your Tickets:** {user_tickets:,} / {total_tickets:,} total\n"
            info += "Use `!lottery <n>` to buy more tickets"
            try:
                my_automatch = (await persistence.load_lottery_automatch(ctx.guild.id)).get(str(uid))
            except Exception:
                my_automatch = None
            if my_automatch:
                info += f"\n**Automatch:** on — auto-buys to tie other buyers, up to {my_automatch:,} 🎟️"
            else:
                info += "\nUse `!lottery automatch <max>` to auto-match other buyers"

            await ctx.send(embed=emb(f"🎰 Current Lottery • ends <t:{timestamp}:R>", info, C_PURPLE))
            return

        # Block purchases in the 1-hour window before the draw (5-6pm CT on the 1st)
        if now_cst.day == 1 and now_cst.hour == 17:
            await ctx.send(embed=emb("🔒 Lottery Locked", "Ticket sales are closed for the final hour before the draw. Check back after 6pm CT!", C_RED))
            return

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
            # Cost estimate for the confirm dialog; the authoritative cost is
            # recomputed from the live discount counter inside the lock.
            cost, _ = ticket_cost(
                tickets,
                discount_tickets_remaining(state.economy["users"][str(uid)], _ct_today()),
            )
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

        result = await self._execute_purchase(ctx.guild.id, uid, tickets)
        if result.get("error") == "cap":
            await ctx.send(embed=emb(
                "🎟️ Ticket Cap Reached",
                f"Each player can hold at most **{TICKET_CAP:,}** 🎟️ per lottery.",
                C_RED,
            ))
            return
        if result.get("error") == "funds":
            await ctx.send(embed=emb("💸 Insufficient Funds", f"Need {result['cost']:,} 🪙. Balance: {await get_balance(uid):,} 🪙", C_RED))
            return

        bonus_msg = "(+1,000 bonus as new player)" if result["was_new_player"] else ""
        half_msg = f" ({result['discounted']:,} at half price)" if result["discounted"] else ""

        # Calculate when lottery ends
        timestamp = int(next_lottery_draw_dt(now_cst).timestamp())

        embed_msg = emb(
            "🎰 Tickets Purchased",
            f"**{ctx.author.display_name}** bought **{tickets:,}** 🎟️ for **{result['cost']:,} 🪙**{half_msg}\n\n"
            f"**Prize Pool:** {result['prize_pool']:,} 🪙 {bonus_msg}\n"
            f"**Your Tickets:** {result['user_tickets']:,} / {result['total_tickets']:,} total\n"
            f"**Ends:** <t:{timestamp}:R>",
            C_GREEN
        )
        await ctx.send(embed=embed_msg)

        await self._process_automatch(ctx.guild, ctx.channel, uid, result["user_tickets"])



async def setup(bot):
    await bot.add_cog(LotteryCog(bot))

