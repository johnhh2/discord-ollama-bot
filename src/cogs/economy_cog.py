import asyncio
import logging
import random
import time
import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_ORANGE, C_GREY, C_BLUE, parse_amount, parse_int_amount, send_ephemeral, fetch_member, shop_charge, OptionalMember,
    announce_record,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, get_guild_house_balance,
    add_guild_house, is_insured, get_insurance_expiry, sweep_insurance_subs, _ct_now, _ct_today, do_daily_reset, _ensure_user,
    next_daily_reset_ts, get_savings_value, add_savings, remove_savings,
    savings_growth, SAVINGS_DAILY_PCT,
    seize_from_savings, record_crime_event, CRIME_ELIGIBLE_NET_WORTH,
    _maybe_latch_crime_eligible,
)
from src.permissions import (
    requires_perm,
    is_silenced,
)
from src.guild_config import get_guild_cfg
from src.persistence import (
    save_economy, save_rigged_steal,
    load_lottery, load_records, load_global_records, try_set_record,
)
from src.config import (
    DAILY_REWARD, DAILY_RESET_HOUR,
)
from src.jail_reasons import format_steal_reason, format_mug_reason, format_bankheist_reason
from src.artifacts import bail_cost, steal_success_chance, crime_catch_chance
from src.properties import bank_property_revenue
from src.confirm_view import confirm_purchase
from src import state


# (escape_chance, steal_pct, jail_chance, fine, jail_days)
# Bail cost on jail = 10,000 + steal_amount/2, so higher steal_pct also raises bail.
STEAL_TIERS = [
    (0.10, 0.10, 0.25, 1000, 1),
    (0.09, 0.15, 0.30, 1350, 1),
    (0.08, 0.20, 0.35, 3250, 1),
]


# ── Shared crime-score record ─────────────────────────────────────────────────
# !steal, !mug and !bankheist all compete for ONE record rather than three:
# the biggest single haul taken off another player, whichever crime produced
# it. The crime that set it rides along in extra_json as `crime_type` so
# !records and the record-broken announcement can name it.
CRIME_RECORD_CATEGORY = "crime"

CRIME_TYPE_LABELS = {
    "steal": "Steal",
    "mug": "Mug",
    "bankheist": "Bank Heist",
}


def format_crime_record_detail(rec: dict) -> str:
    """Render the 'which crime, on whom, split how' line for a crime record.

    Shared by the !records entry and the announce_record detail line so the
    two can't drift. `rec` is a record dict (or the meta kwargs about to be
    stored) — everything past `crime_type` is optional, so an older row
    written before a field existed still renders.

    A bankheist row carries `crew`: every participant and their cut. Cuts are
    equal except for the integer-division remainder the host pockets, so the
    common case collapses to a single "N 🪙 each"; when the remainder makes
    them differ, each cut is spelled out instead.
    """
    parts = [CRIME_TYPE_LABELS.get(rec.get("crime_type"), "Crime")]

    victim = rec.get("victim")
    if victim:
        parts.append(f"robbed {victim}")

    crew = rec.get("crew") or []
    if crew:
        cuts = [int(c.get("cut", 0)) for c in crew]
        names = [str(c.get("name", "?")) for c in crew]
        if len(set(cuts)) == 1:
            parts.append(f"crew: {', '.join(names)} — {cuts[0]:,} 🪙 each")
        else:
            parts.append(
                "crew: " + ", ".join(f"{n} {c:,} 🪙" for n, c in zip(names, cuts))
            )

    return " • ".join(parts)


async def try_set_crime_record(
    channel, guild, amount: int, holder_id: int, holder_name: str,
    crime_type: str, victim_name: str, **meta,
) -> bool:
    """Offer `amount` to the shared crime-score record, announcing on a break.

    `holder_name` is the display string the record is attributed to — a single
    thief for !steal / !mug, the whole crew for a !bankheist (the user-visible
    holder of a group score is the group). `holder_id` stays a real user id
    either way so the tie-break and any future per-user lookup still work.

    Returns whether the record was taken. Crimes that took nothing (`amount`
    <= 0) and crimes outside a guild never compete.
    """
    if guild is None or amount <= 0:
        return False
    meta = {"crime_type": crime_type, "victim": victim_name, **meta}
    if not await try_set_record(
        guild.id, CRIME_RECORD_CATEGORY, amount, holder_id, holder_name, **meta,
    ):
        return False
    await announce_record(
        channel, CRIME_RECORD_CATEGORY, holder_name, amount,
        detail=format_crime_record_detail(meta), holder_id=holder_id,
    )
    return True


def _is_public_channel(channel) -> bool:
    """True if @everyone can view this channel (i.e. it's a public channel).

    A channel is considered private when the guild's default role (@everyone)
    is denied `view_channel` via a permission overwrite. Crimes (steal / mug /
    bankheist) are blocked in private channels so they can only target people
    in the open. DMs / non-guild contexts return False (not public)."""
    guild = getattr(channel, "guild", None)
    if guild is None:
        return False
    default_role = getattr(guild, "default_role", None)
    perms_for = getattr(channel, "permissions_for", None)
    if default_role is None or perms_for is None:
        # Channel doesn't expose permission introspection (e.g. an exotic
        # channel type) — fail open and treat it as public.
        return True
    return bool(perms_for(default_role).view_channel)


def _crime_public_only_error(ctx) -> discord.Embed | None:
    """Return an error embed if this crime can't run in the current channel
    because it's private, else None. Crimes are public-channel only."""
    if not _is_public_channel(ctx.channel):
        return emb(
            "🔒 Private Channel",
            "Crimes can only be committed out in the open — try this in a public channel.",
            C_RED,
        )
    return None


def _jail_body(name: str, jail_until_ts: float, reason: str | None) -> str:
    """Render the standard 'in jail' embed body. Omits the Reason line
    when reason is None/empty (legacy jails written before jail_reason existed)."""
    body = f"**{name}** is locked up! Released <t:{int(jail_until_ts)}:R>."
    if reason:
        body += f"\nReason: {reason}"
    return body


