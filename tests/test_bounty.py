"""Bounty feature: escrow, lifecycle transitions, payout math, persistence.

Drives BountyCog's internal handlers directly (not through the gateway) with a
small fake bot that records embed edits, DMs, and reactions. Uses the real `db`
fixture so the bounties table round-trips through the SQLite translator exactly
like production MariaDB.
"""
import time
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.bounty_cog import BountyCog, _poll_payout_fraction
from src.economy import add_balance, get_balance
from src.guild_config import get_guild_cfg

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeTextChannel


pytestmark = pytest.mark.asyncio

GUILD_ID = 777
BOUNTY_CHANNEL_ID = 888


class _FakeMsg:
    def __init__(self, msg_id):
        self.id = msg_id
        self.embeds = []
        self.edit = AsyncMock(side_effect=self._edit)
        self.add_reaction = AsyncMock()
        self.reactions = []

    async def _edit(self, *, embed=None, **kw):
        if embed is not None:
            self.embeds = [embed]


class _FakeDMUser:
    def __init__(self, uid):
        self.id = uid
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

    # Route ctx.send through the fake bounty channel so the posted bounty embed
    # is fetchable later (the cog edits it on every transition). Records embeds
    # on ctx.sent_embeds too, like the stock FakeCtx.
    async def _send(content=None, *, embed=None, view=None, **kw):
        if embed is not None:
            ctx.sent_embeds.append(embed)
        if content is not None:
            ctx.sent_messages.append(content)
        return await channel._send(content, embed=embed, **kw)

    ctx.send = _send
    # _wrong_channel_reply uses ctx.message.reply; keep it a harmless AsyncMock.
    ctx.message.reply = AsyncMock(return_value=_FakeMsg(1))
    return ctx


# ── creation / escrow ─────────────────────────────────────────────────────────
async def test_create_escrows_and_persists(db):
    cog, bot, channel = _make_cog()
    author = 11
    await add_balance(author, 50_000)
    ctx = _ctx(author, channel)

    await cog.create_bounty(ctx, ("10k", "wash", "my", "car"))

    # Escrowed 10k.
    assert await get_balance(author) == 40_000
    # One bounty cached + persisted.
    assert len(_state.active_bounties) == 1
    mid = next(iter(_state.active_bounties))
    row = await _persistence.get_bounty_by_message(mid)
    assert row["status"] == "open"
    assert row["amount"] == 10_000
    assert row["condition"] == "wash my car"
    assert row["author_id"] == author


async def test_create_rejects_below_minimum(db):
    cog, bot, channel = _make_cog()
    author = 12
    await add_balance(author, 50_000)
    ctx = _ctx(author, channel)

    await cog.create_bounty(ctx, ("500", "too", "cheap"))
    assert await get_balance(author) == 50_000          # nothing escrowed
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
    ctx.channel = FakeTextChannel(ch_id=123, name="random")  # not the bounty channel
    # FakeCtx isn't a real commands.Context, so _wrong_channel_reply uses
    # ctx.reply directly — stub it.
    ctx.reply = AsyncMock(return_value=_FakeMsg(1))

    await cog.create_bounty(ctx, ("5k", "do", "thing"))
    assert await get_balance(author) == 50_000             # no escrow
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


# ── claim → accept (happy path) ───────────────────────────────────────────────
async def _open_bounty(cog, channel, author=20, amount=10_000):
    await add_balance(author, amount)
    ctx = _ctx(author, channel)
    await cog.create_bounty(ctx, (f"{amount}", "do", "the", "task"))
    mid = next(iter(_state.active_bounties))
    return mid, author


