"""Bounty feature: escrow, optional expiry, concurrent per-claim lifecycle,
90% author refund, poll payout math, and persistence.

Drives BountyCog's internal handlers directly (not through the gateway) with a
small fake bot that records embed edits, DMs, and reactions. Uses the real `db`
fixture so the bounties + bounty_claims tables round-trip through the SQLite
translator exactly like production MariaDB.
"""
import time
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.bounty_cog import BountyCog, _poll_payout_fraction, CLAIM_EMOJI
from src.economy import add_balance, get_balance
from src.guild_config import get_guild_cfg

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeTextChannel


pytestmark = pytest.mark.asyncio

GUILD_ID = 777
BOUNTY_CHANNEL_ID = 888


class _FakeResp:
    """Minimal stand-in for the aiohttp response discord.HTTPException needs."""

    def __init__(self, status: int):
        self.status = status
        self.reason = "test"


class _FakeReaction:
    def __init__(self, emoji, voters):
        self.emoji = emoji
        self._voters = voters

    def users(self):
        async def _gen():
            for v in self._voters:
                yield v
        return _gen()


class _FakeMsg:
    def __init__(self, msg_id):
        self.id = msg_id
        self.embeds = []
        self.edit = AsyncMock(side_effect=self._edit)
        self.add_reaction = AsyncMock()
        self.clear_reaction = AsyncMock()
        self.clear_reactions = AsyncMock()
        self.remove_reaction = AsyncMock()
        self.reactions = []

    async def _edit(self, *, embed=None, **kw):
        if embed is not None:
            self.embeds = [embed]


class _FakeDMUser:
    def __init__(self, uid):
        self.id = uid
        self.bot = False
        self.sent = []

    async def send(self, *, embed=None, **kw):
        msg = _FakeMsg(900_000 + len(self.sent))
        msg.embeds = [embed] if embed else []
        self.sent.append(msg)
        return msg


class _FakeChannel(FakeTextChannel):
    def __init__(self, ch_id):
        super().__init__(ch_id=ch_id, name="bounty")
        self._messages = {}
        self._next = 1000
        self.send = AsyncMock(side_effect=self._send)

    async def _send(self, content=None, *, embed=None, **kw):
        self._next += 1
        msg = _FakeMsg(self._next)
        msg.embeds = [embed] if embed else []
        self._messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        return self._messages[mid]


class _FakeBot:
    def __init__(self, channel):
        self.user = FakeMember(uid=999999)
        self.user.bot = True
        self._channel = channel
        self._users = {}

    def get_channel(self, cid):
        return self._channel if cid == self._channel.id else None

    async def fetch_channel(self, cid):
        return self._channel

    def get_user(self, uid):
        return self._users.setdefault(uid, _FakeDMUser(uid))

    async def fetch_user(self, uid):
        return self.get_user(uid)


def _make_cog():
    channel = _FakeChannel(BOUNTY_CHANNEL_ID)
    bot = _FakeBot(channel)
    cog = BountyCog(bot=None)        # bot=None skips starting the expiry loop
    cog.bot = bot
    return cog, bot, channel


def _ctx(author_id, channel):
    guild = FakeGuild(gid=GUILD_ID)
    ctx = FakeCtx(author=FakeMember(uid=author_id), guild=guild, channel=channel)
    get_guild_cfg(GUILD_ID)["bounty_channel"] = BOUNTY_CHANNEL_ID

    async def _send(content=None, *, embed=None, view=None, **kw):
        if embed is not None:
            ctx.sent_embeds.append(embed)
        if content is not None:
            ctx.sent_messages.append(content)
        return await channel._send(content, embed=embed, **kw)

    ctx.send = _send
    ctx.message.reply = AsyncMock(return_value=_FakeMsg(1))
    return ctx


def _bounty(mid):
    return _state.active_bounties[mid]


def _only_claim(mid):
    return _bounty(mid)["claims"][0]


