import asyncio
import logging
import random
import time
import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_ORANGE, C_GREY, C_BLUE, parse_amount, send_ephemeral, fetch_member, shop_charge, MemberConverter,
)
from src.economy import (
    add_balance, deduct_balance, get_balance, get_guild_house_balance,
    add_guild_house, is_insured, get_insurance_expiry, _ct_now, _ct_today, do_daily_reset, _ensure_user,
    next_daily_reset_ts, get_savings_value, add_savings, remove_savings,
    seize_from_savings, record_crime_event, CRIME_ELIGIBLE_NET_WORTH,
)
from src.permissions import (
    requires_perm,
)
from src.persistence import (
    save_economy, save_rigged_steal,
    load_lottery, load_records,
)
from src.config import (
    DAILY_REWARD, DAILY_RESET_HOUR,
)
from src.jail_reasons import format_steal_reason, format_mug_reason, format_bankheist_reason
from src.confirm_view import confirm_purchase
from src import state


def _jail_body(name: str, jail_until_ts: float, reason: str | None) -> str:
    """Render the standard 'in jail' embed body. Omits the Reason line
    when reason is None/empty (legacy jails written before jail_reason existed)."""
    body = f"**{name}** is locked up! Released <t:{int(jail_until_ts)}:R>."
    if reason:
        body += f"\nReason: {reason}"
    return body



class EconomyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # uids currently mid-crime (steal/mug). In-flight per-cog state, never persisted.
        self._crime_active: set[int] = set()
        # channel_id → heist_state. One bankheist lobby per channel at a time.
        self._active_heists: dict[int, dict] = {}

    @commands.command(name="daily")
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
        gid = ctx.guild.id if ctx.guild else None
        await add_balance(uid, DAILY_REWARD, guild_id=gid, holder_name=ctx.author.display_name)
        user_data["daily_date"] = today
        user_data["last_daily"] = time.time()
        await save_economy(uid=uid)
        await ctx.send(embed=emb("🪙 Daily Reward", f"**{ctx.author.display_name}** claimed **+{DAILY_REWARD:,} 🪙**! Balance: **{await get_balance(uid):,} 🪙**", C_GREEN))


    @commands.command(name="balance", aliases=["bal", "b", "!", "$"])
    async def cmd_balance(self, ctx: commands.Context, target: MemberConverter = None):
        target = target or ctx.author
        if self.bot.user and target.id == self.bot.user.id and ctx.guild:
            bal = get_guild_house_balance(ctx.guild.id)
            await ctx.send(embed=emb("🏦 House Pot", f"**{ctx.guild.name}**: {bal:,} 🪙", C_GOLD))
        else:
            bal = await get_balance(target.id)
            await ctx.send(embed=emb("💰 Balance", f"**{target.display_name}**: {bal:,} 🪙", C_GREEN))


    @commands.command(name="leaderboard", aliases=["leaderboards", "lb"])
    async def cmd_leaderboard(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("Leaderboard is only available in servers.")
            return
        lottery = await load_lottery(ctx.guild.id)
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
            ticket_str = f" • {tickets:,} 🎟️" if tickets else ""
            lines.append(f"{prefix} **{name}** — {data['balance']:,} 🪙{ticket_str}")
        lines.append("\n*Also: `!levels` XP · `!lbr` roles*")
        await ctx.send(embed=emb("🪙 Leaderboard", "\n".join(lines), C_GREEN))


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
        lines = [
            f"**`!steal @user [tier]`**{steal_lock} — Pick a pocket. Chance to steal a % of their balance; risk jail if caught.",
            "  **Tier 1** — 10% steal chance, steal 10% | Jail chance: 25% | Fee: 1,000 🪙 | Jail: 1 day",
            "  **Tier 2** — 7% steal chance, steal 15%  | Jail chance: 35% | Fee: 1,000 🪙 | Jail: 1 day",
            "  **Tier 3** — 5% steal chance, steal 25%  | Jail chance: 50% | Fee: 1,000 🪙 | Jail: 1 day",
            "",
            f"**`!mug @user <amount>`**{mug_lock} — Pay muggers `<amount>` 🪙 to take that amount from a target. The muggers keep it. 50% chance you get jailed 1 day.",
            "",
            "**`!jailbreak`** — Attempt to escape jail (20% success). One attempt per day.",
            "",
            jail_status,
        ]
        await send_ephemeral(ctx, embed=emb("🦹 Crime", "\n".join(lines), C_GOLD))

    @commands.command(name="steal")
    async def cmd_steal(self, ctx: commands.Context, target: MemberConverter = None):
        TIERS = [
            # (steal_chance, steal_pct, jail_chance, fee, jail_days)
            (0.10, 0.10, 0.25, 1000, 1),
            (0.07, 0.15, 0.35, 1000, 1),
            (0.05, 0.25, 0.50, 1000, 1),
        ]
        TRACK = 20

        if target is None:
            await ctx.invoke(self.cmd_crime)
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

        await _ensure_user(thief_id)
        await _ensure_user(victim_id)
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

        if await is_insured(victim_id, "steal"):
            _exp = get_insurance_expiry(victim_id)
            await ctx.send(embed=emb("🛡️ Protected", f"**{target.display_name}** has insurance — you can't rob them! (expires <t:{_exp}:R>)", C_GOLD))
            return

        steal_chance, steal_pct, jail_chance, fee, jail_days = TIERS[tier_num - 1]
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

        self._crime_active.add(thief_id)
        try:
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
                e = emb("🦹 Heist in Progress...", frame, C_ORANGE)
                if msg is None:
                    msg = await ctx.send(embed=e)
                else:
                    await msg.edit(embed=e)
                await asyncio.sleep(0.6)

            # Resolve outcome
            if success:
                if victim_bal < steal_amount:
                    steal_amount = victim_bal
                if steal_amount <= 0:
                    result_embed = emb("🦹 Heist Failed", f"**{target.display_name}** is broke — nothing to steal!", C_RED)
                else:
                    await deduct_balance(victim_id, steal_amount)
                    await add_balance(thief_id, steal_amount, guild_id=ctx.guild.id if ctx.guild else None, holder_name=ctx.author.display_name)
                    await record_crime_event(thief_id, gained=steal_amount)
                    await record_crime_event(victim_id, lost=steal_amount)
                    result_embed = emb(
                        "🦹 Successful Heist!",
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
                await record_crime_event(thief_id, lost=actual_fine)
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

            await msg.edit(embed=result_embed)
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
        cuts: list[tuple] = []
        for p in participants:
            cut = share + (remainder if p.id == host.id else 0)
            await add_balance(
                p.id, cut,
                guild_id=ctx.guild.id if ctx.guild else None,
                holder_name=p.display_name,
            )
            await record_crime_event(p.id, gained=cut)
            cuts.append((p, cut))
        await record_crime_event(target.id, lost=seized)

        jailed = await self._roll_participant_jail(
            participants, target.display_name, intended_per_person=share,
        )
        cut_lines = "\n".join(
            f"  • {p.display_name}: **{cut:,} 🪙**" for p, cut in cuts
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
    async def cmd_bankheist(self, ctx: commands.Context, target: MemberConverter = None):
        if target is None:
            await ctx.send(embed=emb(
                "🏦 Bank Heist",
                "Usage: `!bankheist @user` — opens a 4-slot lobby. Up to 3 others react "
                "2️⃣/3️⃣/4️⃣ to join, then host reacts 🚀 to start (or ❌ to cancel). "
                "Auto-starts in 60s.",
                C_BLUE,
            ))
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
    async def cmd_jail(self, ctx: commands.Context, target: MemberConverter = None):
        member = target or ctx.author
        await _ensure_user(member.id)
        user_data = state.economy["users"][str(member.id)]
        jail_until = user_data.get("jail_until", 0)
        if time.time() < jail_until:
            reason = user_data.get("jail_reason")
            await ctx.send(embed=emb(
                "🚔 In Jail",
                _jail_body(member.display_name, jail_until, reason),
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
    async def cmd_adminjailbreak(self, ctx: commands.Context, target: MemberConverter = None):
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
    async def cmd_bail(self, ctx: commands.Context, target: MemberConverter = None):
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
        cost = 10_000 + (bail_amount // 2)

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

        if time.time() >= jdata.get("jail_until", 0):
            free_msg = (
                "You got out before you confirmed — no charge."
                if is_self
                else f"**{jailed.display_name}** got out before you confirmed — no charge."
            )
            await ctx.send(embed=emb("🔓 Already Free", free_msg, C_GOLD))
            return
        if await get_balance(payer.id) < cost:
            await ctx.send(embed=emb(
                "❌ Insufficient Funds",
                f"Your balance dropped during confirmation — bail costs **{cost:,} 🪙**.",
                C_RED,
            ))
            return

        await deduct_balance(payer.id, cost)
        jdata["jail_until"] = 0
        jdata["jail_reason"] = None
        jdata["bail_amount"] = 0
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
    async def cmd_mug(self, ctx: commands.Context, target: MemberConverter = None, amount: str = None):
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

        if uid in self._crime_active:
            await ctx.send(embed=emb("⏳ Already Running", "You already have a crime in progress — wait for it to finish.", C_RED))
            return

        if target.id == uid:
            await ctx.send(embed=emb("❌ Self Mug", "You can't mug yourself!", C_RED))
            return
        if self.bot.user and target.id == self.bot.user.id:
            await ctx.send(embed=emb("❌ Invalid Target", "You can't mug the house.", C_RED))
            return

        await _ensure_user(target.id)
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

        jailed = uid not in state.godmode_users and random.random() < 0.5

        TRACK = 20
        steps = 8
        self._crime_active.add(uid)
        try:
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
                    msg = await ctx.send(embed=e)
                else:
                    await msg.edit(embed=e)
                await asyncio.sleep(0.6)

            actual_steal = min(parsed, target_bal)
            await deduct_balance(target.id, actual_steal)
            # Attacker paid `parsed` upfront (muggers' fee, charged via shop_charge);
            # victim loses `actual_steal`. Neither gains.
            await record_crime_event(uid, lost=parsed)
            await record_crime_event(target.id, lost=actual_steal)

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
            await msg.edit(embed=result_embed)
        finally:
            self._crime_active.discard(uid)

    @commands.command(name="records", aliases=["record", "rec"])
    async def cmd_records(self, ctx: commands.Context):
        """Display all-time records for economy and games."""
        if ctx.guild is None:
            await ctx.send(embed=emb("🏆 Records", "Records are only available in servers.", C_RED))
            return

        r = await load_records(ctx.guild.id)

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

        lines = [
            fmt("highest_balance", "Balance"),
            fmt("lottery", "Lottery Payout"),
            fmt("slots_jackpot", "Slots Jackpot",
                lambda rec: f"\n  ↳ Symbols: {rec.get('symbols', '?')} • Bet: {rec['bet']:,} 🪙" if rec.get('bet') is not None else ""),
            fmt("slots_non_jackpot", "Slots Non-Jackpot",
                lambda rec: f"\n  ↳ Symbols: {rec.get('symbols', '?')} • Bet: {rec['bet']:,} 🪙" if rec.get('bet') is not None else ""),
            fmt("blackjack", "Blackjack Payout",
                lambda rec: f"\n  ↳ Hand: {rec.get('player_hand', '?')} ({rec.get('player_score', '?')}) • Dealer: {rec.get('dealer_score', '?')}"),
            fmt("flip", "Flip Payout"),
            fmt("hangman_payout", "Hangman Payout",
                lambda rec: f"\n  ↳ Word: `{rec.get('word', '?')}`"),
            hm_wins_str,
        ]

        embed = discord.Embed(title="🏆 All-Time Records", color=C_GOLD)
        embed.description = "\n".join(lines)
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

        show_principals = action in ("principals", "principal")

        if action is None or action not in ("add", "remove"):
            value = await get_savings_value(uid)
            deposits = state.economy["users"][str(uid)].get("savings", [])
            if not deposits:
                desc = (
                    f"**{ctx.author.display_name}** has no savings yet.\n\n"
                    "**Usage:**\n"
                    "`!savings add <amount>` or `!savings +<amount>` — deposit coins\n"
                    "`!savings remove <amount>` or `!savings -<amount>` — withdraw coins\n\n"
                    "*Savings earn **1% compound interest per day**.*"
                )
            elif show_principals:
                now = time.time()
                principal = int(sum(e["amount"] for e in deposits))
                interest = int(value) - principal
                deposit_lines = []
                for e in deposits:
                    days = (now - e["deposited_at"]) / 86400.0
                    e_val = int(e["amount"] * (1.01 ** days))
                    e_principal = int(e["amount"])
                    e_interest = e_val - e_principal
                    date_str = datetime.datetime.fromtimestamp(e["deposited_at"]).strftime("%Y-%m-%d")
                    deposit_lines.append(
                        f"`{date_str}` — {e_principal:,} 🪙 (+{e_interest:,})"
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
                    "`!savings add <amount>` — deposit coins\n"
                    "`!savings remove <amount>` — withdraw coins\n"
                    "`!savings principals` — show deposit breakdown\n\n"
                    "*1% compound interest per day, compounded on each deposit separately.*"
                )
            await send_ephemeral(ctx, embed=emb("🐷 Piggy Bank", desc, C_GREEN))
            return

        parsed = await parse_amount(ctx, amount)
        if parsed is None:
            return
        if parsed <= 0:
            await ctx.send(embed=emb("❌ Invalid Amount", "Amount must be positive.", C_RED))
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
                total_savings += int(sum(e["amount"] * (1.01 ** ((now - e["deposited_at"]) / 86400.0)) for e in deps))
            if u.get("jail_until", 0) > now:
                jailed += 1

        total_circulation = total_wallets + total_savings
        lottery_pool = (await load_lottery(ctx.guild.id)).get("prize_pool", 0) if ctx.guild else 0
        house_bal = get_guild_house_balance(ctx.guild.id) if ctx.guild else 0
        total_users = len(users)

        stats = (
            f"**Total in wallets:** {total_wallets:,} 🪙\n"
            f"**Total in savings:** {total_savings:,} 🪙\n"
            f"**Total in circulation:** {total_circulation:,} 🪙\n"
            f"**Lottery pool:** {lottery_pool:,} 🪙\n"
            f"**House pot:** {house_bal:,} 🪙\n\n"
            f"**Users tracked:** {total_users:,}\n"
            f"**Users with savings:** {users_with_savings:,}\n"
            f"**Users in jail:** {jailed:,}\n\n"
            "**Economy commands:**\n"
            "`!balance [@user]` — Check wallet\n"
            "`!pay @user <amount>` — Send coins\n"
            f"{fmt_line('savings', '`!savings` — Piggy bank (1% daily interest)', uid_help, gid_help)}\n"
            "`!crime` — Steal, mug, jailbreak\n"
            "`!lottery` — Weekly lottery info\n"
            "`!shop` — Spend coins"
        )
        await send_ephemeral(ctx, embed=emb("📊 Economy", stats, C_GOLD))

    @commands.command(name="pay", aliases=["give", "gift", "donate"])
    async def cmd_pay(self, ctx: commands.Context, recipient: MemberConverter = None, amount: str = None):
        if recipient is None or amount is None:
            await ctx.send("Usage: `!pay @user <amount>`")
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
        try:
            amount = int(amount)
            if amount <= 0:
                raise ValueError
        except ValueError:
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
    async def cmd_give(self, ctx: commands.Context, target: MemberConverter = None, amount: str = None):
        if target is None or amount is None:
            await ctx.send(embed=emb("⚙️ Give", "Usage: `!give @user <amount>`", C_GREY))
            return
        try:
            amount = int(amount)
            if amount == 0:
                raise ValueError
            if amount < 0:
                if self.bot.user and target.id == self.bot.user.id:
                    amount = max(amount, -1 * get_guild_house_balance(ctx.guild.id if ctx.guild else 0))
                else:
                    amount = max(amount, -1 * await get_balance(target.id))
        except ValueError:
            await ctx.send(embed=emb("❌ Invalid Amount", "Please provide a non-zero whole number.", C_RED))
            return
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
    async def cmd_givexp(self, ctx: commands.Context, target: MemberConverter = None, amount: str = None):
        if target is None or amount is None:
            await ctx.send(embed=emb("⚙️ Give XP", "Usage: `!admingivexp @user <amount>`", C_GREY))
            return
        if not ctx.guild:
            await ctx.send(embed=emb("❌ Guild Only", "This command can only be used in a server.", C_RED))
            return
        try:
            amount = int(amount)
            if amount == 0:
                raise ValueError
        except ValueError:
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
