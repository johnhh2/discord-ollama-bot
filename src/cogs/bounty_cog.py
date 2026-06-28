"""Honor-based bounties: !shop bounty / !bounty.

An author posts a coin bounty (escrowed at creation) with a free-text
condition in the guild's configured bounty channel. Lifecycle, all driven by
reactions so it survives reboots (rows are persisted; on_raw_reaction_add looks
them up by message id):

  1. open      — red embed with a 🙋 claim reaction. The AUTHOR reacting 🙋
                 cancels the bounty and refunds their escrow. Any OTHER user
                 reacting 🙋 submits a claim → status `pending`.
  2. pending   — embed turns yellow and logs the submitter. The author is DM'd
                 the claim with ✅ accept / ❌ reject reactions and a 1-week
                 deadline (Discord relative timestamp).
                   • accept → pay the claimant, embed turns green (terminal).
                   • reject → claimant is DM'd a "contest?" offer (✅/❌, 3-day
                     deadline) → status `contesting`.
                   • no answer in 1 week → treated as a silent reject; the
                     claimant still gets the contest offer.
  3. contesting — claimant ✅ → an @everyone poll is posted in the bounty
                 channel (status `polling`); claimant ❌ or 3-day timeout →
                 embed turns red, escrow refunded to author (terminal reject).
  4. polling   — 3-day ✅/❌ poll. yes-ratio (excluding author & claimant)
                 <50% → no payout (refund author); 50%→50% payout ramping
                 linearly to ≥66.6%→100% payout. Embed turns green on any
                 payout, red on none (terminal).

Escrow is real coins: deducted from the author at creation, paid to the
claimant on a win (full or partial), and refunded to the author on
cancel / reject / expiry / poll-loss. A partial poll payout splits between
claimant and an author refund.
"""
import logging
import time

import discord
from discord.ext import commands, tasks

from src.helpers import emb, C_GREEN, C_RED, C_GOLD, C_GREY, parse_int_amount
from src.economy import add_balance, deduct_balance, get_balance
from src.guild_config import get_guild_cfg
from src.permissions import _wrong_channel_reply
from src.persistence import (
    insert_bounty, get_bounty_by_message, get_bounty_by_dm,
    get_bounty_by_contest, update_bounty,
)
from src.config import (
    BOUNTY_MIN_AMOUNT, BOUNTY_CLAIM_DURATION_SECS,
    BOUNTY_CONTEST_DURATION_SECS, BOUNTY_POLL_DURATION_SECS,
    BOUNTY_POLL_MIN_RATIO, BOUNTY_POLL_FULL_RATIO,
)
from src import state

CLAIM_EMOJI = "🙋"
ACCEPT_EMOJI = "✅"
REJECT_EMOJI = "❌"


# ── Embed rendering ───────────────────────────────────────────────────────────
def _status_color(status: str) -> int:
    return {
        "open": C_RED,
        "pending": C_GOLD,
        "contesting": C_GOLD,
        "polling": C_GOLD,
        "accepted": C_GREEN,
        "cancelled": C_GREY,
        "rejected": C_RED,
    }.get(status, C_RED)


def render_bounty_embed(bounty: dict) -> discord.Embed:
    """Build the channel embed for a bounty from its persisted row. The color
    encodes the lifecycle: red open, yellow in-progress, green paid, red/grey
    terminal. The claim log is appended at the bottom."""
    status = bounty["status"]
    amount = bounty["amount"]
    e = discord.Embed(
        title="🎯 Bounty",
        description=(
            f"**Reward:** {amount:,} 🪙\n"
            f"**Posted by:** <@{bounty['author_id']}>\n\n"
            f"**Condition:**\n{bounty['condition']}"
        ),
        color=_status_color(status),
    )
    if status == "open":
        e.set_footer(text=f"React {CLAIM_EMOJI} to claim this bounty. The author can react to cancel & refund.")
    elif status == "pending":
        e.set_footer(text="A claim is under review by the author.")
    elif status == "contesting":
        e.set_footer(text="Claim rejected — awaiting the claimant's contest decision.")
    elif status == "polling":
        e.set_footer(text="Contested — the community is voting on this bounty.")
    elif status == "accepted":
        e.set_footer(text="Bounty paid out. ✅")
    elif status == "cancelled":
        e.set_footer(text="Bounty cancelled — reward refunded to the author.")
    elif status == "rejected":
        e.set_footer(text="Bounty closed — no payout.")

    log = bounty.get("claim_log") or []
    if log:
        e.add_field(name="Claims", value="\n".join(log[-10:]), inline=False)
    return e


class BountyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if bot is not None:
            self._expiry_loop.start()

    def cog_unload(self):
        if self.bot is not None:
            self._expiry_loop.cancel()

    # ── Helpers ───────────────────────────────────────────────────────────────
    async def _refresh_embed(self, bounty: dict):
        """Re-render the channel embed for a bounty from its current row."""
        try:
            channel = self.bot.get_channel(bounty["channel_id"]) or await self.bot.fetch_channel(bounty["channel_id"])
            msg = await channel.fetch_message(bounty["message_id"])
            await msg.edit(embed=render_bounty_embed(bounty))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] failed to refresh embed %s: %s", bounty["message_id"], ex)

    async def _clear_claim_reaction(self, bounty: dict):
        """Remove the 🙋 claim reaction from a bounty's channel embed once it's
        no longer claimable (claimed/cancelled/etc.), so it doesn't keep
        inviting reactions. Best-effort — needs Manage Messages to clear other
        users' reactions; falls back to removing just the bot's own reaction."""
        try:
            channel = self.bot.get_channel(bounty["channel_id"]) or await self.bot.fetch_channel(bounty["channel_id"])
            msg = await channel.fetch_message(bounty["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] failed to fetch message to clear reactions %s: %s", bounty["message_id"], ex)
            return
        try:
            await msg.clear_reaction(CLAIM_EMOJI)
        except discord.Forbidden:
            # No Manage Messages — at least drop the bot's own 🙋 so the count
            # ticks down and the prompt is less inviting.
            try:
                await msg.remove_reaction(CLAIM_EMOJI, self.bot.user)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        except (discord.HTTPException, discord.NotFound) as ex:
            logging.warning("[bounty] failed to clear claim reaction %s: %s", bounty["message_id"], ex)

    async def _persist_and_cache(self, message_id: int, bounty: dict, **fields):
        """Apply `fields` to the in-memory bounty dict, persist them, and keep
        state.active_bounties in sync. Pass the bounty dict so the in-memory
        copy stays authoritative for the next reaction."""
        bounty.update(fields)
        await update_bounty(message_id, **fields)
        if bounty["status"] in ("accepted", "rejected", "cancelled"):
            state.active_bounties.pop(message_id, None)
        else:
            state.active_bounties[message_id] = bounty

    async def _dm(self, user_id: int, embed: discord.Embed, reactions: list[str]) -> discord.Message | None:
        """DM `embed` to a user and seed `reactions`. Returns the message, or
        None if the user has DMs closed."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            dm = await user.send(embed=embed)
            for r in reactions:
                await dm.add_reaction(r)
            return dm
        except (discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] DM to %s failed: %s", user_id, ex)
            return None

    # ── Command entry point (called by !shop bounty and !bounty) ──────────────
    async def create_bounty(self, ctx: commands.Context, args: tuple[str, ...]):
        """Validate, escrow, and post a new bounty. Shared by the !shop
        subcommand and the !bounty top-level alias."""
        if ctx.guild is None:
            await ctx.send(embed=emb("🎯 Bounty", "Bounties only work in servers.", C_RED))
            return

        cfg = get_guild_cfg(ctx.guild.id)
        bounty_channel_id = cfg.get("bounty_channel")
        if not bounty_channel_id:
            await ctx.send(embed=emb(
                "🎯 Bounties Not Enabled",
                "No bounty channel is configured. A server admin can set one with "
                "`!settings bounty-channel #channel`.",
                C_GREY,
            ))
            return
        if ctx.channel.id != bounty_channel_id:
            await _wrong_channel_reply(
                ctx, f"Bounties can only be posted in <#{bounty_channel_id}>."
            )
            return

        if len(args) < 2:
            await ctx.send(embed=emb(
                "🎯 Bounty",
                "Usage: `!bounty <coins> <condition>`\n"
                "Example: `!bounty 5k give Joseph a new nickname`",
                C_GOLD,
            ))
            return

        amount = parse_int_amount(args[0])
        if amount is None or amount < BOUNTY_MIN_AMOUNT:
            await ctx.send(embed=emb(
                "🎯 Bounty",
                f"Bounty amount must be a positive number of at least "
                f"**{BOUNTY_MIN_AMOUNT:,} 🪙** (e.g. `1k`, `5000`).",
                C_RED,
            ))
            return

        condition = " ".join(args[1:]).strip()
        if not condition:
            await ctx.send(embed=emb("🎯 Bounty", "Describe what the bounty is for.", C_GOLD))
            return
        if len(condition) > 1500:
            await ctx.send(embed=emb("🎯 Bounty", "Condition is too long (max 1500 characters).", C_RED))
            return

        uid = ctx.author.id
        # Escrow the reward up front. deduct_balance is a single atomic sync
        # mutation, so concurrent !bounty invocations can't both pass on a thin
        # balance — one wins, the other gets insufficient-funds.
        if uid not in state.godmode_users:
            if not await deduct_balance(uid, amount):
                await ctx.send(embed=emb(
                    "💸 Insufficient Funds",
                    f"A bounty of **{amount:,} 🪙** needs to be held in escrow. "
                    f"Balance: {await get_balance(uid):,} 🪙",
                    C_RED,
                ))
                return

        # Post the embed, then persist keyed by its message id. If the post
        # fails, refund the escrow.
        bounty = {
            "guild_id": ctx.guild.id, "channel_id": ctx.channel.id,
            "author_id": uid, "amount": amount, "condition": condition,
            "status": "open", "claimant_id": None, "claim_log": [],
            "dm_message_id": None, "contest_message_id": None,
            "poll_message_id": None, "poll_channel_id": None,
            "claim_expires_at": None, "contest_expires_at": None,
            "poll_expires_at": None,
        }
        try:
            msg = await ctx.send(embed=render_bounty_embed({**bounty, "message_id": 0}))
            await msg.add_reaction(CLAIM_EMOJI)
        except discord.HTTPException as ex:
            if uid not in state.godmode_users:
                await add_balance(uid, amount)
            logging.warning("[bounty] post failed, escrow refunded: %s", ex)
            await ctx.send(embed=emb("🎯 Bounty", "Couldn't post the bounty — your coins were refunded.", C_RED))
            return

        bounty["message_id"] = msg.id
        await insert_bounty(
            guild_id=ctx.guild.id, channel_id=ctx.channel.id, message_id=msg.id,
            author_id=uid, amount=amount, condition=condition,
        )
        state.active_bounties[msg.id] = bounty

    # ── Reaction dispatch ─────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        emoji = str(payload.emoji)

        # 1) Claim reaction on the bounty embed itself (in a guild channel).
        if payload.guild_id is not None and emoji == CLAIM_EMOJI:
            bounty = state.active_bounties.get(payload.message_id)
            if bounty is None:
                bounty = await get_bounty_by_message(payload.message_id)
                if bounty is None or bounty["status"] not in ("open", "pending", "contesting", "polling"):
                    return
            if bounty["status"] == "open":
                await self._handle_claim_reaction(bounty, payload.user_id)
            return

        # 2) Accept/reject on the author's claim DM (no guild_id in DMs).
        if payload.guild_id is None and emoji in (ACCEPT_EMOJI, REJECT_EMOJI):
            bounty = await get_bounty_by_dm(payload.message_id)
            if bounty is not None and bounty["status"] == "pending" and payload.user_id == bounty["author_id"]:
                await self._resolve_claim(bounty, accepted=(emoji == ACCEPT_EMOJI))
                return
            # 3) Contest offer DM to the rejected claimant.
            bounty = await get_bounty_by_contest(payload.message_id)
            if bounty is not None and bounty["status"] == "contesting" and payload.user_id == bounty["claimant_id"]:
                await self._resolve_contest(bounty, contested=(emoji == ACCEPT_EMOJI))
                return

    async def _handle_claim_reaction(self, bounty: dict, user_id: int):
        """A 🙋 on an open bounty. Author → cancel+refund. Other user → claim."""
        mid = bounty["message_id"]
        # Re-read the live in-memory status as the gate. The claim below is a
        # synchronous state flip before any await, so two near-simultaneous
        # claimers can't both pass: the first flips status off "open", the
        # second sees "pending" and bails.
        live = state.active_bounties.get(mid, bounty)
        if live["status"] != "open":
            return

        if user_id == bounty["author_id"]:
            # Author cancels — refund escrow, terminal.
            live["status"] = "cancelled"  # claim sync, before await
            if bounty["author_id"] not in state.godmode_users:
                await add_balance(bounty["author_id"], bounty["amount"])
            await self._persist_and_cache(mid, live, status="cancelled")
            await self._refresh_embed(live)
            await self._clear_claim_reaction(live)
            return

        # Another user submits a claim.
        claim_exp = time.time() + BOUNTY_CLAIM_DURATION_SECS
        log = list(live.get("claim_log") or [])
        log.append(f"🙋 <@{user_id}> submitted a claim <t:{int(time.time())}:R>")
        live["status"] = "pending"  # claim sync, before await
        live["claimant_id"] = user_id

        # DM the author with accept/reject.
        dm_embed = emb(
            "🎯 Bounty Claim",
            f"<@{user_id}> claims they completed your **{bounty['amount']:,} 🪙** bounty:\n\n"
            f"**{bounty['condition']}**\n\n"
            f"React {ACCEPT_EMOJI} to **accept** (pays them) or {REJECT_EMOJI} to **reject**.\n"
            f"This claim expires <t:{int(claim_exp)}:R>.",
            C_GOLD,
        )
        dm = await self._dm(bounty["author_id"], dm_embed, [ACCEPT_EMOJI, REJECT_EMOJI])
        await self._persist_and_cache(
            mid, live, status="pending", claimant_id=user_id,
            dm_message_id=(dm.id if dm else None),
            claim_expires_at=claim_exp, claim_log=log,
        )
        await self._refresh_embed(live)
        # Bounty is now claimed (pending review) — drop the 🙋 so nobody else
        # tries to claim it while the author reviews.
        await self._clear_claim_reaction(live)

        # If the author's DMs are closed, the claim can still be settled from the
        # channel side later; surface a hint in the channel.
        if dm is None:
            try:
                channel = self.bot.get_channel(bounty["channel_id"])
                if channel:
                    await channel.send(embed=emb(
                        "🎯 Bounty Claim",
                        f"<@{bounty['author_id']}> — <@{user_id}> claimed your bounty, but I "
                        f"couldn't DM you. Open your DMs to review it before it expires "
                        f"<t:{int(claim_exp)}:R>.",
                        C_GOLD,
                    ))
            except discord.HTTPException:
                pass

    async def _resolve_claim(self, bounty: dict, accepted: bool):
        """Author accepted or rejected a pending claim."""
        mid = bounty["message_id"]
        live = state.active_bounties.get(mid, bounty)
        if live["status"] != "pending":
            return
        claimant_id = live["claimant_id"]
        log = list(live.get("claim_log") or [])

        if accepted:
            live["status"] = "accepted"  # claim sync, before await
            await add_balance(claimant_id, bounty["amount"], guild_id=bounty["guild_id"])
            log.append(f"✅ Author accepted <@{claimant_id}>'s claim — paid {bounty['amount']:,} 🪙")
            await self._persist_and_cache(mid, live, status="accepted", claim_log=log)
            await self._refresh_embed(live)
            await self._dm(
                claimant_id,
                emb("🎯 Bounty Accepted",
                    f"Your claim was accepted! **{bounty['amount']:,} 🪙** has been paid to you.",
                    C_GREEN),
                [],
            )
            return

        # Rejected — offer the claimant a chance to contest.
        contest_exp = time.time() + BOUNTY_CONTEST_DURATION_SECS
        live["status"] = "contesting"  # claim sync, before await
        log.append(f"❌ Author rejected <@{claimant_id}>'s claim")
        contest_embed = emb(
            "🎯 Claim Rejected",
            f"The author rejected your claim on their **{bounty['amount']:,} 🪙** bounty:\n\n"
            f"**{bounty['condition']}**\n\n"
            f"React {ACCEPT_EMOJI} to **contest** (starts a community vote) or {REJECT_EMOJI} to **drop it**.\n"
            f"This offer expires <t:{int(contest_exp)}:R>.",
            C_GOLD,
        )
        dm = await self._dm(claimant_id, contest_embed, [ACCEPT_EMOJI, REJECT_EMOJI])
        await self._persist_and_cache(
            mid, live, status="contesting",
            contest_message_id=(dm.id if dm else None),
            contest_expires_at=contest_exp, claim_log=log,
        )
        await self._refresh_embed(live)

    async def _resolve_contest(self, bounty: dict, contested: bool):
        """Rejected claimant chose to contest (→ poll) or drop it (→ refund)."""
        mid = bounty["message_id"]
        live = state.active_bounties.get(mid, bounty)
        if live["status"] != "contesting":
            return

        if not contested:
            await self._settle_reject(live, note="Claimant dropped the contest")
            return

        await self._start_poll(live)

    async def _start_poll(self, bounty: dict):
        """Post an @everyone poll in the bounty channel and move to `polling`."""
        mid = bounty["message_id"]
        claimant_id = bounty["claimant_id"]
        poll_exp = time.time() + BOUNTY_POLL_DURATION_SECS
        try:
            channel = self.bot.get_channel(bounty["channel_id"]) or await self.bot.fetch_channel(bounty["channel_id"])
            poll_embed = emb(
                "🎯 Bounty Dispute — Community Vote",
                f"<@{claimant_id}> contests the rejection of this **{bounty['amount']:,} 🪙** bounty:\n\n"
                f"**{bounty['condition']}**\n\n"
                f"Did they complete it?\n"
                f"{ACCEPT_EMOJI} = completed   {REJECT_EMOJI} = not completed\n\n"
                f"Voting closes <t:{int(poll_exp)}:R>. (The author and claimant's votes don't count.)\n"
                f"≥50% yes pays out partially, ≥66.6% pays in full.",
                C_GOLD,
            )
            poll_msg = await channel.send(
                content="@everyone",
                embed=poll_embed,
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
            await poll_msg.add_reaction(ACCEPT_EMOJI)
            await poll_msg.add_reaction(REJECT_EMOJI)
        except discord.HTTPException as ex:
            logging.warning("[bounty] failed to start poll for %s: %s", mid, ex)
            # Couldn't poll — fall back to refunding the author (terminal).
            await self._settle_reject(bounty, note="Could not start contest poll")
            return

        log = list(bounty.get("claim_log") or [])
        log.append(f"🗳️ <@{claimant_id}> contested — community vote started")
        bounty["status"] = "polling"
        await self._persist_and_cache(
            mid, bounty, status="polling",
            poll_message_id=poll_msg.id, poll_channel_id=channel.id,
            poll_expires_at=poll_exp, claim_log=log,
        )
        await self._refresh_embed(bounty)

    async def _tally_poll(self, bounty: dict):
        """Count the poll reactions (excluding author & claimant) and settle."""
        mid = bounty["message_id"]
        author_id, claimant_id = bounty["author_id"], bounty["claimant_id"]
        yes = no = 0
        try:
            channel = self.bot.get_channel(bounty["poll_channel_id"]) or await self.bot.fetch_channel(bounty["poll_channel_id"])
            poll_msg = await channel.fetch_message(bounty["poll_message_id"])
            for reaction in poll_msg.reactions:
                if str(reaction.emoji) not in (ACCEPT_EMOJI, REJECT_EMOJI):
                    continue
                async for voter in reaction.users():
                    if voter.bot or voter.id in (author_id, claimant_id):
                        continue
                    if str(reaction.emoji) == ACCEPT_EMOJI:
                        yes += 1
                    else:
                        no += 1
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] poll tally fetch failed for %s: %s", mid, ex)

        total = yes + no
        ratio = (yes / total) if total else 0.0
        payout_frac = _poll_payout_fraction(ratio)
        await self._settle_poll(bounty, yes=yes, no=no, payout_frac=payout_frac)

    async def _settle_poll(self, bounty: dict, yes: int, no: int, payout_frac: float):
        """Apply the poll result: pay the claimant a fraction, refund the rest
        to the author. payout_frac 0 → full refund, terminal reject."""
        mid = bounty["message_id"]
        amount = bounty["amount"]
        claimant_id = bounty["claimant_id"]
        log = list(bounty.get("claim_log") or [])

        if payout_frac <= 0:
            log.append(f"🗳️ Vote failed ({yes}✅/{no}❌) — no payout, author refunded")
            bounty["status"] = "rejected"
            if bounty["author_id"] not in state.godmode_users:
                await add_balance(bounty["author_id"], amount)
            await self._persist_and_cache(mid, bounty, status="rejected", claim_log=log)
            await self._refresh_embed(bounty)
            await self._dm(claimant_id, emb(
                "🎯 Bounty Vote", f"The community vote failed ({yes}✅/{no}❌). No payout.", C_RED), [])
            return

        payout = int(amount * payout_frac)
        refund = amount - payout
        log.append(f"🗳️ Vote passed ({yes}✅/{no}❌) — paid {payout:,} 🪙 ({int(payout_frac*100)}%) to <@{claimant_id}>")
        bounty["status"] = "accepted"
        if payout > 0:
            await add_balance(claimant_id, payout, guild_id=bounty["guild_id"])
        if refund > 0 and bounty["author_id"] not in state.godmode_users:
            await add_balance(bounty["author_id"], refund)
        await self._persist_and_cache(mid, bounty, status="accepted", claim_log=log)
        await self._refresh_embed(bounty)
        await self._dm(claimant_id, emb(
            "🎯 Bounty Vote Won",
            f"The community voted in your favor ({yes}✅/{no}❌). "
            f"You were paid **{payout:,} 🪙** ({int(payout_frac*100)}% of the bounty).",
            C_GREEN), [])

    async def _settle_reject(self, bounty: dict, note: str):
        """Terminal reject: refund the author, turn the embed red."""
        mid = bounty["message_id"]
        log = list(bounty.get("claim_log") or [])
        log.append(f"❌ {note} — author refunded {bounty['amount']:,} 🪙")
        bounty["status"] = "rejected"
        if bounty["author_id"] not in state.godmode_users:
            await add_balance(bounty["author_id"], bounty["amount"])
        await self._persist_and_cache(mid, bounty, status="rejected", claim_log=log)
        await self._refresh_embed(bounty)

    # ── Expiry loop ───────────────────────────────────────────────────────────
    @tasks.loop(minutes=2)
    async def _expiry_loop(self):
        """Settle timed-out claims, contests, and polls. Cheap: iterates the
        in-memory active set, acts only on rows past their deadline."""
        now = time.time()
        # Copy values: settlement mutates state.active_bounties.
        for bounty in list(state.active_bounties.values()):
            try:
                status = bounty["status"]
                if status == "pending":
                    exp = bounty.get("claim_expires_at")
                    if exp and now >= exp:
                        # Author never answered → silent reject, but still offer
                        # the claimant a contest (mirrors an explicit reject).
                        await self._auto_reject_unanswered(bounty)
                elif status == "contesting":
                    exp = bounty.get("contest_expires_at")
                    if exp and now >= exp:
                        await self._settle_reject(bounty, note="Contest offer expired")
                elif status == "polling":
                    exp = bounty.get("poll_expires_at")
                    if exp and now >= exp:
                        await self._tally_poll(bounty)
            except Exception:
                logging.exception("[bounty] expiry settlement failed for %s", bounty.get("message_id"))

    async def _auto_reject_unanswered(self, bounty: dict):
        """A pending claim the author never answered in 1 week. Treat as a
        rejection and offer the claimant the same contest path."""
        mid = bounty["message_id"]
        live = state.active_bounties.get(mid, bounty)
        if live["status"] != "pending":
            return
        contest_exp = time.time() + BOUNTY_CONTEST_DURATION_SECS
        live["status"] = "contesting"
        log = list(live.get("claim_log") or [])
        log.append("⌛ Author didn't respond in time — claim auto-rejected")
        dm = await self._dm(
            live["claimant_id"],
            emb("🎯 Claim Expired",
                f"The author didn't respond to your claim on their **{bounty['amount']:,} 🪙** bounty in time.\n\n"
                f"React {ACCEPT_EMOJI} to **contest** (community vote) or {REJECT_EMOJI} to **drop it**.\n"
                f"This offer expires <t:{int(contest_exp)}:R>.",
                C_GOLD),
            [ACCEPT_EMOJI, REJECT_EMOJI],
        )
        await self._persist_and_cache(
            mid, live, status="contesting",
            contest_message_id=(dm.id if dm else None),
            contest_expires_at=contest_exp, claim_log=log,
        )
        await self._refresh_embed(live)

    @_expiry_loop.before_loop
    async def _before_expiry(self):
        await self.bot.wait_until_ready()
        import src.persistence as _pkg
        await _pkg.init_done.wait()

    # ── Command surface ───────────────────────────────────────────────────────
    @commands.command(name="bounty")
    async def cmd_bounty(self, ctx: commands.Context, *args):
        await self.create_bounty(ctx, args)


def _poll_payout_fraction(ratio: float) -> float:
    """Map a yes-vote ratio to a payout fraction.

    <50%        → 0 (no payout)
    50%         → 0.5 (half payout)
    50%–66.6%   → linear ramp from 0.5 to 1.0
    ≥66.6%      → 1.0 (full payout)
    """
    if ratio < BOUNTY_POLL_MIN_RATIO:
        return 0.0
    if ratio >= BOUNTY_POLL_FULL_RATIO:
        return 1.0
    span = BOUNTY_POLL_FULL_RATIO - BOUNTY_POLL_MIN_RATIO
    return 0.5 + 0.5 * (ratio - BOUNTY_POLL_MIN_RATIO) / span


async def setup(bot):
    await bot.add_cog(BountyCog(bot))
