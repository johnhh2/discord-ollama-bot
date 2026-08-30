import asyncio
import hashlib
import logging
import random
import datetime

import discord
from discord.ext import commands, tasks

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_PURPLE, C_GREY,
    toggle_member_role, announce_record,
)
from src.economy import (
    add_balance, _ct_now, _ct_today, do_daily_reset, _ensure_user, record_gambling_event,
)
from src.permissions import (
    check_game_channel,
)
from src.leveling import grant_xp
from src.persistence import (
    save_economy, save_gambler_streak,
    save_rigged_scratch, try_set_record
)
from src.dailies import keep_in_dailies_channel
from src.guild_config import get_guild_cfg
from src.config import (
    SCRATCH_SYMBOLS, SCRATCHOFF_MAX_DAILY,
)
from src.artifacts import scratchoff_daily_cap
from src import state, status_manager


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


GAMBLER_ROLE_STREAK_REQUIRED = 3


def scratchoff_attempts_remaining(user: dict, today: str, cap: int = SCRATCHOFF_MAX_DAILY) -> int:
    """Return how many scratchoff attempts the user has left today.

    Mutates `user` to roll the daily counters to `today` if the stored
    `scratch_date` is stale (or missing) — zeroing both `scratch_used` and
    the day's `scratch_won_today` total — and normalizes `scratch_used`
    to an int so the caller can safely do `user["scratch_used"] += 1`.
    Mirrors the pre-condition logic at the top of cmd_scratchoff so the
    rollover + cap behavior can be tested in isolation. Pass the user's
    artifact-adjusted cap (scratchoff_daily_cap) for the real limit.
    """
    if user.get("scratch_date") != today:
        user["scratch_date"] = today
        user["scratch_used"] = 0
        user["scratch_won_today"] = 0
    elif "scratch_used" not in user:
        user["scratch_used"] = 0
    return max(0, cap - user["scratch_used"])


def _get_streak_entry(uid_key: str) -> dict:
    """Return the streak entry as a dict {date, count}, normalizing legacy str entries."""
    entry = state.gambler_streak.get(uid_key)
    if entry is None:
        return {"date": None, "count": 0}
    if isinstance(entry, dict):
        return {"date": entry.get("date"), "count": int(entry.get("count", 1))}
    return {"date": entry, "count": 1}


async def update_gambler_streak(uid: int, today_ct: str) -> int:
    """Bump the user's full-day scratchoff streak. Returns the new streak count."""
    uid_key = str(uid)
    yesterday = (datetime.date.fromisoformat(today_ct) - datetime.timedelta(days=1)).isoformat()
    entry = _get_streak_entry(uid_key)

    if entry["date"] == today_ct:
        return entry["count"]
    if entry["date"] == yesterday:
        new_count = entry["count"] + 1
    else:
        new_count = 1

    state.gambler_streak[uid_key] = {"date": today_ct, "count": new_count}
    await save_gambler_streak()
    return new_count


async def maybe_assign_gambler_role(guild: discord.Guild, member: discord.Member, channel: discord.abc.Messageable, streak_count: int):
    """Assign the Gamblers role if the user has hit the required streak. Does not remove on loss."""
    cfg = get_guild_cfg(guild.id)
    if not cfg.get("gambler_role_enabled", False):
        return
    if streak_count < GAMBLER_ROLE_STREAK_REQUIRED:
        return

    role = await get_or_create_gamblers_role(guild)
    if role and role not in member.roles:
        if await toggle_member_role(member, role, True, reason=f"Used all 3 scratchoffs {GAMBLER_ROLE_STREAK_REQUIRED} days in a row"):
            # The member just scratched their third card in this channel, so
            # they're already watching — mention for the highlight, no push.
            await channel.send(
                f"🎲 {member.mention} You've been automatically added to the **Gamblers** role for using all 3 scratchoffs **{GAMBLER_ROLE_STREAK_REQUIRED} days in a row**! "
                f"You'll be pinged whenever a slots jackpot or lottery is won. "
                f"Use `!gambler-role off` to opt out.",
                silent=True,
            )


# The presence line stays hidden until the server has scratched more than
# this many cards in the current gameplay-day.
SCRATCHOFF_STATUS_THRESHOLD = 3


def scratchoffs_today() -> int:
    """Total scratchoff cards played across all users this gameplay-day.

    Derived from the per-user scratch_used/scratch_date fields, so it rolls
    over at the normal 5am CT reset with no extra bookkeeping: entries whose
    scratch_date is stale simply stop counting.
    """
    today = _ct_today()
    users = state.economy.get("users") or {}
    return sum(
        int(u.get("scratch_used") or 0)
        for u in users.values()
        if u.get("scratch_date") == today
    )


def scratchoff_status_text() -> "str | None":
    """Presence line for the status manager; None hides it."""
    total = scratchoffs_today()
    if total <= SCRATCHOFF_STATUS_THRESHOLD:
        return None
    return f"{total}x 🎫 scratched today"


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