# ── creation / escrow / duration parse ────────────────────────────────────────
async def test_create_escrows_and_persists(db):
    cog, bot, channel = _make_cog()
    author = 11
    await add_balance(author, 50_000)
    ctx = _ctx(author, channel)

    await cog.create_bounty(ctx, ("10k", "wash", "my", "car"))

    assert await get_balance(author) == 40_000
    assert len(_state.active_bounties) == 1
    mid = next(iter(_state.active_bounties))
    row = await _persistence.get_bounty_by_message(mid)
    assert row["status"] == "open"
    assert row["amount"] == 10_000
    assert row["condition"] == "wash my car"
    assert row["author_id"] == author
    assert row["expires_at"] is None


async def test_create_with_duration_sets_expiry(db):
    cog, bot, channel = _make_cog()
    author = 16
    await add_balance(author, 50_000)
    ctx = _ctx(author, channel)

    await cog.create_bounty(ctx, ("5k", "7d", "watch", "this", "video"))
    mid = next(iter(_state.active_bounties))
    row = await _persistence.get_bounty_by_message(mid)
    assert row["condition"] == "watch this video"     # duration token consumed
    assert row["expires_at"] is not None
    # ~7 days out.
    assert 6.9 * 86_400 < row["expires_at"] - time.time() < 7.1 * 86_400


async def test_duration_token_not_consumed_without_condition(db):
    """`!bounty 5k 7d` with no trailing condition keeps 7d as the condition and
    errors (condition required)."""
    cog, bot, channel = _make_cog()
    author = 17
    await add_balance(author, 50_000)
    ctx = _ctx(author, channel)
    # Only amount + one token: too few args (need >=2). Add a real condition
    # that is itself a duration-looking word to prove it's NOT eaten.
    await cog.create_bounty(ctx, ("5k", "2w"))   # len(args) == 2, "2w" is condition
    mid = next(iter(_state.active_bounties))
    row = await _persistence.get_bounty_by_message(mid)
    assert row["condition"] == "2w"
    assert row["expires_at"] is None


async def test_create_rejects_below_minimum(db):
    cog, bot, channel = _make_cog()
    author = 12
    await add_balance(author, 50_000)
    ctx = _ctx(author, channel)
    await cog.create_bounty(ctx, ("500", "too", "cheap"))
    assert await get_balance(author) == 50_000
    assert not _state.active_bounties


async def test_create_insufficient_funds(db):
    cog, bot, channel = _make_cog()
    author = 13
    await add_balance(author, 1_000)
    ctx = _ctx(author, channel)
    await cog.create_bounty(ctx, ("5k", "do", "thing"))
    assert await get_balance(author) == 1_000
    assert not _state.active_bounties
    assert any("Insufficient" in e.title for e in ctx.sent_embeds)


async def test_create_wrong_channel_blocked(db):
    cog, bot, channel = _make_cog()
    author = 14
    await add_balance(author, 50_000)
    ctx = _ctx(author, channel)
    ctx.channel = FakeTextChannel(ch_id=123, name="random")
    ctx.reply = AsyncMock(return_value=_FakeMsg(1))
    await cog.create_bounty(ctx, ("5k", "do", "thing"))
    assert await get_balance(author) == 50_000
    assert not _state.active_bounties


async def test_create_disabled_when_no_channel(db):
    cog, bot, channel = _make_cog()
    author = 15
    await add_balance(author, 50_000)
    ctx = _ctx(author, channel)
    get_guild_cfg(GUILD_ID)["bounty_channel"] = None
    await cog.create_bounty(ctx, ("5k", "do", "thing"))
    assert await get_balance(author) == 50_000
    assert any("Not Enabled" in e.title for e in ctx.sent_embeds)


# ── claim helpers ─────────────────────────────────────────────────────────────
async def _open_bounty(cog, channel, author=20, amount=10_000, args=None):
    await add_balance(author, amount)
    ctx = _ctx(author, channel)
    await cog.create_bounty(ctx, args or (f"{amount}", "do", "the", "task"))
    mid = next(iter(_state.active_bounties))
    return mid, author