async def test_author_self_claim_cancels_and_refunds(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    assert await get_balance(author) == 0

    bounty = _state.active_bounties[mid]
    await cog._handle_claim_reaction(bounty, author)   # author reacts 🙋

    assert await get_balance(author) == 10_000          # refunded
    assert mid not in _state.active_bounties            # terminal, dropped
    row = await _persistence.get_bounty_by_message(mid)
    assert row["status"] == "cancelled"


async def test_claim_then_accept_pays_claimant(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 30

    bounty = _state.active_bounties[mid]
    await cog._handle_claim_reaction(bounty, claimant)   # claimant reacts 🙋
    assert _state.active_bounties[mid]["status"] == "pending"
    assert _state.active_bounties[mid]["claimant_id"] == claimant
    # Author was DM'd accept/reject.
    assert bot.get_user(author).sent

    await cog._resolve_claim(_state.active_bounties.get(mid, bounty), accepted=True)
    assert await get_balance(claimant) == 10_000
    assert mid not in _state.active_bounties
    row = await _persistence.get_bounty_by_message(mid)
    assert row["status"] == "accepted"


async def test_concurrent_claims_only_first_wins(db):
    """Two users react 🙋 near-simultaneously; only one becomes the claimant."""
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    bounty = _state.active_bounties[mid]

    import asyncio
    await asyncio.gather(
        cog._handle_claim_reaction(bounty, 31),
        cog._handle_claim_reaction(bounty, 32),
    )
    # Exactly one claimant latched; status pending (not double-claimed).
    row = _state.active_bounties[mid]
    assert row["status"] == "pending"
    assert row["claimant_id"] in (31, 32)


# ── reject → contest → poll payout ────────────────────────────────────────────
async def test_reject_then_drop_refunds_author(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 40
    await cog._handle_claim_reaction(_state.active_bounties[mid], claimant)
    await cog._resolve_claim(_state.active_bounties[mid], accepted=False)
    assert _state.active_bounties[mid]["status"] == "contesting"

    # Claimant declines to contest → author refunded, terminal reject.
    await cog._resolve_contest(_state.active_bounties[mid], contested=False)
    assert await get_balance(author) == 10_000
    assert await get_balance(claimant) == 0
    row = await _persistence.get_bounty_by_message(mid)
    assert row["status"] == "rejected"


async def test_contest_starts_poll(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 41
    await cog._handle_claim_reaction(_state.active_bounties[mid], claimant)
    await cog._resolve_claim(_state.active_bounties[mid], accepted=False)
    await cog._resolve_contest(_state.active_bounties[mid], contested=True)

    row = _state.active_bounties[mid]
    assert row["status"] == "polling"
    assert row["poll_message_id"] is not None


async def test_poll_full_payout(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 42
    bounty = _state.active_bounties[mid]
    bounty["claimant_id"] = claimant
    await cog._settle_poll(bounty, yes=7, no=1, payout_frac=1.0)
    assert await get_balance(claimant) == 10_000
    assert await get_balance(author) == 0
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "accepted"


async def test_poll_partial_payout_splits(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 43
    bounty = _state.active_bounties[mid]
    bounty["claimant_id"] = claimant
    await cog._settle_poll(bounty, yes=5, no=5, payout_frac=0.5)
    assert await get_balance(claimant) == 5_000      # half to claimant
    assert await get_balance(author) == 5_000        # rest refunded
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "accepted"


async def test_poll_failed_refunds_author(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 44
    bounty = _state.active_bounties[mid]
    bounty["claimant_id"] = claimant
    await cog._settle_poll(bounty, yes=1, no=9, payout_frac=0.0)
    assert await get_balance(claimant) == 0
    assert await get_balance(author) == 10_000
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "rejected"


# ── expiry loop ───────────────────────────────────────────────────────────────
async def test_expired_contest_refunds_author(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 50
    await cog._handle_claim_reaction(_state.active_bounties[mid], claimant)
    await cog._resolve_claim(_state.active_bounties[mid], accepted=False)
    # Force the contest deadline into the past.
    _state.active_bounties[mid]["contest_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    assert await get_balance(author) == 10_000
    assert (await _persistence.get_bounty_by_message(mid))["status"] == "rejected"


async def test_expired_pending_offers_contest(db):
    cog, bot, channel = _make_cog()
    mid, author = await _open_bounty(cog, channel)
    claimant = 51
    await cog._handle_claim_reaction(_state.active_bounties[mid], claimant)
    _state.active_bounties[mid]["claim_expires_at"] = time.time() - 1

    await cog._expiry_loop.coro(cog)
    # Author non-response auto-rejects but offers the claimant a contest.
    assert _state.active_bounties[mid]["status"] == "contesting"
    assert bot.get_user(claimant).sent      # claimant got the contest DM


# ── payout fraction math ──────────────────────────────────────────────────────
async def test_poll_payout_fraction_boundaries():
    assert _poll_payout_fraction(0.0) == 0.0
    assert _poll_payout_fraction(0.49) == 0.0
    assert _poll_payout_fraction(0.5) == 0.5
    assert _poll_payout_fraction(2 / 3) == 1.0
    assert _poll_payout_fraction(1.0) == 1.0
    mid = _poll_payout_fraction(0.58)
    assert 0.5 < mid < 1.0
