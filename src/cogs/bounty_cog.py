"""Honor-based bounties: !shop bounty / !bounty.

An author posts a coin bounty (escrowed at creation) with a free-text condition,
and an OPTIONAL expiration (`!bounty 5k 7d wash my car`), in the guild's
configured bounty channel. Everything is reaction-driven and persisted, so the
whole flow survives a reboot (on_raw_reaction_add resolves rows by message id).

A bounty stays **open** and supports **several concurrent claims**:

  • 🙋 by a non-author → a new claim. The author is DM'd ✅ accept / ❌ reject
    with a 1-week deadline. Multiple users can have claims in flight at once;
    each is independent. A user gets at most ONE claim per bounty, ever (a
    rejected claim does not let them re-claim).
  • 🙋 by the author → cancels the bounty and refunds 90% — but ONLY if the
    bounty has no deadline. A bounty with a deadline can't be self-cancelled.

Per claim:
  • author ✅  → pay that claimant in full, bounty → accepted (terminal). Every
    sibling claim is voided; any live contest poll is edited to "void" and
    skipped at tally.
  • author ❌ (or 1-week no-answer) → the claimant is DM'd a contest offer
    (✅ contest / ❌ drop, 3-day deadline).
      – drop / 3-day timeout → claim rejected; the BOUNTY STAYS OPEN.
      – contest → a community poll is posted (3-day). Votes are recorded in
        bounty_claims.poll_votes as ✅/❌ reactions come and go, and the poll
        embed shows that live tally — the tracked set (not the reactions still
        on the message at close, which can be cleared or lost) is what gets
        counted; a close-time reaction scan only backfills votes cast while the
        bot was offline. yes-ratio (excluding author & claimant) <50% → claim
        rejected, bounty stays open; 50%→50% payout ramping to ≥66.6%→100% →
        pay claimant that fraction, bounty accepted (voids siblings). No author
        refund on a partial poll — the unpaid remainder is a house cut. Every
        eligible voter (not the author or claimant) is paid a flat
        BOUNTY_POLL_VOTER_REWARD when the poll closes, regardless of how they
        voted.

Open bounties with a deadline auto-close when it passes: refund the author 90%,
bounty → expired, all in-flight claims voided.

Escrow is real coins: deducted from the author at creation; paid to a winning
claimant; refunded (90%) to the author only when the author walks away
(self-cancel or deadline expiry).
"""
import logging
import time

import discord
from discord.ext import commands, tasks

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_GREY,
    parse_int_amount, parse_duration,
)
from src.confirm_view import confirm_purchase
from src.economy import add_balance, deduct_balance, get_balance
from src.guild_config import get_guild_cfg
from src.permissions import _wrong_channel_reply, is_silenced
from src.persistence import (
    insert_bounty, get_bounty_by_message, update_bounty,
    insert_claim, update_claim,
    get_claim_by_dm, get_claim_by_contest,
)
from src.config import (
    BOUNTY_MIN_AMOUNT, BOUNTY_CLAIM_DURATION_SECS,
    BOUNTY_CONTEST_DURATION_SECS, BOUNTY_POLL_DURATION_SECS,
    BOUNTY_POLL_MIN_RATIO, BOUNTY_POLL_FULL_RATIO,
    BOUNTY_AUTHOR_REFUND_FRACTION, BOUNTY_POLL_VOTER_REWARD,
)
from src import state

CLAIM_EMOJI = "🙋"
ACCEPT_EMOJI = "✅"
REJECT_EMOJI = "❌"

# Bounty-level terminal states (dropped from state.active_bounties).
_BOUNTY_TERMINAL = ("accepted", "cancelled", "expired")
# Claim-level non-terminal states (still drive reactions / expiry).
_CLAIM_ACTIVE = ("pending", "contesting", "polling")


# ── Embed rendering ───────────────────────────────────────────────────────────
def _bounty_color(status: str) -> int:
    return {
        "open": C_RED,
        "accepted": C_GREEN,
        "cancelled": C_GREY,
        "expired": C_GREY,
    }.get(status, C_RED)