# ── author self-cancel (90% refund, no-deadline only) ─────────────────────────
async def test_author_self_cancel_refunds_90pct(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    assert await get_balance(author) == 0

    await cog._handle_claim_reaction(_bounty(mid), author)   # author reacts 🙋

    assert await get_balance(author) == 9_000          # 90% of 10k
    assert mid not in _state.active_bounties            # terminal, dropped
    row = await _persistence.get_bounty_by_message(mid)
    assert row["status"] == "cancelled"
    channel._messages[mid].clear_reaction.assert_awaited_with(CLAIM_EMOJI)


async def test_author_cannot_cancel_when_deadline_set(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel, args=("10000", "7d", "do", "it"))
    await cog._handle_claim_reaction(_bounty(mid), author)   # author reacts 🙋
    # Still open, no refund — a deadline bounty can't be self-cancelled.
    assert _bounty(mid)["status"] == "open"
    assert await get_balance(author) == 0


# ── claim → accept ────────────────────────────────────────────────────────────
async def test_claim_then_accept_pays_claimant(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 30

    await cog._handle_claim_reaction(_bounty(mid), claimant)
    claim = _only_claim(mid)
    assert claim["status"] == "pending"
    assert claim["claimant_id"] == claimant
    assert bot.get_user(author).sent           # author DM'd accept/reject
    assert _bounty(mid)["status"] == "open"    # bounty stays open during review

    await cog._resolve_claim(_bounty(mid), claim, accepted=True)
    assert await get_balance(claimant) == 10_000
    assert mid not in _state.active_bounties
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "accepted"


async def test_one_claim_per_user_ever(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 33
    await cog._handle_claim_reaction(_bounty(mid), claimant)
    # Reject the claim, then the same user tries again — blocked.
    claim = _only_claim(mid)
    await cog._resolve_claim(_bounty(mid), claim, accepted=False)
    await cog._resolve_contest(_bounty(mid), claim, contested=False)  # drop
    # Bounty stays open; the user's claim row exists (rejected).
    assert _bounty(mid)["status"] == "open"
    await cog._handle_claim_reaction(_bounty(mid), claimant)   # re-claim attempt
    # No new pending claim was created (insert blocked by UNIQUE).
    active = [c for c in _bounty(mid)["claims"] if c["status"] == "pending"]
    assert active == []


async def test_concurrent_claims_both_tracked(db):
    """Two different users claim near-simultaneously — both get a claim; the
    bounty stays open."""
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)

    import asyncio
    await asyncio.gather(
        cog._handle_claim_reaction(_bounty(mid), 31),
        cog._handle_claim_reaction(_bounty(mid), 32),
    )
    claimants = {c["claimant_id"] for c in _bounty(mid)["claims"]}
    assert claimants == {31, 32}
    assert _bounty(mid)["status"] == "open"


async def test_accept_voids_sibling_claims(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    await cog._handle_claim_reaction(_bounty(mid), 34)
    await cog._handle_claim_reaction(_bounty(mid), 35)
    bounty = _bounty(mid)
    winner = next(c for c in bounty["claims"] if c["claimant_id"] == 34)

    await cog._resolve_claim(bounty, winner, accepted=True)
    assert await get_balance(34) == 10_000
    assert mid not in _state.active_bounties
    # The sibling claim was voided in the DB.
    found = await _persistence.get_claim_by_dm(
        next(c for c in bounty["claims"] if c["claimant_id"] == 35)["dm_message_id"]
    )
    assert found is not None
    _b, sib = found
    assert sib["status"] == "voided"


async def test_accept_voids_sibling_live_poll(db):
    """A sibling claim already in an @everyone poll has its poll cancelled (embed
    edited, reactions cleared) and the claim voided when another claim is
    accepted."""
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    # Claim A goes all the way to a live poll.
    await cog._handle_claim_reaction(_bounty(mid), 36)
    claim_a = _only_claim(mid)
    await cog._resolve_claim(_bounty(mid), claim_a, accepted=False)
    await cog._resolve_contest(_bounty(mid), claim_a, contested=True)
    assert claim_a["status"] == "polling"
    poll_msg = channel._messages[claim_a["poll_message_id"]]

    # Claim B is submitted and accepted.
    await cog._handle_claim_reaction(_bounty(mid), 37)
    claim_b = next(c for c in _bounty(mid)["claims"] if c["claimant_id"] == 37)
    await cog._resolve_claim(_bounty(mid), claim_b, accepted=True)

    assert await get_balance(37) == 10_000
    # Claim A's poll was edited to a void embed and its reactions cleared.
    poll_msg.clear_reactions.assert_awaited()
    assert "void" in poll_msg.embeds[0].title.lower() or "cancel" in poll_msg.embeds[0].title.lower()
    found = await _persistence.get_claim_by_dm(claim_a["dm_message_id"])
    assert found is not None and found[1]["status"] == "voided"


# ── reject → contest → leaves bounty open ─────────────────────────────────────
async def test_reject_then_drop_keeps_bounty_open_no_refund(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 40
    await cog._handle_claim_reaction(_bounty(mid), claimant)
    claim = _only_claim(mid)
    await cog._resolve_claim(_bounty(mid), claim, accepted=False)
    assert claim["status"] == "contesting"

    await cog._resolve_contest(_bounty(mid), claim, contested=False)   # drop
    # Bounty stays open; author NOT refunded (escrow held for future claimers).
    assert _bounty(mid)["status"] == "open"
    assert await get_balance(author) == 0
    assert claim["status"] == "rejected"


async def test_contest_starts_poll(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 41
    await cog._handle_claim_reaction(_bounty(mid), claimant)
    claim = _only_claim(mid)
    await cog._resolve_claim(_bounty(mid), claim, accepted=False)
    await cog._resolve_contest(_bounty(mid), claim, contested=True)

    assert claim["status"] == "polling"
    assert claim["poll_message_id"] is not None
    assert _bounty(mid)["status"] == "open"


# ── poll tally ────────────────────────────────────────────────────────────────
async def _setup_polling_claim(cog, channel, author, claimant, amount=10_000):
    mid, _ = await _open_bounty(cog, channel, author=author, amount=amount)
    await cog._handle_claim_reaction(_bounty(mid), claimant)
    claim = _only_claim(mid)
    await cog._resolve_claim(_bounty(mid), claim, accepted=False)
    await cog._resolve_contest(_bounty(mid), claim, contested=True)
    return mid, claim


def _seed_poll_votes(channel, claim, yes_ids, no_ids):
    poll_msg = channel._messages[claim["poll_message_id"]]
    poll_msg.reactions = [
        _FakeReaction("✅", [_FakeDMUser(u) for u in yes_ids]),
        _FakeReaction("❌", [_FakeDMUser(u) for u in no_ids]),
    ]


async def test_poll_full_payout(db):
    cog, bot, channel = _make_cog()
    mid, claim = await _setup_polling_claim(cog, channel, author=20, claimant=42)
    _seed_poll_votes(channel, claim, yes_ids=[1, 2, 3, 4, 5, 6, 7], no_ids=[8])
    claim["poll_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert await get_balance(42) == 10_000          # full payout
    assert await get_balance(20) == 0               # author keeps nothing back
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "accepted"


async def test_poll_partial_payout_no_author_refund(db):
    cog, bot, channel = _make_cog()
    mid, claim = await _setup_polling_claim(cog, channel, author=20, claimant=43)
    _seed_poll_votes(channel, claim, yes_ids=[1, 2, 3, 4, 5], no_ids=[6, 7, 8, 9, 10])  # 50%
    claim["poll_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert await get_balance(43) == 5_000           # 50% payout
    assert await get_balance(20) == 0               # remainder is a house cut
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "accepted"


async def test_poll_failed_keeps_bounty_open(db):
    cog, bot, channel = _make_cog()
    mid, claim = await _setup_polling_claim(cog, channel, author=20, claimant=44)
    _seed_poll_votes(channel, claim, yes_ids=[1], no_ids=[2, 3, 4, 5, 6, 7, 8, 9])
    claim["poll_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert await get_balance(44) == 0
    assert await get_balance(20) == 0               # author not refunded
    assert _bounty(mid)["status"] == "open"         # stays open for others
    assert claim["status"] == "rejected"


async def test_poll_excludes_author_and_claimant(db):
    cog, bot, channel = _make_cog()
    mid, claim = await _setup_polling_claim(cog, channel, author=20, claimant=45)
    # Author (20) and claimant (45) vote yes but must NOT count; one neutral no.
    _seed_poll_votes(channel, claim, yes_ids=[20, 45], no_ids=[7])
    claim["poll_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    # Only the one neutral 'no' counts → 0% yes → no payout, bounty stays open.
    assert await get_balance(45) == 0
    assert _bounty(mid)["status"] == "open"
    # Author and claimant get no voter reward even though they reacted.
    assert await get_balance(20) == 0
    assert await get_balance(45) == 0
    # The neutral voter (7) is rewarded.
    assert await get_balance(7) == 100


async def test_poll_voters_each_rewarded_on_pass(db):
    cog, bot, channel = _make_cog()
    mid, claim = await _setup_polling_claim(cog, channel, author=20, claimant=46)
    _seed_poll_votes(channel, claim, yes_ids=[1, 2, 3, 4, 5, 6, 7], no_ids=[8])
    claim["poll_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    # Every eligible voter (yes and no) got 100 coins, once each.
    for v in (1, 2, 3, 4, 5, 6, 7, 8):
        assert await get_balance(v) == 100
    # Claimant still got the full payout; author and claimant get no voter reward.
    assert await get_balance(46) == 10_000
    assert await get_balance(20) == 0


async def test_poll_voter_rewarded_once_when_voting_both_ways(db):
    """A user who reacts both ✅ and ❌ is paid the 100-coin reward only once."""
    cog, bot, channel = _make_cog()
    mid, claim = await _setup_polling_claim(cog, channel, author=20, claimant=47)
    # Voter 9 appears in both yes and no lists.
    _seed_poll_votes(channel, claim, yes_ids=[9], no_ids=[9, 10])
    claim["poll_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert await get_balance(9) == 100      # paid once, not 200
    assert await get_balance(10) == 100


# ── expiry loop ───────────────────────────────────────────────────────────────
async def test_open_bounty_expires_refunds_90pct(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel, args=("10000", "7d", "do", "it"))
    _bounty(mid)["expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert await get_balance(author) == 9_000       # 90% refund
    assert mid not in _state.active_bounties
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "expired"


async def test_expired_open_voids_inflight_claims(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel, args=("10000", "7d", "do", "it"))
    await cog._handle_claim_reaction(_bounty(mid), 60)
    claim = _only_claim(mid)
    _bounty(mid)["expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "expired"
    # The in-flight claim is voided.
    found = await _persistence.get_claim_by_dm(claim["dm_message_id"])
    assert found is not None and found[1]["status"] == "voided"


async def test_expired_pending_offers_contest(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 51
    await cog._handle_claim_reaction(_bounty(mid), claimant)
    _only_claim(mid)["claim_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert _only_claim(mid)["status"] == "contesting"
    assert bot.get_user(claimant).sent
    assert _bounty(mid)["status"] == "open"


async def test_expired_contest_keeps_bounty_open(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 52
    await cog._handle_claim_reaction(_bounty(mid), claimant)
    claim = _only_claim(mid)
    await cog._resolve_claim(_bounty(mid), claim, accepted=False)
    claim["contest_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert claim["status"] == "rejected"
    assert _bounty(mid)["status"] == "open"
    assert await get_balance(author) == 0


# ── reaction cleanup fallback ─────────────────────────────────────────────────
async def test_claim_reaction_cleanup_falls_back_when_no_manage_messages(db):
    import discord
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    msg = channel._messages[mid]
    msg.clear_reaction = AsyncMock(side_effect=discord.Forbidden(_FakeResp(403), "no perms"))

    # Author self-cancel clears the reaction → fallback path.
    await cog._handle_claim_reaction(_bounty(mid), author)
    msg.clear_reaction.assert_awaited_with(CLAIM_EMOJI)
    msg.remove_reaction.assert_awaited_with(CLAIM_EMOJI, bot.user)


# ── payout fraction math ──────────────────────────────────────────────────────
async def test_poll_payout_fraction_boundaries():
    assert _poll_payout_fraction(0.0) == 0.0
    assert _poll_payout_fraction(0.49) == 0.0
    assert _poll_payout_fraction(0.5) == 0.5
    assert _poll_payout_fraction(2 / 3) == 1.0
    assert _poll_payout_fraction(1.0) == 1.0
    assert 0.5 < _poll_payout_fraction(0.58) < 1.0


# ── !bounties list command ────────────────────────────────────────────────────
async def test_bounties_list_empty(db):
    cog, bot, channel = _make_cog()
    ctx = _ctx(1, channel)
    await cog.cmd_bounties.callback(cog, ctx)
    assert any("No open bounties" in e.description for e in ctx.sent_embeds)


async def test_bounties_list_not_enabled(db):
    cog, bot, channel = _make_cog()
    ctx = _ctx(1, channel)
    get_guild_cfg(GUILD_ID)["bounty_channel"] = None
    await cog.cmd_bounties.callback(cog, ctx)
    assert any("Not Enabled" in e.title for e in ctx.sent_embeds)


async def test_bounties_list_shows_open_bounties(db):
    cog, bot, channel = _make_cog()
    await _open_bounty(cog, channel, author=70, amount=5_000,
                       args=("5000", "wash", "the", "dishes"))
    await _open_bounty(cog, channel, author=71, amount=12_000,
                       args=("12000", "7d", "mow", "the", "lawn"))

    ctx = _ctx(1, channel)
    await cog.cmd_bounties.callback(cog, ctx)
    listing = next(e for e in ctx.sent_embeds if "Open Bounties" in e.title)
    assert "(2)" in listing.title
    assert "5,000 🪙" in listing.description
    assert "wash the dishes" in listing.description
    assert "12,000 🪙" in listing.description
    assert "mow the lawn" in listing.description
    assert "expires <t:" in listing.description       # the 7d bounty shows expiry
    assert f"<#{BOUNTY_CHANNEL_ID}>" in listing.description   # links the bounty channel


async def test_bounties_list_annotates_active_claims(db):
    cog, bot, channel = _make_cog()
    mid, _ = await _open_bounty(cog, channel, author=72, amount=8_000,
                                args=("8000", "do", "a", "thing"))
    await cog._handle_claim_reaction(_bounty(mid), 73)

    ctx = _ctx(1, channel)
    await cog.cmd_bounties.callback(cog, ctx)
    listing = next(e for e in ctx.sent_embeds if "Open Bounties" in e.title)
    assert "1 active claim" in listing.description


async def test_bounties_list_excludes_other_guilds(db):
    cog, bot, channel = _make_cog()
    await _open_bounty(cog, channel, author=74, amount=5_000,
                       args=("5000", "task", "here"))
    # A stray open bounty in a different guild must not appear.
    other = dict(_bounty(next(iter(_state.active_bounties))))
    other = {**other, "guild_id": 999999, "message_id": 424242}
    _state.active_bounties[other["message_id"]] = other

    ctx = _ctx(1, channel)
    await cog.cmd_bounties.callback(cog, ctx)
    listing = next(e for e in ctx.sent_embeds if "Open Bounties" in e.title)
    assert "(1)" in listing.title