async def play_scratchoffs(bot, author, channel, guild, count: int = 1) -> int:
    """Play up to `count` of the user's remaining daily scratchoffs.

    Extracted from cmd_scratchoff so the dailies-channel reaction handler can
    run a player's scratchoffs without a commands.Context. `author` must be a
    discord.Member for XP level-up announcements and the Gamblers role grant;
    results are sent to `channel`. Returns the total coins won across the
    cards played (0 if the daily cap was already spent) — the dailies claim
    uses this as the stake for its flip/slots gamble reactions.
    """
    uid = author.id
    await _ensure_user(uid)

    cap = scratchoff_daily_cap(uid)
    count = max(1, min(cap, count))

    today = _ct_today()
    user = state.economy["users"][str(uid)]
    remaining = scratchoff_attempts_remaining(user, today, cap)
    if remaining <= 0:
        await save_economy(uid=uid)
        await channel.send(embed=emb("🎰 Daily Limit", f"**{author.display_name}** has used all **{cap}** daily scratchoffs.\nCome back tomorrow!", C_GOLD), silent=True)
        return 0

    count = min(count, remaining)

    # Reserve attempts up front (sync) so concurrent invocations see the
    # updated counter before they pass the remaining > 0 gate. Without
    # this, a user spamming !scratchoff can cross the await boundaries
    # below and run more than 3 cards in a single day.
    first_attempt = user["scratch_used"]
    user["scratch_used"] += count

    # Generate daily goal seeded by date (same for everyone). hashlib, not
    # hash() — str hashing is randomized per process (PYTHONHASHSEED), so
    # a mid-day redeploy would silently change everyone's goal.
    seed_val = int(hashlib.sha256(today.encode()).hexdigest(), 16) % (2**31)
    random.seed(seed_val)
    goal = random.choices(SCRATCH_SYMBOLS, k=4)
    random.seed()

    goal_str = " ".join(goal)

    show_hint = not user.get("scratchoff_seen_rewards", False)
    if show_hint:
        user["scratchoff_seen_rewards"] = True

    total_won = 0
    for i in range(count):
        attempt_idx = first_attempt + i
        rig_matches = None
        if uid in state.rigged_scratch:
            # Fire on a uniformly random one of the user's remaining cards
            # today: P = 1/(cards left including this one), so the last
            # card of the day is guaranteed if it hasn't fired earlier.
            cards_left = cap - attempt_idx
            if cards_left <= 1 or random.random() < 1.0 / cards_left:
                rig_matches = state.rigged_scratch[uid]

        if rig_matches is not None:
            # Build a card with exactly rig_matches positions matching the goal
            positions = list(range(4))
            random.shuffle(positions)
            match_positions = set(positions[:rig_matches])
            card = []
            for pos in range(4):
                if pos in match_positions:
                    card.append(goal[pos])
                else:
                    # Pick a symbol that doesn't match the goal at this position
                    non_matches = [s for s in SCRATCH_SYMBOLS if s != goal[pos]]
                    card.append(random.choice(non_matches) if non_matches else random.choice(SCRATCH_SYMBOLS))
            del state.rigged_scratch[uid]
            await save_rigged_scratch()
        else:
            card = random.choices(SCRATCH_SYMBOLS, k=4)

        matches = sum(c == g for c, g in zip(card, goal))

        payout = 0
        match_text = ""
        if matches == 0:
            match_text = "❌ No matches."
        elif matches == 1:
            payout = 100
            match_text = f"⭐ 1 Match! **{author.display_name}** won 100 🪙!"
        elif matches == 2:
            payout = 1000
            match_text = f"🎉 2 Matches! **{author.display_name}** won 1,000 🪙!"
        elif matches == 3:
            payout = 10000
            match_text = f"🏆 3 Matches! **{author.display_name}** won 10,000 🪙!"
        elif matches == 4:
            payout = 100000
            match_text = f"💎 4 Matches! **{author.display_name}** won 100,000 🪙!"

        await add_balance(uid, payout)
        # Running total for the "best scratchoff day" record. Bumped per card
        # (not once per batch) so it lands in the same save_economy write as
        # the payout and a concurrent invocation sees it immediately.
        user["scratch_won_today"] = int(user.get("scratch_won_today", 0) or 0) + payout
        if payout > 0:
            await record_gambling_event(guild.id if guild else None, uid, gained=payout)
        await save_economy(uid=uid)

        # Award 10 XP per scratchoff played
        if guild:
            _, leveled_up = await grant_xp(uid, "scratch", guild_id=guild.id)
            # _announce_levelup grants the coin reward and skips the
            # announcement itself when no channel is configured.
            if leveled_up:
                cog = bot.cogs.get("LevelingCog")
                if cog and isinstance(author, discord.Member):
                    asyncio.create_task(cog._announce_levelup(author, guild.id))

        card_str = " ".join(card)
        attempts_left = cap - (attempt_idx + 1)

        embed = discord.Embed(title="🎫 Scratchoff", color=C_GREEN if payout > 0 else C_RED)
        embed.description = f"Daily Goal: {goal_str}\nYour Card:  {card_str}\n\n{match_text}\n\nAttempts left: {attempts_left}/{cap}"

        if show_hint:
            embed.add_field(name="📊 Payout Info", value="Use `!scratchoffrewards` to see all payouts!", inline=False)
            show_hint = False

        total_won += payout
        msg = await channel.send(embed=embed, silent=True)

        # Big wins posted in the guild's dailies channel are pinned until the
        # 5am reset (the reset repost purges the channel and clears the list).
        await keep_in_dailies_channel(guild, channel, msg, payout)

        # Track full-day scratchoff streak for Gamblers role.
        # Done after the card embed so the role-grant announcement
        # appears after the third scratch, not between cards.
        if (attempt_idx + 1) >= 3 and guild:
            new_streak = await update_gambler_streak(uid, today)
            await maybe_assign_gambler_role(guild, author, channel, new_streak)

    # "Best scratchoff day" record: the combined payout of every card the user
    # has scratched this gameplay-day. Attempted once per batch rather than per
    # card so !scratches / the dailies 🎟️ button announce a single embed for
    # the whole run, and so three separate !scratchoff calls still add up to
    # the same number the batch path would produce.
    day_total = int(user.get("scratch_won_today", 0) or 0)
    if guild is not None and day_total > 0:
        if await try_set_record(guild.id, "scratchoff_day", day_total, uid, author.display_name):
            await announce_record(channel, "scratchoff_day", author.display_name, day_total, holder_id=uid)

    return total_won


class ScratchoffCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        status_manager.register("scratchoffs", scratchoff_status_text)
        self._daily_reset_task.start()

    def cog_unload(self):
        status_manager.unregister("scratchoffs")
        self._daily_reset_task.cancel()

    @tasks.loop(minutes=1)
    async def _daily_reset_task(self):
        """Reset daily counters at 5am CT. Guarded — a failed tick must not
        stop the loop for good."""
        try:
            now_ct = _ct_now()
            if now_ct.hour != 5 or now_ct.minute != 0:
                return
            today = now_ct.date().isoformat()
            if state.economy.get("last_daily_reset") != today:
                await do_daily_reset()
        except Exception:
            logging.exception("[scratchoff] daily reset tick failed")

    @_daily_reset_task.before_loop
    async def _before_daily_reset_task(self):
        # State must be loaded before the reset can compare/stamp dates.
        await self.bot.wait_until_ready()
        from src.persistence import init_done
        await init_done.wait()

    @commands.command(name="scratchoff", aliases=["scratch"])
    async def cmd_scratchoff(self, ctx: commands.Context, count: int = 1):
        if await check_game_channel(ctx, "Gambling"):
            return
        await play_scratchoffs(ctx.bot, ctx.author, ctx.channel, ctx.guild, count)

    @commands.command(name="scratches", aliases=["scratchoffs"])
    async def cmd_scratches(self, ctx: commands.Context):
        await ctx.invoke(self.cmd_scratchoff, count=scratchoff_daily_cap(ctx.author.id))

    @commands.command(name="streak")
    async def cmd_streak(self, ctx: commands.Context):
        uid = ctx.author.id
        await _ensure_user(uid)

        today_ct = _ct_today()
        yesterday = (datetime.date.fromisoformat(today_ct) - datetime.timedelta(days=1)).isoformat()

        user = state.economy["users"][str(uid)]
        scratch_used = user.get("scratch_used", 0) if user.get("scratch_date") == today_ct else 0
        cap = scratchoff_daily_cap(uid)
        entry = _get_streak_entry(str(uid))
        last_full_day = entry["date"]
        count = entry["count"]

        # Effective current streak: today/yesterday keep it alive; otherwise it's broken
        if last_full_day == today_ct:
            effective = count
            streak_text = f"🔥 **{effective}-day streak** — you filled all 3 today!"
            color = C_GREEN
        elif last_full_day == yesterday:
            effective = count
            streak_text = f"⏳ **{effective}-day streak** — fill all 3 today to extend it! ({scratch_used}/{cap} used today)"
            color = C_GOLD
        elif last_full_day:
            effective = 0
            streak_text = f"❌ **Streak broken** — last full day was `{last_full_day}` ({count}-day streak). Use all 3 today to start a new streak! ({scratch_used}/{cap} used today)"
            color = C_RED
        else:
            effective = 0
            streak_text = f"❌ **No streak yet** — use all 3 scratchoffs in a day to start one! ({scratch_used}/{cap} used today)"
            color = C_GREY

        cfg = get_guild_cfg(ctx.guild.id) if ctx.guild else {}
        role_enabled = cfg.get("gambler_role_enabled", False)
        role_line = ""
        if role_enabled:
            if effective >= GAMBLER_ROLE_STREAK_REQUIRED:
                role_line = "\n\n🎲 You've earned the **Gamblers** role!"
            else:
                role_line = f"\n\nFill all 3 **{GAMBLER_ROLE_STREAK_REQUIRED} days in a row** to auto-join the **Gamblers** role."

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


async def setup(bot):
    await bot.add_cog(ScratchoffCog(bot))