def render_bounty_embed(bounty: dict) -> discord.Embed:
    """Build the channel embed for a bounty from its row. Red = open, green =
    paid out, grey = cancelled/expired. The claim log and (if set) the expiry
    timestamp render at the bottom."""
    status = bounty["status"]
    amount = bounty["amount"]
    desc = (
        f"**Reward:** {amount:,} 🪙\n"
        f"**Posted by:** <@{bounty['author_id']}>\n\n"
        f"**Condition:**\n{bounty['condition']}"
    )
    expires_at = bounty.get("expires_at")
    if expires_at and status == "open":
        desc += f"\n\n**Expires:** <t:{int(expires_at)}:R>"
    e = discord.Embed(title="🎯 Bounty", description=desc, color=_bounty_color(status))

    if status == "open":
        footer = f"React {CLAIM_EMOJI} to claim."
        if expires_at:
            footer += " Expires automatically if unclaimed."
        e.set_footer(text=footer)
    elif status == "accepted":
        e.set_footer(text="Bounty paid out. ✅")
    elif status == "cancelled":
        e.set_footer(text="Bounty cancelled — reward refunded to the author.")
    elif status == "expired":
        e.set_footer(text="Bounty expired unclaimed — reward refunded to the author.")

    log = bounty.get("claim_log") or []
    if log:
        e.add_field(name="Activity", value="\n".join(log[-10:]), inline=False)
    return e


def _poll_vote_counts(votes: "dict | None") -> tuple[int, int]:
    """(yes, no) from a tracked-vote dict. A both-ways voter previously added
    1 to each side, dragging the ratio toward 50% — which is a payout
    threshold. Their votes cancel: they count for neither side."""
    yes_set = set((votes or {}).get("yes", []))
    no_set = set((votes or {}).get("no", []))
    both = yes_set & no_set
    return len(yes_set - both), len(no_set - both)


def render_poll_embed(bounty: dict, claimant_id: int, poll_expires_at: float,
                      yes: int, no: int) -> discord.Embed:
    """Build the contest-poll embed, including the bot-computed live tally.
    The tally line is the authoritative count (eligible voters only, both-ways
    votes cancelled) — the raw reaction counts include the bot's seed
    reactions and ineligible voters, so they always read wrong."""
    return emb(
        "🎯 Bounty Dispute — Community Vote",
        f"<@{claimant_id}> contests the rejection of this **{bounty['amount']:,} 🪙** bounty:\n\n"
        f"**{bounty['condition']}**\n\n"
        f"Did they complete it?\n"
        f"{ACCEPT_EMOJI} = completed   {REJECT_EMOJI} = not completed\n\n"
        f"**Current tally: {yes} {ACCEPT_EMOJI} · {no} {REJECT_EMOJI}**\n\n"
        f"Voting closes <t:{int(poll_expires_at)}:R>. (The author and claimant's votes don't count.)\n"
        f"≥50% yes pays out partially, ≥66.6% pays in full.\n\n"
        f"🪙 Vote and you'll get **{BOUNTY_POLL_VOTER_REWARD:,} 🪙** when the poll closes!",
        C_GOLD)


class BountyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if bot is not None:
            self._expiry_loop.start()

    def cog_unload(self):
        if self.bot is not None:
            self._expiry_loop.cancel()

    # ── Coin helpers ──────────────────────────────────────────────────────────
    async def _refund_author(self, bounty: dict, fraction: float):
        """Refund the author `fraction` of the escrow (godmode authors paid
        nothing, so they get nothing back)."""
        if bounty["author_id"] in state.godmode_users:
            return 0
        refund = int(bounty["amount"] * fraction)
        if refund > 0:
            await add_balance(bounty["author_id"], refund)
        return refund

    # ── Message helpers ───────────────────────────────────────────────────────
    async def _refresh_embed(self, bounty: dict):
        """Re-render the channel embed for a bounty from its current row."""
        try:
            channel = self.bot.get_channel(bounty["channel_id"]) or await self.bot.fetch_channel(bounty["channel_id"])
            msg = await channel.fetch_message(bounty["message_id"])
            await msg.edit(embed=render_bounty_embed(bounty))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] failed to refresh embed %s: %s", bounty["message_id"], ex)

    async def _clear_claim_reaction(self, bounty: dict):
        """Remove the 🙋 claim reaction from a bounty embed once it's terminal,
        so it's no longer invitingly claimable. Best-effort."""
        try:
            channel = self.bot.get_channel(bounty["channel_id"]) or await self.bot.fetch_channel(bounty["channel_id"])
            msg = await channel.fetch_message(bounty["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] fetch to clear reactions %s: %s", bounty["message_id"], ex)
            return
        try:
            await msg.clear_reaction(CLAIM_EMOJI)
        except discord.Forbidden:
            try:
                await msg.remove_reaction(CLAIM_EMOJI, self.bot.user)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        except (discord.HTTPException, discord.NotFound) as ex:
            logging.warning("[bounty] failed to clear claim reaction %s: %s", bounty["message_id"], ex)

    async def _dm(self, user_id: int, embed: discord.Embed, reactions: list[str]) -> discord.Message | None:
        """DM `embed` to a user and seed `reactions`. None if DMs are closed."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            dm = await user.send(embed=embed)
            for r in reactions:
                await dm.add_reaction(r)
            return dm
        except (discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] DM to %s failed: %s", user_id, ex)
            return None

    # ── State helpers ─────────────────────────────────────────────────────────
    def _cached_bounty(self, message_id: int) -> "dict | None":
        return state.active_bounties.get(message_id)

    @staticmethod
    def _find_claim(bounty: dict, claim_id: int) -> "dict | None":
        for c in bounty.get("claims", []):
            if c["id"] == claim_id:
                return c
        return None

    @staticmethod
    def _claim_for_user(bounty: dict, user_id: int) -> "dict | None":
        for c in bounty.get("claims", []):
            if c["claimant_id"] == user_id:
                return c
        return None

    async def _persist_bounty(self, bounty: dict, **fields):
        """Patch bounty-level fields, persist, and sync the active cache. Drops
        the bounty from the cache once it reaches a terminal state."""
        bounty.update(fields)
        await update_bounty(bounty["message_id"], **fields)
        if bounty["status"] in _BOUNTY_TERMINAL:
            state.active_bounties.pop(bounty["message_id"], None)
        else:
            state.active_bounties[bounty["message_id"]] = bounty

    async def _persist_claim(self, claim: dict, **fields):
        """Patch claim-level fields and persist them."""
        claim.update(fields)
        await update_claim(claim["id"], **fields)

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

        usage = (
            "Usage: `!bounty <coins> [duration] <condition>`\n"
            "Examples: `!bounty 5k give Joseph a new nickname`\n"
            "`!bounty 5k 7d watch this video and give honest thoughts`"
        )
        if len(args) < 2:
            await ctx.send(embed=emb("🎯 Bounty", usage, C_GOLD))
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

        # Optional leading duration: the 2nd token is the expiry only if it
        # parses as a duration AND there's still condition text after it. This
        # mirrors `!shop spellcheck @user [days]`. A condition that genuinely
        # starts with a duration-like word can be reordered to avoid the clash.
        rest = list(args[1:])
        expires_at = None
        duration_secs = parse_duration(rest[0])
        if duration_secs is not None and len(rest) >= 2:
            expires_at = time.time() + duration_secs
            rest = rest[1:]

        condition = " ".join(rest).strip()
        if not condition:
            await ctx.send(embed=emb("🎯 Bounty", "Describe what the bounty is for.\n\n" + usage, C_GOLD))
            return
        if len(condition) > 1500:
            await ctx.send(embed=emb("🎯 Bounty", "Condition is too long (max 1500 characters).", C_RED))
            return

        uid = ctx.author.id
        expiry_note = f" It expires <t:{int(expires_at)}:R>." if expires_at else ""
        if not await confirm_purchase(
            ctx, title="🎯 Post Bounty",
            description=(
                f"Post a bounty: “{condition}”\n"
                f"The reward is held in escrow until it's claimed or you cancel.{expiry_note}"
            ),
            cost=amount, payer=ctx.author,
        ):
            return
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

        bounty = {
            "guild_id": ctx.guild.id, "channel_id": ctx.channel.id,
            "author_id": uid, "amount": amount, "condition": condition,
            "status": "open", "expires_at": expires_at, "claim_log": [],
            "claims": [],
        }
        try:
            msg = await ctx.send(embed=render_bounty_embed({**bounty, "message_id": 0}))
            await msg.add_reaction(CLAIM_EMOJI)
        except discord.HTTPException as ex:
            if uid not in state.godmode_users:
                await add_balance(uid, amount)   # full refund — nothing happened
            logging.warning("[bounty] post failed, escrow refunded: %s", ex)
            await ctx.send(embed=emb("🎯 Bounty", "Couldn't post the bounty — your coins were refunded.", C_RED))
            return

        bounty["message_id"] = msg.id
        bounty["id"] = await insert_bounty(
            guild_id=ctx.guild.id, channel_id=ctx.channel.id, message_id=msg.id,
            author_id=uid, amount=amount, condition=condition, expires_at=expires_at,
        )
        state.active_bounties[msg.id] = bounty

    # ── Reaction dispatch ─────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        # Mirror the on_message blocklist silence. Without this a banned user
        # still files claims and collects the poll-voter reward — reactions
        # never pass through on_message.
        if is_silenced(payload.user_id, payload.guild_id):
            return
        emoji = str(payload.emoji)

        # 1) 🙋 on the bounty embed in a guild channel.
        if payload.guild_id is not None and emoji == CLAIM_EMOJI:
            bounty = self._cached_bounty(payload.message_id)
            if bounty is None:
                bounty = await get_bounty_by_message(payload.message_id)
                if bounty is None or bounty["status"] != "open":
                    return
                state.active_bounties[bounty["message_id"]] = bounty
            if bounty["status"] == "open":
                await self._handle_claim_reaction(bounty, payload.user_id)
            return

        # 2) ✅/❌ on a live contest poll in a guild channel — record the vote.
        if payload.guild_id is not None and emoji in (ACCEPT_EMOJI, REJECT_EMOJI):
            member = getattr(payload, "member", None)
            if member is not None and member.bot:
                return
            await self._handle_poll_vote(payload.message_id, payload.user_id, emoji, added=True)
            return

        # 3) ✅/❌ in a DM (no guild_id). Resolve to a claim by its DM id, then
        #    by its contest-offer id.
        if payload.guild_id is None and emoji in (ACCEPT_EMOJI, REJECT_EMOJI):
            found = await get_claim_by_dm(payload.message_id)
            if found is not None:
                bounty, claim = found
                if claim["status"] == "pending" and payload.user_id == bounty["author_id"]:
                    await self._resolve_claim(bounty, claim, accepted=(emoji == ACCEPT_EMOJI))
                return
            found = await get_claim_by_contest(payload.message_id)
            if found is not None:
                bounty, claim = found
                if claim["status"] == "contesting" and payload.user_id == claim["claimant_id"]:
                    await self._resolve_contest(bounty, claim, contested=(emoji == ACCEPT_EMOJI))
                return

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Un-reacting on a live contest poll retracts the tracked vote."""
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        emoji = str(payload.emoji)
        if payload.guild_id is not None and emoji in (ACCEPT_EMOJI, REJECT_EMOJI):
            await self._handle_poll_vote(payload.message_id, payload.user_id, emoji, added=False)

    def _find_polling_claim(self, message_id: int) -> "tuple[dict, dict] | None":
        """(bounty, claim) whose live poll message is `message_id`, or None.

        Memory-only on purpose: every open bounty (and its polling claims) is
        rehydrated into state.active_bounties at boot, and most guild ✅/❌
        reactions have nothing to do with polls — a DB fallback here would
        cost a query per stray checkmark anywhere in the server.
        """
        for bounty in state.active_bounties.values():
            for claim in bounty.get("claims", []):
                if claim.get("poll_message_id") == message_id and claim["status"] == "polling":
                    return bounty, claim
        return None

    async def _handle_poll_vote(self, message_id: int, user_id: int, emoji: str, added: bool):
        """Track a ✅/❌ vote on a live poll and refresh the embed's tally.
        The tracked set — not the message's reactions — is what the close-time
        tally counts, so a vote can't be erased later by clearing reactions or
        deleting the message. Author/claimant votes are ignored, matching the
        tally's eligibility rules."""
        found = self._find_polling_claim(message_id)
        if found is None:
            return
        bounty, claim = found
        if user_id in (bounty["author_id"], claim["claimant_id"]):
            return
        votes = claim.get("poll_votes") or {"yes": [], "no": []}
        side = "yes" if emoji == ACCEPT_EMOJI else "no"
        ids = set(votes.get(side, []))
        if added == (user_id in ids):
            return          # duplicate add or removal of an untracked vote
        (ids.add if added else ids.discard)(user_id)
        votes[side] = sorted(ids)
        await self._persist_claim(claim, poll_votes=votes)
        await self._refresh_poll_embed(bounty, claim)

    async def _refresh_poll_embed(self, bounty: dict, claim: dict):
        """Re-render a live poll's embed with the current tracked tally."""
        yes, no = _poll_vote_counts(claim.get("poll_votes"))
        try:
            channel = self.bot.get_channel(claim["poll_channel_id"]) or await self.bot.fetch_channel(claim["poll_channel_id"])
            poll_msg = await channel.fetch_message(claim["poll_message_id"])
            await poll_msg.edit(embed=render_poll_embed(
                bounty, claim["claimant_id"], claim["poll_expires_at"], yes, no))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] failed to refresh poll embed %s: %s",
                            claim.get("poll_message_id"), ex)

    def _sync_bounty_into_cache(self, bounty: dict) -> dict:
        """Return the cached bounty for this message id, preferring the live
        cached copy (authoritative for siblings). Re-seeds the cache from a
        DB-loaded bounty when the cache missed (post-reboot first touch)."""
        cached = state.active_bounties.get(bounty["message_id"])
        if cached is not None:
            return cached
        state.active_bounties[bounty["message_id"]] = bounty
        return bounty

    def _merge_claim(self, bounty: dict, claim: dict) -> dict:
        """Ensure `claim` is present in bounty['claims'] and return the stored
        instance (so callers mutate the one the cache holds)."""
        existing = self._find_claim(bounty, claim["id"])
        if existing is not None:
            return existing
        bounty.setdefault("claims", []).append(claim)
        return claim

    async def _handle_claim_reaction(self, bounty: dict, user_id: int):
        """🙋 on an open bounty. Author → cancel (no-deadline only). Other user
        with no prior claim → new claim."""
        live = self._sync_bounty_into_cache(bounty)
        if live["status"] != "open":
            return

        if user_id == live["author_id"]:
            # Author self-cancel — only when there's no deadline. With a
            # deadline the author is committed until it lapses or a claim wins.
            if live.get("expires_at"):
                return
            log = list(live.get("claim_log") or [])
            live["status"] = "cancelled"  # claim sync, before await
            refund = await self._refund_author(live, BOUNTY_AUTHOR_REFUND_FRACTION)
            log.append(f"🚫 Author cancelled — refunded {refund:,} 🪙 (90%)")
            await self._persist_bounty(live, status="cancelled", claim_log=log)
            await self._refresh_embed(live)
            await self._clear_claim_reaction(live)
            return

        # A non-author claim. One claim per user per bounty, ever — a prior
        # claim (even rejected/voided) blocks a new one. The cache only holds
        # active claims, so also guard against the UNIQUE constraint by catching
        # insert failure below for the post-reboot / terminal-claim case.
        if self._claim_for_user(live, user_id) is not None:
            return
        try:
            claim_id = await insert_claim(bounty_id=live["id"], claimant_id=user_id)
        except Exception:
            # UNIQUE(bounty_id, claimant_id) violated → user already claimed once.
            return

        claim_exp = time.time() + BOUNTY_CLAIM_DURATION_SECS
        claim = {
            "id": claim_id, "bounty_id": live["id"], "claimant_id": user_id,
            "status": "pending", "dm_message_id": None, "contest_message_id": None,
            "poll_message_id": None, "poll_channel_id": None,
            "claim_expires_at": claim_exp, "contest_expires_at": None,
            "poll_expires_at": None,
        }
        self._merge_claim(live, claim)

        log = list(live.get("claim_log") or [])
        log.append(f"🙋 <@{user_id}> submitted a claim <t:{int(time.time())}:R>")

        dm = await self._dm(
            live["author_id"],
            emb("🎯 Bounty Claim",
                f"<@{user_id}> claims they completed your **{live['amount']:,} 🪙** bounty:\n\n"
                f"**{live['condition']}**\n\n"
                f"React {ACCEPT_EMOJI} to **accept** (pays them) or {REJECT_EMOJI} to **reject**.\n"
                f"This claim expires <t:{int(claim_exp)}:R>.",
                C_GOLD),
            [ACCEPT_EMOJI, REJECT_EMOJI],
        )
        await self._persist_claim(claim, dm_message_id=(dm.id if dm else None), claim_expires_at=claim_exp)
        await self._persist_bounty(live, claim_log=log)
        await self._refresh_embed(live)

        if dm is None:
            try:
                channel = self.bot.get_channel(live["channel_id"])
                if channel:
                    await channel.send(embed=emb(
                        "🎯 Bounty Claim",
                        f"<@{live['author_id']}> — <@{user_id}> claimed your bounty, but I "
                        f"couldn't DM you. Open your DMs to review it before it expires "
                        f"<t:{int(claim_exp)}:R>.",
                        C_GOLD,
                    ))
            except discord.HTTPException:
                pass

    # ── Claim resolution ──────────────────────────────────────────────────────
    async def _resolve_claim(self, bounty: dict, claim: dict, accepted: bool):
        """Author accepted or rejected a pending claim."""
        live = self._sync_bounty_into_cache(bounty)
        claim = self._merge_claim(live, claim)
        if claim["status"] != "pending" or live["status"] != "open":
            return
        claimant_id = claim["claimant_id"]

        if accepted:
            await self._award_bounty(live, claim, payout_frac=1.0, note=None)
            return

        # Reject → offer the claimant a contest. The bounty stays open.
        contest_exp = time.time() + BOUNTY_CONTEST_DURATION_SECS
        log = list(live.get("claim_log") or [])
        log.append(f"❌ Author rejected <@{claimant_id}>'s claim")
        dm = await self._dm(
            claimant_id,
            emb("🎯 Claim Rejected",
                f"The author rejected your claim on their **{live['amount']:,} 🪙** bounty:\n\n"
                f"**{live['condition']}**\n\n"
                f"React {ACCEPT_EMOJI} to **contest** (starts a community vote) or {REJECT_EMOJI} to **drop it**.\n"
                f"This offer expires <t:{int(contest_exp)}:R>.",
                C_GOLD),
            [ACCEPT_EMOJI, REJECT_EMOJI],
        )
        await self._persist_claim(
            claim, status="contesting",
            contest_message_id=(dm.id if dm else None), contest_expires_at=contest_exp,
        )
        await self._persist_bounty(live, claim_log=log)
        await self._refresh_embed(live)

    async def _resolve_contest(self, bounty: dict, claim: dict, contested: bool):
        """Rejected claimant chose to contest (→ poll) or drop it (→ claim dies,
        bounty stays open)."""
        live = self._sync_bounty_into_cache(bounty)
        claim = self._merge_claim(live, claim)
        if claim["status"] != "contesting" or live["status"] != "open":
            return
        if not contested:
            await self._reject_claim(live, claim, note=f"<@{claim['claimant_id']}> dropped their contest")
            return
        await self._start_poll(live, claim)

    async def _start_poll(self, bounty: dict, claim: dict):
        """Post a community poll for this claim and move it to `polling`."""
        claimant_id = claim["claimant_id"]
        poll_exp = time.time() + BOUNTY_POLL_DURATION_SECS
        try:
            channel = self.bot.get_channel(bounty["channel_id"]) or await self.bot.fetch_channel(bounty["channel_id"])
            poll_msg = await channel.send(
                embed=render_poll_embed(bounty, claimant_id, poll_exp, yes=0, no=0),
            )
            await poll_msg.add_reaction(ACCEPT_EMOJI)
            await poll_msg.add_reaction(REJECT_EMOJI)
        except discord.HTTPException as ex:
            logging.warning("[bounty] failed to start poll for claim %s: %s", claim["id"], ex)
            # Couldn't poll — the claim simply dies; bounty stays open.
            await self._reject_claim(bounty, claim, note="Could not start contest poll")
            return

        log = list(bounty.get("claim_log") or [])
        log.append(f"🗳️ <@{claimant_id}> contested — community vote started")
        await self._persist_claim(
            claim, status="polling",
            poll_message_id=poll_msg.id, poll_channel_id=channel.id, poll_expires_at=poll_exp,
            poll_votes={"yes": [], "no": []},
        )
        await self._persist_bounty(bounty, claim_log=log)
        await self._refresh_embed(bounty)

    async def _tally_poll(self, bounty: dict, claim: dict):
        """Settle a closed poll from the tracked votes (recorded reaction by
        reaction while the poll ran), reward each eligible voter, and pay out.

        The tracked set is authoritative — the message's reactions aren't
        trusted to still be accurate at close (a moderator can clear them, the
        message can be deleted). A best-effort reaction scan is unioned in only
        to catch votes cast while the bot was offline; a vote retracted while
        the bot was online was already removed from the tracked set."""
        author_id, claimant_id = bounty["author_id"], claim["claimant_id"]
        votes = claim.get("poll_votes") or {}
        yes_voters: set[int] = set(votes.get("yes", []))
        no_voters: set[int] = set(votes.get("no", []))
        try:
            channel = self.bot.get_channel(claim["poll_channel_id"]) or await self.bot.fetch_channel(claim["poll_channel_id"])
            poll_msg = await channel.fetch_message(claim["poll_message_id"])
            for reaction in poll_msg.reactions:
                if str(reaction.emoji) not in (ACCEPT_EMOJI, REJECT_EMOJI):
                    continue
                async for voter in reaction.users():
                    if voter.bot or voter.id in (author_id, claimant_id):
                        continue
                    if str(reaction.emoji) == ACCEPT_EMOJI:
                        yes_voters.add(voter.id)
                    else:
                        no_voters.add(voter.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] poll tally fetch failed for claim %s: %s", claim["id"], ex)

        voters = yes_voters | no_voters
        # A both-ways reactor previously added 1 to each side, dragging the
        # ratio toward 50% — which is a payout threshold. Their votes cancel:
        # count them for the reward but for neither side of the tally.
        both_ways = yes_voters & no_voters
        yes = len(yes_voters - both_ways)
        no = len(no_voters - both_ways)

        # Reward each unique eligible voter once, regardless of how they voted or
        # the poll's outcome. Freshly minted, capped at one payout per voter even
        # if they reacted with both emojis.
        if voters and BOUNTY_POLL_VOTER_REWARD > 0:
            for voter_id in voters:
                await add_balance(voter_id, BOUNTY_POLL_VOTER_REWARD)
            log = list(bounty.get("claim_log") or [])
            log.append(f"🪙 Paid {BOUNTY_POLL_VOTER_REWARD:,} 🪙 to {len(voters)} voter(s)")
            await self._persist_bounty(bounty, claim_log=log)

        total = yes + no
        ratio = (yes / total) if total else 0.0
        payout_frac = _poll_payout_fraction(ratio)
        if payout_frac <= 0:
            await self._reject_claim(bounty, claim, note=f"🗳️ Vote failed ({yes}✅/{no}❌) for <@{claimant_id}>")
            await self._dm(claimant_id, emb(
                "🎯 Bounty Vote", f"The community vote failed ({yes}✅/{no}❌). No payout.", C_RED), [])
            return
        await self._award_bounty(bounty, claim, payout_frac=payout_frac, note=f"🗳️ Vote passed ({yes}✅/{no}❌)")

    # ── Terminal transitions ──────────────────────────────────────────────────
    async def _award_bounty(self, bounty: dict, claim: dict, payout_frac: float, note: "str | None"):
        """Pay `claim`'s claimant `payout_frac` of the escrow, mark the bounty
        accepted, and void every sibling claim (cancelling any live poll). The
        unpaid remainder of a partial payout is a house cut — not refunded."""
        claimant_id = claim["claimant_id"]
        amount = bounty["amount"]
        payout = int(amount * payout_frac)
        log = list(bounty.get("claim_log") or [])

        # Claim terminal status synchronously BEFORE the persist/payout awaits:
        # an author accepting two pending claims in quick succession (or a poll
        # tally racing an author-accept) must not both pass the open-status
        # gate and pay the escrow twice.
        bounty["status"] = "accepted"

        await self._persist_claim(claim, status="accepted")
        if payout > 0:
            await add_balance(claimant_id, payout, guild_id=bounty["guild_id"])
        pct = int(payout_frac * 100)
        prefix = f"{note} — " if note else ""
        log.append(f"✅ {prefix}paid {payout:,} 🪙 ({pct}%) to <@{claimant_id}>")

        await self._void_siblings(bounty, winner_claim_id=claim["id"], log=log)
        await self._persist_bounty(bounty, status="accepted", claim_log=log)
        await self._refresh_embed(bounty)
        await self._clear_claim_reaction(bounty)
        await self._dm(claimant_id, emb(
            "🎯 Bounty Accepted",
            f"Your claim was accepted! **{payout:,} 🪙**"
            + (f" ({pct}% of the bounty)" if payout_frac < 1.0 else "")
            + " has been paid to you.",
            C_GREEN), [])

    async def _void_siblings(self, bounty: dict, winner_claim_id: int, log: list):
        """Void every non-terminal claim other than the winner. A sibling with a
        live poll has it edited to a 'cancelled' embed and reactions cleared."""
        for sib in bounty.get("claims", []):
            if sib["id"] == winner_claim_id or sib["status"] not in _CLAIM_ACTIVE:
                continue
            if sib["status"] == "polling" and sib.get("poll_message_id"):
                await self._void_poll_message(sib)
            await self._persist_claim(sib, status="voided")
            log.append(f"➖ <@{sib['claimant_id']}>'s claim voided (bounty already paid out)")

    async def _void_poll_message(self, claim: dict):
        """Edit a live contest poll to show it was superseded, and clear its
        reactions so no further votes register. Tally skips voided claims."""
        try:
            channel = self.bot.get_channel(claim["poll_channel_id"]) or await self.bot.fetch_channel(claim["poll_channel_id"])
            poll_msg = await channel.fetch_message(claim["poll_message_id"])
            await poll_msg.edit(embed=emb(
                "🎯 Bounty Dispute — Vote Cancelled",
                "This bounty was already paid out to another claimant. This vote is void.",
                C_GREY,
            ))
            try:
                await poll_msg.clear_reactions()
            except (discord.Forbidden, discord.HTTPException):
                pass
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as ex:
            logging.warning("[bounty] failed to void poll %s: %s", claim.get("poll_message_id"), ex)

    async def _reject_claim(self, bounty: dict, claim: dict, note: str):
        """Mark a single claim rejected. The BOUNTY STAYS OPEN for others — no
        author refund (the escrow is still held for a future winner)."""
        log = list(bounty.get("claim_log") or [])
        log.append(f"❌ {note}")
        await self._persist_claim(claim, status="rejected")
        await self._persist_bounty(bounty, claim_log=log)
        await self._refresh_embed(bounty)

    async def _expire_bounty(self, bounty: dict):
        """An open bounty hit its deadline: refund the author 90%, void all
        in-flight claims (cancelling polls), mark expired."""
        log = list(bounty.get("claim_log") or [])
        bounty["status"] = "expired"
        refund = await self._refund_author(bounty, BOUNTY_AUTHOR_REFUND_FRACTION)
        log.append(f"⌛ Bounty expired unclaimed — refunded {refund:,} 🪙 (90%) to the author")
        # Void any in-flight claims (and their polls). winner_claim_id=-1 → none.
        await self._void_siblings(bounty, winner_claim_id=-1, log=log)
        await self._persist_bounty(bounty, status="expired", claim_log=log)
        await self._refresh_embed(bounty)
        await self._clear_claim_reaction(bounty)

    # ── Expiry loop ───────────────────────────────────────────────────────────
    @tasks.loop(minutes=2)
    async def _expiry_loop(self):
        """Settle timed-out bounties, claims, contests, and polls. Cheap: walks
        the in-memory open set, acting only on rows past a deadline."""
        now = time.time()
        for bounty in list(state.active_bounties.values()):
            try:
                # Bounty-level deadline first — expiring it voids the claims.
                exp = bounty.get("expires_at")
                if bounty["status"] == "open" and exp and now >= exp:
                    await self._expire_bounty(bounty)
                    continue
                # Per-claim deadlines. Copy: settlement mutates the claim list.
                for claim in list(bounty.get("claims", [])):
                    if claim["status"] == "pending":
                        c = claim.get("claim_expires_at")
                        if c and now >= c:
                            await self._auto_reject_unanswered(bounty, claim)
                    elif claim["status"] == "contesting":
                        c = claim.get("contest_expires_at")
                        if c and now >= c:
                            await self._reject_claim(bounty, claim, note=f"<@{claim['claimant_id']}>'s contest offer expired")
                    elif claim["status"] == "polling":
                        c = claim.get("poll_expires_at")
                        if c and now >= c:
                            await self._tally_poll(bounty, claim)
            except Exception:
                logging.exception("[bounty] expiry settlement failed for %s", bounty.get("message_id"))

    async def _auto_reject_unanswered(self, bounty: dict, claim: dict):
        """A pending claim the author never answered. Offer the claimant the
        same contest path as an explicit reject."""
        if claim["status"] != "pending":
            return
        contest_exp = time.time() + BOUNTY_CONTEST_DURATION_SECS
        log = list(bounty.get("claim_log") or [])
        log.append(f"⌛ Author didn't respond to <@{claim['claimant_id']}>'s claim in time")
        dm = await self._dm(
            claim["claimant_id"],
            emb("🎯 Claim Expired",
                f"The author didn't respond to your claim on their **{bounty['amount']:,} 🪙** bounty in time.\n\n"
                f"React {ACCEPT_EMOJI} to **contest** (community vote) or {REJECT_EMOJI} to **drop it**.\n"
                f"This offer expires <t:{int(contest_exp)}:R>.",
                C_GOLD),
            [ACCEPT_EMOJI, REJECT_EMOJI],
        )
        await self._persist_claim(
            claim, status="contesting",
            contest_message_id=(dm.id if dm else None), contest_expires_at=contest_exp,
        )
        await self._persist_bounty(bounty, claim_log=log)
        await self._refresh_embed(bounty)

    @_expiry_loop.before_loop
    async def _before_expiry(self):
        await self.bot.wait_until_ready()
        import src.persistence as _pkg
        await _pkg.init_done.wait()

    # ── Command surface ───────────────────────────────────────────────────────
    @commands.command(name="bounty")
    async def cmd_bounty(self, ctx: commands.Context, *args):
        await self.create_bounty(ctx, args)

    @commands.command(name="bounties", aliases=["allbounties", "bountylist", "quests"])
    async def cmd_bounties(self, ctx: commands.Context):
        """List this server's open bounties and their rewards."""
        if ctx.guild is None:
            await ctx.send(embed=emb("🎯 Bounties", "Bounties only work in servers.", C_RED))
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

        # Open bounties live in memory, keyed by message id. Newest first.
        bounties = sorted(
            (b for b in state.active_bounties.values()
             if b.get("guild_id") == ctx.guild.id and b.get("status") == "open"),
            key=lambda b: b.get("created_at") or 0,
            reverse=True,
        )
        if not bounties:
            await ctx.send(embed=emb(
                "🎯 Open Bounties",
                f"No open bounties right now. Post one with `!bounty <coins> [duration] <condition>` "
                f"in <#{bounty_channel_id}>.",
                C_GOLD,
            ))
            return

        lines = []
        for b in bounties[:25]:
            cond = b["condition"].replace("\n", " ")
            if len(cond) > 100:
                cond = cond[:99] + "…"
            link = (
                f"https://discord.com/channels/{b['guild_id']}/{b['channel_id']}/{b['message_id']}"
            )
            parts = [f"**{b['amount']:,} 🪙** — {cond} · [jump]({link})"]
            claims = [c for c in b.get("claims", []) if c["status"] in _CLAIM_ACTIVE]
            if claims:
                parts.append(f"🙋 {len(claims)} active claim{'s' if len(claims) != 1 else ''}")
            if b.get("expires_at"):
                parts.append(f"⌛ expires <t:{int(b['expires_at'])}:R>")
            lines.append(" · ".join(parts))

        desc = "\n".join(lines)
        extra = f"\n\n…and {len(bounties) - 25} more." if len(bounties) > 25 else ""
        desc += f"{extra}\n\n📍 Bounty channel: <#{bounty_channel_id}>"
        await ctx.send(embed=emb(
            f"🎯 Open Bounties ({len(bounties)})",
            desc,
            C_GOLD,
        ))


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