class _StealTierView(discord.ui.View):
    """3-button tier picker shown when `!steal @user` runs without a tier arg.
    Only the original invoker can click; on click we drop the buttons entirely
    and delegate to `cog._run_steal` so the heist follows the same code path
    as `!steal @user N`."""

    def __init__(self, cog, ctx, target, victim_bal: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.target = target
        self.message: discord.Message | None = None
        self._fired = False

        styles = [discord.ButtonStyle.success, discord.ButtonStyle.primary, discord.ButtonStyle.danger]
        for i, (_escape, pct, _jail, _fine, _days) in enumerate(STEAL_TIERS):
            amount = max(1, int(victim_bal * pct))
            label = f"{amount:,} 🪙"
            self.add_item(_StealTierButton(label=label, tier=i + 1, style=styles[i]))

    async def on_timeout(self):
        if self._fired or self.message is None:
            return
        for c in self.children:
            c.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


class _StealTierButton(discord.ui.Button):
    def __init__(self, *, label: str, tier: int, style: discord.ButtonStyle):
        super().__init__(label=label, style=style)
        self.tier = tier

    async def callback(self, interaction: discord.Interaction):
        view: _StealTierView = self.view  # type: ignore[assignment]
        if interaction.user.id != view.ctx.author.id:
            await interaction.response.send_message("Not your robbery.", ephemeral=True)
            return
        view._fired = True
        await interaction.response.edit_message(view=None)
        view.stop()
        await view.cog._run_steal(view.ctx, view.target, self.tier)



class EconomyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # uids currently mid-crime (steal/mug). In-flight per-cog state, never persisted.
        self._crime_active: set[int] = set()
        # channel_id → heist_state. One bankheist lobby per channel at a time.
        self._active_heists: dict[int, dict] = {}

    async def cog_load(self):
        # Started here (not __init__) so tests constructing the cog directly
        # don't spawn the background loop — only bot.add_cog does.
        self._insurance_sweep_task.start()

    def cog_unload(self):
        self._insurance_sweep_task.cancel()

    @tasks.loop(minutes=1)
    async def _insurance_sweep_task(self):
        """Charge insurance subscribers for the new gameplay-day at the first
        tick after the 5am CT rollover — whether or not they log on. Guarded:
        a failed tick must not stop the loop for good."""
        try:
            await sweep_insurance_subs()
        except Exception:
            logging.exception("[insurance] sweep tick failed")

    @_insurance_sweep_task.before_loop
    async def _before_insurance_sweep(self):
        await self.bot.wait_until_ready()
        import src.persistence as _pkg
        await _pkg.init_done.wait()

    @commands.group(name="daily", invoke_without_command=True)
    async def cmd_daily(self, ctx: commands.Context):
        uid = ctx.author.id
        await _ensure_user(uid)
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
        # Claim the day BEFORE awaiting add_balance — otherwise a user spamming
        # !daily can pass the check above twice before either invocation
        # increments the counter, double-collecting the reward.
        user_data["daily_date"] = today
        user_data["last_daily"] = time.time()
        # Premiums charged by the 5am sweep since the user's last claim —
        # read + reset inside the same synchronous claim window.
        ins_paid = int(user_data.get("ins_paid_since_claim", 0) or 0)
        ins_lapsed = int(user_data.get("ins_lapsed_since_claim", 0) or 0)
        user_data["ins_paid_since_claim"] = 0
        user_data["ins_lapsed_since_claim"] = 0
        gid = ctx.guild.id if ctx.guild else None
        # Property revenue rides the daily claim (banks + stamps atomically;
        # see src/properties.py). Banked BEFORE the reward's add_balance so
        # the highest_balance record offer below sees the full new balance.
        prop_rev = await bank_property_revenue(uid)
        await add_balance(uid, DAILY_REWARD, guild_id=gid, holder_name=ctx.author.display_name)
        await save_economy(uid=uid)
        prop_str = f" + **{prop_rev:,} 🪙** property revenue" if prop_rev else ""
        ins_str = f"\n🛡️ Insurance paid since your last claim: **{ins_paid:,} 🪙**" if ins_paid else ""
        lapse_str = (
            f"\n⚠️ {ins_lapsed} insurance renewal{'s' if ins_lapsed != 1 else ''} couldn't be paid — "
            "coverage lapsed those days." if ins_lapsed else ""
        )
        prop_note = ""
        if prop_rev:
            prop_note = (
                "\n*Property revenue joins your dailies 🪙/🎰/🏇 stake — `!daily property` to leave it out.*"
                if user_data.get("daily_gamble_property", False)
                else "\n*Property revenue isn't part of the dailies 🪙/🎰/🏇 stake — `!daily property` to include it.*"
            )
        await ctx.send(embed=emb("🪙 Daily Reward", f"**{ctx.author.display_name}** claimed **+{DAILY_REWARD:,} 🪙**{prop_str}! Balance: **{await get_balance(uid):,} 🪙**{ins_str}{lapse_str}{prop_note}", C_GREEN))

    @cmd_daily.command(name="property")
    async def cmd_daily_property(self, ctx: commands.Context):
        """Toggle whether property revenue joins the dailies 🪙/🎰/🏇 stake.
        Off by default: revenue always banks with the claim either way."""
        uid = ctx.author.id
        await _ensure_user(uid)
        user_data = state.economy["users"][str(uid)]
        enabled = not user_data.get("daily_gamble_property", False)
        user_data["daily_gamble_property"] = enabled
        await save_economy(uid=uid)
        if enabled:
            body = (
                "Property revenue is now **included** in your dailies 🪙/🎰/🏇 stake — "
                "reacting gambles the daily reward + property revenue + scratchoff winnings. "
                "Run `!daily property` again to leave it out."
            )
        else:
            body = (
                "Property revenue is now **left out** of your dailies 🪙/🎰/🏇 stake (the default) — "
                "it still banks with your claim, but only the daily reward + scratchoff winnings "
                "are gambled. Run `!daily property` again to include it."
            )
        await ctx.send(embed=emb("🏠 Dailies Stake", body, C_GREEN))


    @commands.command(name="balance", aliases=["bal", "b", "!", "$"])
    async def cmd_balance(self, ctx: commands.Context, target: OptionalMember = None):
        target = target or ctx.author
        if self.bot.user and target.id == self.bot.user.id and ctx.guild:
            bal = get_guild_house_balance(ctx.guild.id)
            await ctx.send(embed=emb("🏦 House Pot", f"**{ctx.guild.name}**: {bal:,} 🪙", C_GOLD))
        else:
            bal = await get_balance(target.id)
            await ctx.send(embed=emb("💰 Balance", f"**{target.display_name}**: {bal:,} 🪙", C_GREEN))


    @commands.command(name="leaderboard", aliases=["leaderboards", "lb"])
    async def cmd_leaderboard(self, ctx: commands.Context, scope: str = None):
        if ctx.guild is None:
            await ctx.send("Leaderboard is only available in servers.")
            return

        cfg = get_guild_cfg(ctx.guild.id)
        default_scope = cfg.get("leaderboard_default_scope", "global")
        if scope is not None and scope.lower() in ("server", "global"):
            scope = scope.lower()
        else:
            scope = default_scope
        server_only = scope == "server"

        lottery = await load_lottery(ctx.guild.id)
        lottery_players = lottery.get("players", {})
        ranked = sorted(
            ((k, v) for k, v in state.economy["users"].items() if v["balance"] > 0 or k in lottery_players),
            key=lambda x: x[1]["balance"], reverse=True
        )
        # Server scope filters the global economy down to members of this guild.
        # The member cache is only populated for users who've interacted (the
        # bot runs without the privileged members intent), so this reflects
        # active members rather than the full roster.
        if server_only:
            ranked = [(k, v) for k, v in ranked if ctx.guild.get_member(int(k)) is not None]
        sorted_users = ranked[:10]

        title = "🪙 Server Leaderboard" if server_only else "🪙 Leaderboard"
        if not sorted_users:
            empty_msg = "No members on the leaderboard yet." if server_only else "No users yet."
            await ctx.send(embed=emb(title, empty_msg, C_GREEN))
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
            ticket_str = f" • {tickets:,} 🎟️" if tickets else ""
            lines.append(f"{prefix} **{name}** — {data['balance']:,} 🪙{ticket_str}")
        other_scope = "global" if server_only else "server"
        lines.append(f"\n*Scope: **{scope}** · try `!lb {other_scope}` · `!levels` XP · `!lbr` roles*")
        await ctx.send(embed=emb(title, "\n".join(lines), C_GREEN))


    # ── !crime ────────────────────────────────────────────────────────────────
    @commands.group(name="crime", invoke_without_command=True)
    @requires_perm
    async def cmd_crime(self, ctx: commands.Context):
        uid = ctx.author.id
        await _ensure_user(uid)
        jail_until = state.economy["users"][str(uid)].get("jail_until", 0)
        if time.time() < jail_until:
            jail_status = f"🚔 **You are in jail!** Released <t:{int(jail_until)}:R>."
        else:
            jail_status = "✅ **You are not in jail.**"
        from src.level_unlocks import lock_marker
        gid = ctx.guild.id if ctx.guild else 0
        steal_lock = lock_marker("steal", uid, gid)
        mug_lock   = lock_marker("mug",   uid, gid)
        bankheist_lock = lock_marker("bankheist", uid, gid)
        lines = [
            f"**`!steal @user`**{steal_lock} — Pick a pocket. Chance to steal a % of their balance; risk jail if caught.",
            "",
            f"**`!mug @user <amount>`**{mug_lock} — Pay muggers `<amount>` 🪙 to take that amount from a target. The muggers keep it. 50% chance you get jailed 1 day.",
            "",
            f"**`!bankheist @user`**{bankheist_lock} — Open a 4-slot lobby; rally a crew to split a cut of the target's savings.",
            "",
            "**`!jailbreak`** — Attempt to escape jail (20% success). One attempt per day.",
            "",
            jail_status,
        ]
        await send_ephemeral(ctx, embed=emb("🦹 Crime", "\n".join(lines), C_GOLD))

    @commands.command(name="steal")
    async def cmd_steal(self, ctx: commands.Context, target: OptionalMember = None):
        if target is None:
            await ctx.invoke(self.cmd_crime)
            return

        if (err := _crime_public_only_error(ctx)) is not None:
            await ctx.send(embed=err)
            return

        # Parse tier from the rest of the message; if not present, show picker.
        args = ctx.message.content.split()
        tier_str = args[-1] if len(args) >= 3 else None
        if tier_str and tier_str.isdigit() and 1 <= int(tier_str) <= 3:
            await self._run_steal(ctx, target, int(tier_str))
            return

        await self._send_steal_picker(ctx, target)

    async def _steal_preflight(self, ctx: commands.Context, target) -> discord.Embed | None:
        """Tier-independent gating shared by `_run_steal` and the tier picker.
        Returns an error embed to show the user, or None if the steal can proceed.

        Sending happens at the call site so the picker can decide whether to
        attach buttons; centralizing the message text isn't worth the indirection."""
        thief_id = ctx.author.id
        victim_id = target.id

        if victim_id == thief_id:
            return emb("🦹 Steal", "You can't steal from yourself.", C_RED)
        if self.bot.user and victim_id == self.bot.user.id:
            return emb("🦹 Steal", "You can't steal from the house.", C_RED)

        await _ensure_user(thief_id)
        await _ensure_user(victim_id)
        await _maybe_latch_crime_eligible(victim_id)

        if not state.economy["users"][str(victim_id)].get("crime_eligible"):
            return emb(
                "🛡️ Off-Limits",
                f"**{target.display_name}** isn't in the crime system yet — they're below Level 10 and have never held more than {CRIME_ELIGIBLE_NET_WORTH:,} 🪙 across wallet + savings.",
                C_GOLD,
            )

        thief_data = state.economy["users"][str(thief_id)]
        jail_until = thief_data.get("jail_until", 0)
        if time.time() < jail_until:
            return emb(
                "🚔 You're in Jail",
                _jail_body(ctx.author.display_name, jail_until, thief_data.get("jail_reason")),
                C_RED,
            )

        if thief_id in self._crime_active:
            return emb("⏳ Already Running", "You already have a crime in progress — wait for it to finish.", C_RED)

        if await is_insured(victim_id, "steal"):
            _exp = get_insurance_expiry(victim_id)
            return emb("🛡️ Protected", f"**{target.display_name}** has insurance — you can't rob them! (expires <t:{_exp}:R>)", C_GOLD)

        return None

    async def _send_steal_picker(self, ctx: commands.Context, target):
        """Show 3 buttons (10/15/25%) so the user can pick a tier interactively."""
        # Run the same gating as `_run_steal` so we don't show buttons for an
        # impossible heist. Special-case self-target so the test that asserts a
        # plain `ctx.send(...)` string still passes.
        if target.id == ctx.author.id:
            await ctx.send("You can't steal from yourself.")
            return
        gate = await self._steal_preflight(ctx, target)
        if gate is not None:
            await ctx.send(embed=gate)
            return

        victim_bal = await get_balance(target.id)
        rows = []
        for i, (escape, _pct, jail, fine, _days) in enumerate(STEAL_TIERS, start=1):
            # Show the thief's artifact-adjusted odds, not the base tier odds.
            esc = steal_success_chance(ctx.author.id, escape)
            jl = crime_catch_chance(ctx.author.id, jail)
            rows.append(
                f"**Tier {i}** — {esc * 100:g}% escape · "
                f"{jl * 100:g}% jail if caught · fine **{fine:,}**"
            )
        body = (
            f"Pick a tier to steal from **{target.display_name}** "
            f"(wallet: **{victim_bal:,} 🪙**).\n\n"
            + "\n".join(rows)
            + "\n\n*Higher tiers steal more — but the **jail chance and fine** go up too.*"
        )
        view = _StealTierView(cog=self, ctx=ctx, target=target, victim_bal=victim_bal)
        view.message = await ctx.send(embed=emb("🦹 Choose Robbery Tier", body, C_GOLD), view=view)

    async def _run_steal(self, ctx: commands.Context, target, tier_num: int):
        TRACK = 20

        thief_id = ctx.author.id
        victim_id = target.id

        if victim_id == thief_id:
            await ctx.send("You can't steal from yourself.")
            return
        if self.bot.user and victim_id == self.bot.user.id:
            await ctx.send("You can't steal from the house.")
            return

        await _ensure_user(thief_id)
        await _ensure_user(victim_id)
        await _maybe_latch_crime_eligible(victim_id)
        if not state.economy["users"][str(victim_id)].get("crime_eligible"):
            await ctx.send(embed=emb(
                "🛡️ Off-Limits",
                f"**{target.display_name}** isn't in the crime system yet — they're below Level 10 and have never held more than {CRIME_ELIGIBLE_NET_WORTH:,} 🪙 across wallet + savings.",
                C_GOLD,
            ))
            return

        thief_data = state.economy["users"][str(thief_id)]

        # Check jail
        jail_until = thief_data.get("jail_until", 0)
        if time.time() < jail_until:
            reason = thief_data.get("jail_reason")
            await ctx.send(embed=emb(
                "🚔 You're in Jail",
                _jail_body(ctx.author.display_name, jail_until, reason),
                C_RED,
            ))
            return

        if thief_id in self._crime_active:
            await ctx.send(embed=emb("⏳ Already Running", "You already have a crime in progress — wait for it to finish.", C_RED))
            return
        # Claim synchronously at the gate: the insurance/balance lookups below
        # yield, so two rapid invocations could otherwise both pass the check.
        self._crime_active.add(thief_id)
        try:
            if await is_insured(victim_id, "steal"):
                _exp = get_insurance_expiry(victim_id)
                await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance — you can't rob them! (expires <t:{_exp}:R>)", C_GOLD))
                return

            steal_chance, steal_pct, jail_chance, fee, jail_days = STEAL_TIERS[tier_num - 1]
            steal_chance = steal_success_chance(thief_id, steal_chance)
            jail_chance = crime_catch_chance(thief_id, jail_chance)
            victim_bal = await get_balance(victim_id)
            steal_amount = max(1, int(victim_bal * steal_pct))

            if thief_id in state.rigged_steal:
                state.rigged_steal[thief_id] -= 1
                if state.rigged_steal[thief_id] <= 0:
                    del state.rigged_steal[thief_id]
                await save_rigged_steal()
                success = True
            else:
                roll = random.random()
                success = roll < steal_chance

            # Animate the chase
            msg = None

            def build_frame(robber_p, cop_p, done=False, caught=False):
                robber_icon = "🏃" if not caught else "🤜"
                r_track = "░" * robber_p + robber_icon + "░" * (TRACK - robber_p)
                c_track = "░" * cop_p + "👮" + "░" * (TRACK - cop_p)
                lines = [f"`{r_track}` 🦹", f"`{c_track}` 🚔"]
                if done:
                    lines.append("\n**You got caught!** 🚨" if caught else "\n**Escaped!** 💨")
                return "\n".join(lines)

            steps = 8
            if success:
                robber_steps = [int(TRACK * (i + 1) / steps) for i in range(steps)]
                cop_steps = [max(0, int(TRACK * (i + 1) / steps) - random.randint(3, 6)) for i in range(steps)]
            else:
                half = steps // 2
                robber_steps = [int((TRACK // 2) * (i + 1) / half) if i < half else TRACK // 2 for i in range(steps)]
                cop_steps = [max(0, int((TRACK // 2) * (i + 1) / half) - random.randint(2, 4)) if i < half
                             else min(TRACK // 2, int((TRACK // 2) + (TRACK // 2) * (i - half + 1) / (steps - half)))
                             for i in range(steps)]

            for i in range(steps):
                caught_now = (not success) and (i == steps - 1)
                frame = build_frame(robber_steps[i], cop_steps[i], done=(i == steps - 1), caught=caught_now)
                e = emb("🦹 Robbery in Progress...", frame, C_ORANGE)
                if msg is None:
                    # silent=True: the animation (and the result embed it edits
                    # into) must never notify the channel; the victim gets one
                    # explicit ping below instead.
                    msg = await ctx.send(embed=e, silent=True)
                else:
                    await msg.edit(embed=e)
                await asyncio.sleep(0.6)

            # Resolve outcome
            stolen = 0
            if success:
                # Re-read the victim's balance: the ~5s chase animation yielded,
                # so it may have dropped (gambling, another steal). Crediting the
                # thief against the stale pre-animation balance minted coins when
                # the deduct silently failed.
                victim_bal = await get_balance(victim_id)
                if victim_bal < steal_amount:
                    steal_amount = victim_bal
                if steal_amount <= 0 or not await deduct_balance(victim_id, steal_amount):
                    result_embed = emb("🦹 Robbery Failed", f"**{target.display_name}** is broke — nothing to steal!", C_RED)
                else:
                    gid = ctx.guild.id if ctx.guild else 0
                    stolen = steal_amount
                    await add_balance(thief_id, steal_amount, guild_id=gid or None, holder_name=ctx.author.display_name)
                    await record_crime_event(gid, thief_id, gained=steal_amount)
                    await record_crime_event(gid, victim_id, lost=steal_amount)
                    result_embed = emb(
                        "🦹 Successful Robbery!",
                        f"**{ctx.author.display_name}** stole **{steal_amount:,} 🪙** from **{target.display_name}**!\n"
                        f"Your balance: **{await get_balance(thief_id):,} 🪙**",
                        C_GREEN,
                    )
            else:
                jailed = random.random() < jail_chance
                from_wallet = min(fee, await get_balance(thief_id))
                await deduct_balance(thief_id, from_wallet)
                from_savings = await seize_from_savings(thief_id, fee - from_wallet)
                actual_fine = from_wallet + from_savings
                await record_crime_event(ctx.guild.id if ctx.guild else 0, thief_id, lost=actual_fine)
                fine_line = f"Fined **{actual_fine:,} 🪙**"
                if from_savings > 0:
                    fine_line += f" (**{from_savings:,}** taken from savings)"
                if jailed:
                    jail_until_ts = time.time() + jail_days * 86400
                    thief_data["jail_until"] = jail_until_ts
                    thief_data["jail_reason"] = format_steal_reason(target.display_name, steal_amount)
                    thief_data["bail_amount"] = steal_amount
                    await save_economy(uid=thief_id)
                    result_embed = emb(
                        "🚔 Caught & Jailed!",
                        f"**{ctx.author.display_name}** was caught stealing from **{target.display_name}**!\n"
                        f"{fine_line} and jailed until <t:{int(jail_until_ts)}:F> (<t:{int(jail_until_ts)}:R>).\n"
                        f"Balance: **{await get_balance(thief_id):,} 🪙**",
                        C_RED,
                    )
                else:
                    result_embed = emb(
                        "🚔 Caught!",
                        f"**{ctx.author.display_name}** was caught stealing from **{target.display_name}**!\n"
                        f"{fine_line}. You got lucky — no jail time.\n"
                        f"Balance: **{await get_balance(thief_id):,} 🪙**",
                        C_ORANGE,
                    )

            if stolen > 0:
                await ctx.send(f"🚨 <@{victim_id}> — you've been robbed!")
            await msg.edit(embed=result_embed)

            await try_set_crime_record(
                ctx.channel, ctx.guild, stolen, thief_id, ctx.author.display_name,
                "steal", target.display_name,
            )
        finally:
            self._crime_active.discard(thief_id)


    # ── !bankheist ────────────────────────────────────────────────────────────
    # Lobby/party heist: host opens a 4-slot window (slot 1 = host, slots 2–4
    # joinable via 2️⃣/3️⃣/4️⃣ reactions). Host starts with 🚀 or cancels with ❌;
    # auto-starts at 60s with a 10s last-call warning. On success, seizes 20%
    # of the target's savings via seize_from_savings and splits evenly among
    # participants (host gets the integer-division remainder). Failure is a
    # no-op for everyone — savings are only at risk on success.

    BANKHEIST_JOIN_EMOJIS = ["2️⃣", "3️⃣", "4️⃣"]
    BANKHEIST_START_EMOJI = "🚀"
    BANKHEIST_CANCEL_EMOJI = "❌"
    BANKHEIST_LOBBY_TIMEOUT = 60.0
    BANKHEIST_LAST_CALL = 10.0
    BANKHEIST_SEIZE_PCT = 0.20

    @staticmethod
    def _bankheist_chance(host, joiners: list, guild_id: int) -> float:
        """Compute success chance from party size + per-player display level.
        Host: 0–10% bonus linear over levels 1→100 (level 1 = 0%, level 100 = 10%).
        Each joiner: 0–3% bonus on the same scale."""
        from src.level_unlocks import user_display_level
        party_size = 1 + len(joiners)
        base = {1: 0.01, 2: 0.10, 3: 0.15, 4: 0.25}[party_size]
        host_lvl = user_display_level(host.id, guild_id)
        bonus = min(0.10, max(0.0, (host_lvl - 1) / 99.0) * 0.10)
        for j in joiners:
            jl = user_display_level(j.id, guild_id)
            bonus += min(0.03, max(0.0, (jl - 1) / 99.0) * 0.03)
        return min(0.95, base + bonus)

    def _bankheist_header(self, host, target, joiners: list, guild_id: int, savings_value: float, *, include_open_slots: bool = True) -> str:
        """Render the crew roster + chance + pot block. Used by both the
        lobby embed and every resolution embed so the result keeps the
        context the user saw when they joined.

        When `include_open_slots` is False, unfilled slots are omitted
        (used in result embeds where 'open —' is no longer meaningful)."""
        from src.level_unlocks import user_display_level
        chance = self._bankheist_chance(host, joiners, guild_id)
        pot = int(savings_value * self.BANKHEIST_SEIZE_PCT)
        host_lvl = user_display_level(host.id, guild_id)

        # Per-player bonus inline labels — keep in sync with _bankheist_chance().
        host_bonus_pct = round(min(0.10, max(0.0, (host_lvl - 1) / 99.0) * 0.10) * 100, 1)
        crew_lines = [
            f"  👑 {host.display_name}  (Lv {host_lvl}) (+{host_bonus_pct}%; up to 10%)"
        ]
        # joiners is filled left-to-right in slot 2/3/4 — render each with its emoji.
        for i, emoji in enumerate(self.BANKHEIST_JOIN_EMOJIS):
            member = joiners[i] if i < len(joiners) else None
            if member is None:
                if include_open_slots:
                    crew_lines.append(f"  {emoji} — open —")
                continue
            lvl = user_display_level(member.id, guild_id)
            joiner_bonus_pct = round(min(0.03, max(0.0, (lvl - 1) / 99.0) * 0.03) * 100, 1)
            crew_lines.append(
                f"  {emoji} {member.display_name}  (Lv {lvl}) (+{joiner_bonus_pct}%; up to 3%)"
            )

        return (
            f"Host: {host.mention}\n"
            f"Crew:\n" + "\n".join(crew_lines) + "\n\n"
            f"**Success chance:** {round(chance * 100, 1)}%\n"
            f"**Pot if successful:** ~{pot:,} 🪙 from {target.display_name}'s savings"
        )

    def _bankheist_render(self, hstate: dict, guild_id: int, savings_value: float, last_call: bool = False):
        """Build the lobby embed reflecting current slot occupants and chance."""
        host = hstate["host"]
        target = hstate["target"]
        joiners = [m for m in hstate["slots"][1:] if m is not None]

        deadline_ts = int(hstate["opened_at_wall"] + self.BANKHEIST_LOBBY_TIMEOUT)
        body = (
            self._bankheist_header(host, target, joiners, guild_id, savings_value)
            + "\n\n"
            f"React 2️⃣–4️⃣ to join. Host: 🚀 to start, ❌ to cancel.\n"
            f"{target.display_name} cannot join.\n"
            f"Auto-starts <t:{deadline_ts}:R>."
        )
        if last_call:
            body += f"\n\n⚠️ **Last call** — auto-starting <t:{deadline_ts}:R>!"
        return emb(f"🏦 Bank Heist — targeting {target.display_name}", body, C_ORANGE)

    BANKHEIST_PARTICIPANT_JAIL_CHANCE = 0.25
    BANKHEIST_PARTICIPANT_JAIL_SECONDS = 86400  # 1 day

    async def _roll_participant_jail(self, participants: list, target_name: str, intended_per_person: int = 0) -> list:
        """For each participant, roll 25% to be jailed for 1 day. Returns the
        list of jailed Members (for embed display). Mutates state and persists.

        `intended_per_person` is recorded as the participant's bail_amount for
        bail-cost calculation: the amount they tried to / did steal."""
        jailed: list = []
        jail_until_ts = time.time() + self.BANKHEIST_PARTICIPANT_JAIL_SECONDS
        for p in participants:
            if random.random() < self.BANKHEIST_PARTICIPANT_JAIL_CHANCE:
                await _ensure_user(p.id)
                pdata = state.economy["users"][str(p.id)]
                pdata["jail_until"] = jail_until_ts
                pdata["jail_reason"] = format_bankheist_reason(target_name)
                pdata["bail_amount"] = int(intended_per_person)
                await save_economy(uid=p.id)
                jailed.append(p)
        return jailed

    @staticmethod
    def _format_caught_line(jailed: list) -> str:
        """Render the trailing 'Caught:' line for the result embed."""
        if not jailed:
            return ""
        names = ", ".join(p.mention for p in jailed)
        return f"\n\n🚔 **Caught:** {names} — jailed for 1 day."

    async def _bankheist_resolve(self, ctx: commands.Context, hstate: dict) -> discord.Embed:
        """Roll the heist and apply outcome. Returns the result embed.
        Pure-ish: reads/writes state via the standard economy helpers, doesn't
        touch reactions or the lobby loop. Tests call this directly.

        After the loot split (or on failure / empty vault), every participant
        rolls 25% to get jailed for 1 day. Caught players are listed in a
        trailing line on the result embed."""
        host = hstate["host"]
        target = hstate["target"]
        joiners = [m for m in hstate["slots"][1:] if m is not None]
        participants = [host] + joiners
        gid = ctx.guild.id if ctx.guild else 0

        # Snapshot the lobby header (crew + chance + pot) once so every result
        # embed below shows the user the same info they saw in the lobby.
        savings_value = await get_savings_value(target.id)
        header = self._bankheist_header(
            host, target, joiners, gid, savings_value, include_open_slots=False,
        )

        chance = self._bankheist_chance(host, joiners, gid)
        success = random.random() < chance

        if not success:
            jailed = await self._roll_participant_jail(participants, target.display_name, intended_per_person=0)
            return emb(
                "🚨 Heist Failed!",
                header + "\n\n"
                f"The crew bailed at the door — {target.display_name}'s savings are untouched.\n"
                f"(Roll missed a {round(chance * 100, 1)}% chance.)"
                + self._format_caught_line(jailed),
                C_RED,
            )

        intended = int(savings_value * self.BANKHEIST_SEIZE_PCT)
        if intended <= 0:
            jailed = await self._roll_participant_jail(
                participants, target.display_name,
                intended_per_person=intended // len(participants),
            )
            return emb(
                "💨 Empty Vault",
                header + "\n\n"
                f"You broke in clean — but **{target.display_name}** had no savings to steal."
                + self._format_caught_line(jailed),
                C_GREY,
            )

        seized = await seize_from_savings(target.id, intended)
        if seized <= 0:
            jailed = await self._roll_participant_jail(
                participants, target.display_name,
                intended_per_person=intended // len(participants),
            )
            return emb(
                "💨 Empty Vault",
                header + "\n\n"
                f"You broke in clean — but **{target.display_name}**'s savings were already empty."
                + self._format_caught_line(jailed),
                C_GREY,
            )

        share = seized // len(participants)
        remainder = seized - share * len(participants)
        gid = ctx.guild.id if ctx.guild else 0
        cuts: list[tuple] = []
        for p in participants:
            cut = share + (remainder if p.id == host.id else 0)
            await add_balance(
                p.id, cut,
                guild_id=gid or None,
                holder_name=p.display_name,
            )
            await record_crime_event(gid, p.id, gained=cut)
            cuts.append((p, cut))
        await record_crime_event(gid, target.id, lost=seized)

        jailed = await self._roll_participant_jail(
            participants, target.display_name, intended_per_person=share,
        )
        cut_lines = "\n".join(
            f"  • {p.display_name}: **{cut:,} 🪙**" for p, cut in cuts
        )

        # The crew shares one score, so the record is attributed to all of
        # them; holder_id stays the host's. The value is `seized` — the whole
        # pot — which is the same "taken off the victim" measure !steal and
        # !mug compete on, rather than any one player's cut.
        await try_set_crime_record(
            ctx.channel, ctx.guild, seized, host.id,
            ", ".join(p.display_name for p, _ in cuts),
            "bankheist", target.display_name,
            crew=[{"id": p.id, "name": p.display_name, "cut": cut} for p, cut in cuts],
        )

        return emb(
            "🏦 Heist Successful!",
            header + "\n\n"
            f"The crew cracked **{target.display_name}**'s savings for **{seized:,} 🪙**!\n\n"
            f"{cut_lines}"
            + self._format_caught_line(jailed),
            C_GREEN,
        )

    @commands.command(name="bankheist")
    @requires_perm
    async def cmd_bankheist(self, ctx: commands.Context, target: OptionalMember = None):
        if target is None:
            await ctx.send(embed=emb(
                "🏦 Bank Heist",
                "Usage: `!bankheist @user` — opens a 4-slot lobby. Up to 3 others react "
                "2️⃣/3️⃣/4️⃣ to join, then host reacts 🚀 to start (or ❌ to cancel). "
                "Auto-starts in 60s.",
                C_BLUE,
            ))
            return

        if (err := _crime_public_only_error(ctx)) is not None:
            await ctx.send(embed=err)
            return

        host = ctx.author
        gid = ctx.guild.id if ctx.guild else 0

        if target.id == host.id:
            await ctx.send("You can't rob yourself.")
            return
        if target.bot or (self.bot.user and target.id == self.bot.user.id):
            await ctx.send("You can't rob the house.")
            return

        await _ensure_user(target.id)
        await _maybe_latch_crime_eligible(target.id)
        if not state.economy["users"][str(target.id)].get("crime_eligible"):
            await ctx.send(embed=emb(
                "🛡️ Off-Limits",
                f"**{target.display_name}** isn't in the crime system yet — they're below Level 10 and have never held more than {CRIME_ELIGIBLE_NET_WORTH:,} 🪙 across wallet + savings.",
                C_GOLD,
            ))
            return

        if await is_insured(target.id, "steal"):
            _exp = get_insurance_expiry(target.id)
            await ctx.send(embed=emb(
                "🛡️ Protected",
                f"**{target.display_name}** has insurance — their savings are off-limits! (expires <t:{_exp}:R>)",
                C_GOLD,
            ))
            return

        await _ensure_user(host.id)
        host_data = state.economy["users"][str(host.id)]
        jail_until = host_data.get("jail_until", 0)
        if time.time() < jail_until:
            await ctx.send(embed=emb(
                "🚔 You're in Jail",
                f"**{host.display_name}** is locked up! Released <t:{int(jail_until)}:R>.",
                C_RED,
            ))
            return

        ch_id = ctx.channel.id
        if ch_id in self._active_heists:
            await ctx.send(embed=emb(
                "⏳ Heist Already Running",
                "Another bankheist is already open in this channel — wait for it to finish.",
                C_RED,
            ))
            return

        slots: list = [host, None, None, None]
        hstate: dict = {
            "host": host,
            "target": target,
            "slots": slots,
            "message": None,
            "opened_at": asyncio.get_running_loop().time(),
            "opened_at_wall": time.time(),
            "warned": False,
            "started": False,
            "cancelled": False,
        }
        self._active_heists[ch_id] = hstate

        try:
            savings_value = await get_savings_value(target.id)
            lobby_msg = await ctx.send(embed=self._bankheist_render(hstate, gid, savings_value))
            hstate["message"] = lobby_msg

            for emoji in self.BANKHEIST_JOIN_EMOJIS:
                await lobby_msg.add_reaction(emoji)
            await lobby_msg.add_reaction(self.BANKHEIST_START_EMOJI)
            await lobby_msg.add_reaction(self.BANKHEIST_CANCEL_EMOJI)

            def check(reaction, user):
                if reaction.message.id != lobby_msg.id:
                    return False
                if user.bot or user.id == target.id:
                    return False
                emoji_s = str(reaction.emoji)
                if emoji_s in self.BANKHEIST_JOIN_EMOJIS:
                    if user.id == host.id:
                        return False
                    return user.id not in {m.id for m in slots if m is not None}
                if emoji_s in (self.BANKHEIST_START_EMOJI, self.BANKHEIST_CANCEL_EMOJI):
                    return user.id == host.id
                return False

            while True:
                if all(s is not None for s in slots):
                    break  # auto-start when full

                elapsed = asyncio.get_running_loop().time() - hstate["opened_at"]
                time_left = self.BANKHEIST_LOBBY_TIMEOUT - elapsed
                if time_left <= 0:
                    break

                if not hstate["warned"] and time_left <= self.BANKHEIST_LAST_CALL:
                    hstate["warned"] = True
                    try:
                        await lobby_msg.edit(embed=self._bankheist_render(
                            hstate, gid, savings_value, last_call=True,
                        ))
                    except Exception:
                        pass
                    wait_for_timeout = time_left
                else:
                    time_until_warning = max(0.0, time_left - self.BANKHEIST_LAST_CALL)
                    wait_for_timeout = time_until_warning if not hstate["warned"] else time_left
                    if wait_for_timeout <= 0:
                        wait_for_timeout = time_left

                try:
                    reaction, user = await ctx.bot.wait_for(
                        "reaction_add", check=check, timeout=wait_for_timeout,
                    )
                except asyncio.TimeoutError:
                    continue  # loop re-evaluates time_left / warning state

                emoji_s = str(reaction.emoji)
                if emoji_s == self.BANKHEIST_CANCEL_EMOJI:
                    hstate["cancelled"] = True
                    break
                if emoji_s == self.BANKHEIST_START_EMOJI:
                    hstate["started"] = True
                    break
                # Slot reaction — fill the matching index.
                slot_idx = self.BANKHEIST_JOIN_EMOJIS.index(emoji_s) + 1
                if slots[slot_idx] is None:
                    slots[slot_idx] = user
                    try:
                        await lobby_msg.edit(embed=self._bankheist_render(
                            hstate, gid, savings_value, last_call=hstate["warned"],
                        ))
                    except Exception:
                        pass

            # Lobby phase is over — clear the join/start/cancel reactions so
            # nobody can react after the fact. Best-effort: in DMs the bot
            # lacks Manage Messages and Forbidden is fine to swallow.
            try:
                await lobby_msg.clear_reactions()
            except Exception:
                pass

            if hstate["cancelled"]:
                await lobby_msg.edit(embed=emb(
                    "🏦 Bank Heist — Cancelled",
                    f"{host.display_name} called it off.",
                    C_GREY,
                ))
                return

            result_embed = await self._bankheist_resolve(ctx, hstate)
            await lobby_msg.edit(embed=result_embed)
        finally:
            self._active_heists.pop(ch_id, None)


    @commands.command(name="jail")
    async def cmd_jail(self, ctx: commands.Context, target: OptionalMember = None):
        member = target or ctx.author
        await _ensure_user(member.id)
        user_data = state.economy["users"][str(member.id)]
        jail_until = user_data.get("jail_until", 0)
        if time.time() < jail_until:
            reason = user_data.get("jail_reason")
            body = _jail_body(member.display_name, jail_until, reason)
            if member.id == ctx.author.id:
                options = ["`!bail`"]
                if not user_data.get("jailbreak_used", False):
                    options.insert(0, "`!jailbreak`")
                body += f"\nGet out with {' or '.join(options)}."
            await ctx.send(embed=emb(
                "🚔 In Jail",
                body,
                C_RED,
            ))
        else:
            await ctx.send(embed=emb(
                "✅ Not in Jail",
                f"**{member.display_name}** is a free citizen.",
                C_GREEN,
            ))


    @commands.command(name="jailbreak")
    @requires_perm
    async def cmd_jailbreak(self, ctx: commands.Context):
        uid = ctx.author.id
        await _ensure_user(uid)

        today = _ct_today()
        if state.economy.get("last_daily_reset") != today:
            await do_daily_reset()

        user_data = state.economy["users"][str(uid)]

        jail_until = user_data.get("jail_until", 0)
        if time.time() >= jail_until:
            await ctx.send(embed=emb("🔓 Not Jailed", "You're already a free citizen — no need to escape.", C_GOLD))
            return

        if user_data.get("jailbreak_used", False):
            reset_ts = next_daily_reset_ts()
            await ctx.send(embed=emb("🚔 One Attempt Per Day", f"You already tried to break out today. Next attempt available <t:{reset_ts}:R> (at 5am CT).", C_RED))
            return

        user_data["jailbreak_used"] = True
        await save_economy(uid=uid)

        if random.random() < 0.20:
            user_data["jail_until"] = 0
            user_data["jail_reason"] = None
            await save_economy(uid=uid)
            await ctx.send(embed=emb(
                "🏃 Escaped!",
                f"**{ctx.author.display_name}** dug a tunnel under the fence and slipped out! You're free.",
                C_GREEN,
            ))
        else:
            await ctx.send(embed=emb(
                "🚨 Failed Escape",
                f"**{ctx.author.display_name}** was caught by the guards and thrown back in the cell.\n"
                f"Released <t:{int(jail_until)}:R>. No more attempts until tomorrow.",
                C_RED,
            ))

    @commands.command(name="adminjailbreak")
    @requires_perm
    async def cmd_adminjailbreak(self, ctx: commands.Context, target: OptionalMember = None):
        if target is None:
            await ctx.send(embed=emb("❌ Usage", "`!adminjailbreak @user`", C_RED))
            return
        await _ensure_user(target.id)
        user_data = state.economy["users"][str(target.id)]
        user_data["jail_until"] = 0
        user_data["jail_reason"] = None
        await save_economy(uid=target.id)
        await ctx.send(embed=emb("🔓 Released", f"**{target.display_name}** has been freed from jail.", C_GREEN))

    @commands.command(name="bail", aliases=["bailout"])
    @requires_perm
    async def cmd_bail(self, ctx: commands.Context, target: OptionalMember = None):
        payer = ctx.author
        jailed = target if target is not None else payer

        await _ensure_user(payer.id)
        await _ensure_user(jailed.id)
        jdata = state.economy["users"][str(jailed.id)]

        jail_until = jdata.get("jail_until", 0)
        if time.time() >= jail_until:
            who = "You're" if jailed.id == payer.id else f"**{jailed.display_name}** is"
            await ctx.send(embed=emb("🔓 Not Jailed", f"{who} not in jail.", C_GOLD))
            return

        bail_amount = int(jdata.get("bail_amount", 0) or 0)
        cost = bail_cost(payer.id, 10_000 + (bail_amount // 2))

        payer_bal = await get_balance(payer.id)
        if payer_bal < cost:
            await ctx.send(embed=emb(
                "❌ Insufficient Funds",
                f"Bail costs **{cost:,} 🪙** — you only have **{payer_bal:,} 🪙**.",
                C_RED,
            ))
            return

        is_self = jailed.id == payer.id
        confirm_desc = "Bail yourself out of jail." if is_self else f"Bail **{jailed.display_name}** out of jail."
        confirmed = await confirm_purchase(
            ctx,
            title="🪙 Pay Bail",
            description=confirm_desc,
            cost=cost,
            payer=payer,
        )
        if not confirmed:
            return

        # Reserve the bail synchronously so a concurrent !bail confirmation
        # for the same jailed user sees jail_until=0 and bails out early
        # instead of double-charging. Snapshot the prior values first so we
        # can roll back if deduct_balance fails.
        prior_jail_until = jdata.get("jail_until", 0)
        if time.time() >= prior_jail_until:
            free_msg = (
                "You got out before you confirmed — no charge."
                if is_self
                else f"**{jailed.display_name}** got out before you confirmed — no charge."
            )
            await ctx.send(embed=emb("🔓 Already Free", free_msg, C_GOLD))
            return
        prior_jail_reason = jdata.get("jail_reason")
        prior_bail_amount = jdata.get("bail_amount", 0)
        jdata["jail_until"] = 0
        jdata["jail_reason"] = None
        jdata["bail_amount"] = 0

        if not await deduct_balance(payer.id, cost):
            # Roll back the jail state so the user stays jailed.
            jdata["jail_until"] = prior_jail_until
            jdata["jail_reason"] = prior_jail_reason
            jdata["bail_amount"] = prior_bail_amount
            await ctx.send(embed=emb(
                "❌ Insufficient Funds",
                f"Your balance dropped during confirmation — bail costs **{cost:,} 🪙**.",
                C_RED,
            ))
            return

        await save_economy(uid=jailed.id)

        if is_self:
            released_line = f"**{payer.display_name}** paid **{cost:,} 🪙** to bail themself out of jail."
        else:
            released_line = f"**{payer.display_name}** paid **{cost:,} 🪙** to bail **{jailed.display_name}** out of jail."
        await ctx.send(embed=emb(
            "🔓 Released on Bail",
            f"{released_line}\nPayer balance: **{await get_balance(payer.id):,} 🪙**",
            C_GREEN,
        ))

    @commands.command(name="mug")
    @requires_perm
    async def cmd_mug(self, ctx: commands.Context, target: OptionalMember = None, amount: str = None):
        uid = ctx.author.id

        await _ensure_user(uid)
        user_data = state.economy["users"][str(uid)]
        jail_until = user_data.get("jail_until", 0)
        if time.time() < jail_until:
            reason = user_data.get("jail_reason")
            body = f"You can't mug anyone from behind bars! Released <t:{int(jail_until)}:R>."
            if reason:
                body += f"\nReason: {reason}"
            await ctx.send(embed=emb("🚔 In Jail", body, C_RED))
            return

        if target is None or amount is None:
            await ctx.invoke(self.cmd_crime)
            return

        if (err := _crime_public_only_error(ctx)) is not None:
            await ctx.send(embed=err)
            return

        if uid in self._crime_active:
            await ctx.send(embed=emb("⏳ Already Running", "You already have a crime in progress — wait for it to finish.", C_RED))
            return
        # Claim synchronously at the gate: the eligibility/insurance/charge
        # awaits below yield, so two rapid invocations could otherwise both
        # pass the check (and both get charged).
        self._crime_active.add(uid)
        try:
            if target.id == uid:
                await ctx.send(embed=emb("❌ Self Mug", "You can't mug yourself!", C_RED))
                return
            if self.bot.user and target.id == self.bot.user.id:
                await ctx.send(embed=emb("❌ Invalid Target", "You can't mug the house.", C_RED))
                return

            await _ensure_user(target.id)
            await _maybe_latch_crime_eligible(target.id)
            if not state.economy["users"][str(target.id)].get("crime_eligible"):
                await ctx.send(embed=emb(
                    "🛡️ Off-Limits",
                    f"**{target.display_name}** isn't in the crime system yet — they're below Level 10 and have never held more than {CRIME_ELIGIBLE_NET_WORTH:,} 🪙 across wallet + savings.",
                    C_GOLD,
                ))
                return

            parsed = await parse_amount(ctx, amount)
            if parsed is None:
                return
            if parsed <= 0:
                await ctx.send(embed=emb("❌ Invalid Amount", "Amount must be positive.", C_RED))
                return

            if await is_insured(target.id, "steal"):
                _exp = get_insurance_expiry(target.id)
                await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance and can't be mugged (expires <t:{_exp}:R>).", C_GOLD))
                return

            target_bal = await get_balance(target.id)
            if target_bal <= 0:
                await ctx.send(embed=emb("❌ Broke Target", f"**{target.display_name}** has no coins for the muggers to take.", C_RED))
                return

            your_bal = await get_balance(uid)
            if target_bal < your_bal * 0.2:
                await ctx.send(embed=emb("❌ Too Easy", f"**{target.display_name}** has less than 20% of your balance — find someone your own size.", C_RED))
                return

            cost = 0 if uid in state.godmode_users else parsed
            if not await shop_charge(ctx, uid, cost, cost_label=f"{parsed:,}"):
                return

            jailed = uid not in state.godmode_users and random.random() < crime_catch_chance(uid, 0.5)

            TRACK = 20
            steps = 8
            def build_mug_frame(robber_p, cop_p, done=False, caught=False):
                robber_icon = "🏃" if not caught else "🤜"
                r_track = "░" * robber_p + robber_icon + "░" * (TRACK - robber_p)
                c_track = "░" * cop_p + "👮" + "░" * (TRACK - cop_p)
                lines = [f"`{r_track}` 🔪", f"`{c_track}` 🚔"]
                if done:
                    lines.append("\n**Witness called the cops!** 🚨" if caught else "\n**Clean getaway!** 💨")
                return "\n".join(lines)

            if jailed:
                half = steps // 2
                robber_steps = [int((TRACK // 2) * (i + 1) / half) if i < half else TRACK // 2 for i in range(steps)]
                cop_steps = [max(0, int((TRACK // 2) * (i + 1) / half) - random.randint(2, 4)) if i < half
                             else min(TRACK // 2, int((TRACK // 2) + (TRACK // 2) * (i - half + 1) / (steps - half)))
                             for i in range(steps)]
            else:
                robber_steps = [int(TRACK * (i + 1) / steps) for i in range(steps)]
                cop_steps = [max(0, int(TRACK * (i + 1) / steps) - random.randint(3, 6)) for i in range(steps)]

            msg = None
            for i in range(steps):
                caught_now = jailed and (i == steps - 1)
                frame = build_mug_frame(robber_steps[i], cop_steps[i], done=(i == steps - 1), caught=caught_now)
                e = emb("🔪 Mugging in Progress...", frame, C_ORANGE)
                if msg is None:
                    # silent=True: see _run_steal — result must not notify the
                    # channel; the victim gets one explicit ping below.
                    msg = await ctx.send(embed=e, silent=True)
                else:
                    await msg.edit(embed=e)
                await asyncio.sleep(0.6)

            # Re-read the target's balance post-animation; only report a loss
            # the deduct actually took.
            actual_steal = min(parsed, await get_balance(target.id))
            if actual_steal > 0 and not await deduct_balance(target.id, actual_steal):
                actual_steal = 0
            # Attacker paid `parsed` upfront (muggers' fee, charged via shop_charge);
            # victim loses `actual_steal`. Neither gains.
            gid = ctx.guild.id if ctx.guild else 0
            await record_crime_event(gid, uid, lost=parsed)
            await record_crime_event(gid, target.id, lost=actual_steal)

            if jailed:
                jail_until_ts = time.time() + 86400
                state.economy["users"][str(uid)]["jail_until"] = jail_until_ts
                state.economy["users"][str(uid)]["jail_reason"] = format_mug_reason(target.display_name, parsed)
                state.economy["users"][str(uid)]["bail_amount"] = parsed
                await save_economy(uid=uid)
                result_embed = emb(
                    "🔪 Mugged — but Caught!",
                    f"**{ctx.author.display_name}** paid muggers **{parsed:,} 🪙** to take **{actual_steal:,} 🪙** from **{target.display_name}**!\n"
                    f"The muggers kept it all.\n"
                    f"**{target.display_name}**'s balance: **{await get_balance(target.id):,} 🪙**\n\n"
                    f"🚔 A witness called the cops — **{ctx.author.display_name}** is jailed until <t:{int(jail_until_ts)}:F> (<t:{int(jail_until_ts)}:R>)!",
                    C_RED,
                )
            else:
                result_embed = emb(
                    "🔪 Mugged!",
                    f"**{ctx.author.display_name}** paid muggers **{parsed:,} 🪙** to take **{actual_steal:,} 🪙** from **{target.display_name}**!\n"
                    f"The muggers kept it all.\n"
                    f"**{target.display_name}**'s balance: **{await get_balance(target.id):,} 🪙**",
                    C_ORANGE,
                )
            if actual_steal > 0:
                await ctx.send(f"🚨 <@{target.id}> — you've been mugged!")
            await msg.edit(embed=result_embed)

            # Getting caught doesn't undo the take — the victim is out the
            # coins either way — so a jailed mug still competes for the record.
            await try_set_crime_record(
                ctx.channel, ctx.guild, actual_steal, uid, ctx.author.display_name,
                "mug", target.display_name,
            )
        finally:
            self._crime_active.discard(uid)

    @commands.command(name="records", aliases=["record", "rec"])
    async def cmd_records(self, ctx: commands.Context, scope: str = "server"):
        """Display all-time records. `!records global` spans all servers; default is this server."""
        scope = scope.lower()
        if scope not in ("server", "global"):
            await ctx.send(embed=emb(
                "🏆 Records",
                "Usage: `!records [server|global]` — `global` spans all servers, default is this server.",
                C_RED,
            ))
            return

        if scope == "global":
            r = await load_global_records()
            title = "🏆 Global All-Time Records"
        else:
            if ctx.guild is None:
                await ctx.send(embed=emb("🏆 Records", "Server records are only available in servers.", C_RED))
                return
            r = await load_records(ctx.guild.id)
            title = "🏆 All-Time Records"

        def fmt(cat: str, label: str, extra_fn=None, unit: str = "🪙") -> str:
            rec = r.get(cat)
            if not rec:
                return f"**{label}:** *none yet*"
            name = rec.get("holder_name", "?")
            val = rec["value"]
            suffix = f" {unit}" if unit else ""
            base = f"**{label}:** {val:,}{suffix} — **{name}**"
            if extra_fn:
                base += extra_fn(rec)
            return base

        # Most hangman wins
        hangman_wins_entries = [
            (k, v) for k, v in r.items()
            if k.startswith("hangman_wins_")
        ]
        if hangman_wins_entries:
            _, top_v = max(hangman_wins_entries, key=lambda x: x[1]["value"])
            hm_wins_str = f"**Hangmans Completed:** {top_v['value']} — **{top_v['holder_name']}**"
        else:
            hm_wins_str = "**Hangmans Completed:** *none yet*"

        sections = [
            ("💰 Economy", [
                fmt("highest_balance", "Highest Balance"),
                fmt(CRIME_RECORD_CATEGORY, "Crime Payout",
                    lambda rec: f"\n  ↳ {format_crime_record_detail(rec)}"),
                fmt("command_streak", "Streak",
                    unit="day" if (r.get("command_streak") or {}).get("value") == 1 else "days"),
            ]),
            ("🎰 Gambling", [
                fmt("lottery", "Lottery Payout"),
                fmt("slots_jackpot", "Slots Jackpot Payout",
                    lambda rec: f"\n  ↳ Symbols: {rec.get('symbols', '?')} • Bet: {rec['bet']:,} 🪙" if rec.get('bet') is not None else ""),
                fmt("slots_non_jackpot", "Slots Non-Jackpot Payout",
                    lambda rec: f"\n  ↳ Symbols: {rec.get('symbols', '?')} • Bet: {rec['bet']:,} 🪙" if rec.get('bet') is not None else ""),
                fmt("blackjack", "Blackjack Payout",
                    lambda rec: f"\n  ↳ Hand: {rec.get('player_hand', '?')} ({rec.get('player_score', '?')}) • Dealer: {rec.get('dealer_score', '?')}"),
                fmt("flip", "Flip Payout"),
                fmt("race", "Race Payout"),
                fmt("scratchoff_day", "Scratchoff Day Payout"),
            ]),
            ("🎮 Games", [
                fmt("hangman_payout", "Hangman Payout",
                    lambda rec: f"\n  ↳ Word: `{rec.get('word', '?')}`"),
                hm_wins_str,
                fmt("highest_bot_chess_elo_defeated", "Highest Elo Defeated", unit="Elo"),
                fmt("chess_pvp_wins", "PvP Chess Wins",
                    unit="win" if (r.get("chess_pvp_wins") or {}).get("value") == 1 else "wins"),
            ]),
            ("🏠 Assets", [
                fmt("total_assets", "Properties Owned", unit=""),
                fmt("highest_property_value", "Property Portfolio"),
                fmt("total_artifacts", "Artifacts Owned", unit=""),
            ]),
        ]

        embed = discord.Embed(title=title, color=C_GOLD)
        embed.description = "\n\n".join(
            f"__**{header}**__\n" + "\n".join(entries) for header, entries in sections
        )
        await ctx.send(embed=embed)

    @commands.command(name="savings", aliases=["piggybank"])
    @requires_perm
    async def cmd_savings(self, ctx: commands.Context, action: str = None, amount: str = None):
        uid = ctx.author.id
        await _ensure_user(uid)

        # Support fused tokens: !savings +1000 or !savings -500
        if action is not None and action[0] in ("+", "-") and len(action) > 1:
            amount = action[1:]
            action = "add" if action[0] == "+" else "remove"

        # Normalize word aliases
        if action in ("+", "add", "deposit"):
            action = "add"
        elif action in ("-", "remove", "withdraw"):
            action = "remove"

        # A signed amount wins over the action word: the piggy bank's rule is
        # "minus means withdraw" (`!savings -500`), so `!withdraw -500` and
        # `!deposit -500` both withdraw instead of dead-ending on a
        # contradictory "must be positive" error. A leading `+` is just noise.
        if action in ("add", "remove") and amount:
            token = amount.strip()
            if len(token) > 1 and token[0] == "-":
                action = "remove"
                amount = token[1:]
            elif len(token) > 1 and token[0] == "+":
                amount = token[1:]

        show_principals = action in ("principals", "principal")

        if action is None or action not in ("add", "remove"):
            value = await get_savings_value(uid)
            deposits = state.economy["users"][str(uid)].get("savings", [])
            if not deposits:
                desc = (
                    f"**{ctx.author.display_name}** has no savings yet.\n\n"
                    "**Usage:**\n"
                    "`!deposit <amount>` — put coins in (`!savings +<amount>` works too)\n"
                    "`!withdraw <amount>` — take coins out (`!savings -<amount>` works too)\n\n"
                    f"*Savings earn **{SAVINGS_DAILY_PCT} compound interest per day**.*"
                )
            elif show_principals:
                now = time.time()
                principal = int(sum(e["amount"] for e in deposits))
                interest = int(value) - principal
                deposit_lines = []
                for e in deposits:
                    e_val = int(e["amount"] * savings_growth(e["deposited_at"], now))
                    e_principal = int(e["amount"])
                    e_interest = e_val - e_principal
                    # <t:...:d> renders in the viewer's timezone — naive
                    # fromtimestamp used the container's (UTC), so dates near
                    # midnight were off by a day for CT users.
                    deposit_lines.append(
                        f"<t:{int(e['deposited_at'])}:d> — {e_principal:,} 🪙 (+{e_interest:,})"
                    )
                desc = (
                    f"**{ctx.author.display_name}**'s piggy bank:\n\n"
                    f"**Current value:** {int(value):,} 🪙\n"
                    f"**Principal:** {principal:,} 🪙\n"
                    f"**Interest earned:** +{interest:,} 🪙\n\n"
                    "**Deposits:**\n" + "\n".join(deposit_lines)
                )
            else:
                principal = int(sum(e["amount"] for e in deposits))
                interest = int(value) - principal
                desc = (
                    f"**{ctx.author.display_name}**'s piggy bank:\n\n"
                    f"**Current value:** {int(value):,} 🪙\n"
                    f"**Principal:** {principal:,} 🪙\n"
                    f"**Interest earned:** +{interest:,} 🪙\n\n"
                    "**Usage:**\n"
                    "`!deposit <amount>` — put coins in\n"
                    "`!withdraw <amount>` — take coins out\n"
                    "`!savings principals` — show deposit breakdown\n\n"
                    f"*{SAVINGS_DAILY_PCT} compound interest per day, compounded on each deposit separately.*"
                )
            await send_ephemeral(ctx, embed=emb("🐷 Piggy Bank", desc, C_GREEN))
            return

        if amount is None or not amount.strip():
            verb = "deposit" if action == "add" else "withdraw"
            await ctx.send(embed=emb(
                "❌ Missing Amount",
                f"Usage: `!{verb} <amount>` — e.g. `!{verb} 2.5k`.",
                C_RED,
            ))
            return

        if amount.strip().lower() == "all":
            # `!save all` is advertised in command_tips.txt; make withdraw
            # symmetric: everything in the wallet / the full savings value.
            parsed = (
                await get_balance(uid) if action == "add"
                else int(await get_savings_value(uid))
            )
            if parsed <= 0:
                where = "wallet" if action == "add" else "savings"
                await ctx.send(embed=emb(
                    "❌ Nothing to Move", f"Your {where} is empty.", C_RED,
                ))
                return
        else:
            # parse_amount enforces >= 1, and the sign-routing above already
            # turned any minus into a withdraw — so the only failures left
            # are genuinely malformed amounts.
            parsed = await parse_amount(
                ctx, amount,
                error_msg="Amount must be at least 1 coin — plain numbers, `2.5k`/`1m` shorthand, `50%` of your wallet, or `all`.",
            )
            if parsed is None:
                return

        if action == "add":
            if not await add_savings(uid, parsed):
                bal = await get_balance(uid)
                await ctx.send(embed=emb(
                    "❌ Insufficient Funds",
                    f"You only have **{bal:,} 🪙** in your wallet.",
                    C_RED,
                ))
                return
            value = await get_savings_value(uid)
            await ctx.send(embed=emb(
                "🐷 Deposited",
                f"**{ctx.author.display_name}** tucked away **{parsed:,} 🪙** into the piggy bank!\n"
                f"Savings value: **{int(value):,} 🪙** | Wallet: **{await get_balance(uid):,} 🪙**",
                C_GREEN,
            ))
        else:  # remove
            if not await remove_savings(uid, parsed):
                value = await get_savings_value(uid)
                await ctx.send(embed=emb(
                    "❌ Insufficient Savings",
                    f"Your savings are only worth **{int(value):,} 🪙**.",
                    C_RED,
                ))
                return
            value = await get_savings_value(uid)
            await ctx.send(embed=emb(
                "🐷 Withdrawn",
                f"**{ctx.author.display_name}** smashed the piggy bank for **{parsed:,} 🪙**!\n"
                f"Savings remaining: **{int(value):,} 🪙** | Wallet: **{await get_balance(uid):,} 🪙**",
                C_GREEN,
            ))

    @commands.command(name="save")
    async def cmd_save(self, ctx: commands.Context, amount: str = None):
        await self.cmd_savings(ctx, "add", amount)

    # Shorthands for the savings subcommands. Both take a plain positive
    # amount; a minus still flips to withdraw via the sign-routing in
    # cmd_savings. Level-gated with `savings` via _GATE_ALIASES in
    # src/level_unlocks.py.
    @commands.command(name="deposit", aliases=["dep"])
    async def cmd_deposit(self, ctx: commands.Context, amount: str = None):
        await self.cmd_savings(ctx, "add", amount)

    @commands.command(name="withdraw", aliases=["wd"])
    async def cmd_withdraw(self, ctx: commands.Context, amount: str = None):
        await self.cmd_savings(ctx, "remove", amount)

    @commands.command(name="economy", aliases=["eco"])
    @requires_perm
    async def cmd_economy(self, ctx: commands.Context):

        from src.level_unlocks import fmt_line
        uid_help = ctx.author.id
        gid_help = ctx.guild.id if ctx.guild else 0

        users = state.economy["users"]
        now = time.time()

        total_wallets = sum(u.get("balance", 0) for u in users.values())
        total_savings = 0
        users_with_savings = 0
        jailed = 0
        for u in users.values():
            deps = u.get("savings", [])
            if deps:
                users_with_savings += 1
                total_savings += int(sum(e["amount"] * savings_growth(e["deposited_at"], now) for e in deps))
            if u.get("jail_until", 0) > now:
                jailed += 1

        total_circulation = total_wallets + total_savings
        lottery_pool = (await load_lottery(ctx.guild.id)).get("prize_pool", 0) if ctx.guild else 0
        house_bal = get_guild_house_balance(ctx.guild.id) if ctx.guild else 0
        total_users = len(users)

        # Property stats: book value isn't "circulation" (the purchase coins
        # were burned), so it gets its own lines and a combined net worth.
        from src.properties import (
            PROPERTIES, PROPERTIES_BY_ID, property_daily_revenue,
            total_owned_property_value,
        )
        owned_count = sum(1 for pid in state.property_owners if pid in PROPERTIES_BY_ID)
        total_property = total_owned_property_value()
        total_property_daily = sum(
            property_daily_revenue(pid)
            for pid in state.property_owners if pid in PROPERTIES_BY_ID
        )

        stats = (
            f"**Total in wallets:** {total_wallets:,} 🪙\n"
            f"**Total in savings:** {total_savings:,} 🪙\n"
            f"**Total in circulation:** {total_circulation:,} 🪙\n"
            f"**Total in property:** {total_property:,} 🪙 ({owned_count}/{len(PROPERTIES)} owned)\n"
            f"**Daily property revenue:** {total_property_daily:,} 🪙/day\n"
            f"**Net worth (circulation + property):** {total_circulation + total_property:,} 🪙\n"
            f"**Lottery pool:** {lottery_pool:,} 🪙\n"
            f"**House pot:** {house_bal:,} 🪙\n\n"
            f"**Users tracked:** {total_users:,}\n"
            f"**Users with savings:** {users_with_savings:,}\n"
            f"**Users in jail:** {jailed:,}\n\n"
            "**Economy commands:**\n"
            "`!balance [@user]` — Check wallet\n"
            "`!pay @user <amount>` — Send coins\n"
            f"{fmt_line('savings', '`!savings` — Piggy bank (' + SAVINGS_DAILY_PCT + ' daily interest)', uid_help, gid_help)}\n"
            "`!assets` — Real estate (revenue with your daily)\n"
            "`!crime` — Steal, mug, jailbreak\n"
            "`!lottery` — Monthly lottery info\n"
            "`!shop` — Spend coins"
        )
        await send_ephemeral(ctx, embed=emb("📊 Economy", stats, C_GOLD))

    @commands.command(name="pay", aliases=["give", "gift", "donate", "tip", "send"])
    async def cmd_pay(self, ctx: commands.Context, recipient: OptionalMember = None, amount: str = None):
        if recipient is None or amount is None:
            # Echo the alias the user typed (!give, !gift, ...) — a hardcoded
            # `!pay` here reads as "that command doesn't exist, use !pay".
            invoked = getattr(ctx, "invoked_with", None) or "pay"
            await ctx.send(f"Usage: `!{invoked} @user <amount>`")
            return
        if recipient.id == ctx.author.id:
            await ctx.send(embed=emb("❌ Invalid Recipient", f"**{ctx.author.display_name}** can't pay themselves.", C_RED))
            return
        amount = await parse_amount(ctx, amount)
        if amount is None:
            return
        if not await shop_charge(ctx, ctx.author.id, amount):
            return
        if self.bot.user and recipient.id == self.bot.user.id and ctx.guild:
            await add_guild_house(ctx.guild.id, amount)
            await ctx.send(embed=emb(
                "💸 Payment Sent",
                f"**{ctx.author.display_name}** paid **{amount:,} 🪙** to the house pot.\n"
                f"Your balance: **{await get_balance(ctx.author.id):,} 🪙**",
                C_GREEN,
            ))
            return
        await add_balance(recipient.id, amount)
        await ctx.send(embed=emb(
            "💸 Payment Sent",
            f"**{ctx.author.display_name}** paid **{recipient.display_name}** {amount:,} 🪙\n"
            f"Your balance: **{await get_balance(ctx.author.id):,} 🪙**",
            C_GREEN,
        ))


    # ── Bot-admin economy mutators ────────────────────────────────────────────

    @commands.command(name="event")
    @requires_perm
    async def cmd_event(self, ctx: commands.Context, amount: str = None, duration: str = None):
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            logging.warning(f"[event] No permission to delete command message in {ctx.channel}")
        except Exception as e:
            logging.warning(f"[event] Failed to delete command message: {e}")
        if amount is None:
            await ctx.send(embed=emb("⚙️ Event", "Usage: `!event <amount> [duration_hours] [#channel]`", C_GREY))
            return
        amount = parse_int_amount(amount)
        if amount is None or amount <= 0:
            await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a positive whole number.", C_RED))
            return

        duration_hours = None
        if duration is not None:
            if duration.startswith("<#"):
                duration = None
            else:
                try:
                    duration_hours = float(duration)
                    if duration_hours <= 0:
                        raise ValueError
                except ValueError:
                    await ctx.send(embed=emb("❌ Invalid Duration", "Duration must be a positive number of hours.", C_RED))
                    return

        target_channel = ctx.channel
        if ctx.message.channel_mentions:
            target_channel = ctx.message.channel_mentions[-1]

        duration_str = ""
        if duration_hours:
            expires_at = int(time.time() + duration_hours * 3600)
            duration_str = f" (expires <t:{expires_at}:R>)"
        event_msg = await target_channel.send(embed=emb(
            "🎉 Coin Event!",
            f"React with 🪙 to receive **{amount:,} 🪙**!{duration_str}",
            C_GOLD,
        ))
        await event_msg.add_reaction("🪙")
        state.active_events[event_msg.id] = {"amount": amount, "rewarded": set()}

        if target_channel != ctx.channel:
            await ctx.send(embed=emb("✅ Event Started", f"Event posted in {target_channel.mention}.", C_GREEN))

        if duration_hours:
            async def _close_event():
                await asyncio.sleep(duration_hours * 3600)
                if event_msg.id in state.active_events:
                    del state.active_events[event_msg.id]
                    await event_msg.edit(embed=emb(
                        "🎉 Event Ended",
                        f"This event has ended. **{amount:,} 🪙** per reaction was given out.",
                        C_GREY,
                    ))
            asyncio.create_task(_close_event())


    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        """Award the configured event amount to each unique user reacting 🪙
        on a !event message. Idempotent — same user reacting twice grants once."""
        if reaction.message.id not in state.active_events:
            return
        if str(reaction.emoji) != "🪙":
            return
        if getattr(user, "bot", False):
            return
        guild = getattr(reaction.message, "guild", None)
        if is_silenced(user.id, guild.id if guild else None):
            return
        event = state.active_events[reaction.message.id]
        if user.id in event["rewarded"]:
            return
        try:
            event["rewarded"].add(user.id)
            await add_balance(user.id, event["amount"])
        except Exception as e:
            logging.error(f"[event] Error rewarding {user.id}: {e}")
            event["rewarded"].discard(user.id)


    @commands.command(name="admingive", aliases=["adminpay"])
    @requires_perm
    async def cmd_give(self, ctx: commands.Context, target: OptionalMember = None, amount: str = None):
        if target is None or amount is None:
            await ctx.send(embed=emb("⚙️ Give", "Usage: `!give @user <amount>`", C_GREY))
            return
        amount = parse_int_amount(amount, allow_negative=True)
        if amount is None or amount == 0:
            await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a non-zero whole number.", C_RED))
            return
        if amount < 0:
            if self.bot.user and target.id == self.bot.user.id:
                amount = max(amount, -1 * get_guild_house_balance(ctx.guild.id if ctx.guild else 0))
            else:
                amount = max(amount, -1 * await get_balance(target.id))
        if self.bot.user and target.id == self.bot.user.id and ctx.guild:
            await add_guild_house(ctx.guild.id, amount)
            action = "given to" if amount > 0 else "removed from"
            await ctx.send(embed=emb(
                "💸 Give",
                f"**{abs(amount):,} 🪙** {action} the **house pot** for this server. "
                f"House pot: {get_guild_house_balance(ctx.guild.id):,} 🪙",
                C_GOLD,
            ))
        else:
            await add_balance(target.id, amount)
            action = "given" if amount > 0 else "removed"
            await ctx.send(embed=emb(
                "💸 Give",
                f"**{abs(amount):,} 🪙** {action} {'to' if amount > 0 else 'from'} **{target.display_name}**. "
                f"New balance: {await get_balance(target.id):,} 🪙",
                C_GOLD,
            ))


    @commands.command(name="admingivexp", aliases=["adminxp", "adminpayxp"])
    @requires_perm
    async def cmd_givexp(self, ctx: commands.Context, target: OptionalMember = None, amount: str = None):
        if target is None or amount is None:
            await ctx.send(embed=emb("⚙️ Give XP", "Usage: `!admingivexp @user <amount>`", C_GREY))
            return
        if not ctx.guild:
            await ctx.send(embed=emb("❌ Guild Only", "This command can only be used in a server.", C_RED))
            return
        amount = parse_int_amount(amount, allow_negative=True)
        if amount is None or amount == 0:
            await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a non-zero whole number.", C_RED))
            return

        from src.persistence import save_leveling
        from src.leveling import _ensure_lvl_record, level_from_xp, display_level

        rec = _ensure_lvl_record(ctx.guild.id, target.id)
        if amount < 0:
            amount = max(amount, -rec["xp"])
            if amount == 0:
                await ctx.send(embed=emb(
                    "⚙️ Give XP",
                    f"**{target.display_name}** has 0 XP — nothing to remove.",
                    C_GREY,
                ))
                return
        rec["xp"] += amount
        rec["level"] = level_from_xp(rec["xp"])
        await save_leveling(guild_id=ctx.guild.id, uid=target.id)

        action = "given" if amount > 0 else "removed"
        await ctx.send(embed=emb(
            "✨ Give XP",
            f"**{abs(amount):,} XP** {action} {'to' if amount > 0 else 'from'} **{target.display_name}**. "
            f"New total: {rec['xp']:,} XP (Level {display_level(rec['level'])})",
            C_GOLD,
        ))


async def setup(bot):
    await bot.add_cog(EconomyCog(bot))
