"""Tier 2: shop subcommands beyond nickname/insurance.

Pattern is the same for every paid !shop subcommand:
1. Charge via shop_charge (covered exhaustively in test_shop.py).
2. Perform a Discord-API or state-mutation action.
3. Persist via save_*.
4. On Discord-API failure, refund the cost.

Rather than testing 25 near-duplicates, this picks one representative for
each shape: a role op (createrole), a channel op (lockchannel/unlockchannel),
and the three timed effects (mock, curse, tax) that persist to shop_effects.
"""
import pytest
from unittest.mock import AsyncMock

import discord
from discord.ext import commands as dpy_commands

import src.state as _state
import src.persistence as _persistence
from src.cogs.shop_cog import ShopCog
from src.economy import add_balance, get_balance
from src.config import (
    SHOP_ROLE_CREATE_COST, SHOP_LOCK_COST, SHOP_MOCK_COST,
    SHOP_MOCK_MESSAGES, SHOP_CURSE_COST, SHOP_CURSE_MESSAGES,
    SHOP_TAX_COST, SHOP_NICKNAME_SELF_COST, SHOP_RENAME_COST,
)

from tests.fakes.discord import (
    FakeCtx, FakeMember, FakeGuild, FakeRole, FakeTextChannel,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture
def force_member_converter_fallback(monkeypatch):
    """Make discord.py's built-in MemberConverter raise BadArgument so the
    project's substring-fallback path runs. Required to drive any shop
    subcommand that uses MemberConverter without a real Discord cache.
    """
    async def _bad(self, ctx, argument):
        raise dpy_commands.BadArgument(f"forced fallback for {argument!r}")
    monkeypatch.setattr(dpy_commands.MemberConverter, "convert", _bad)


async def _read_db_balance(uid: int) -> int | None:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT balance FROM economy_users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
    return row[0] if row else None


async def _read_shop_effect(uid: int, effect_type: str) -> tuple | None:
    """Return the (user_id, effect_type, remaining, started_by, cursed_by,
    master_id, tax_type, tax_emoji, channel_id) row for a given effect, or None."""
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id, effect_type, remaining, started_by, cursed_by,"
                " master_id, tax_type, tax_emoji, channel_id"
                " FROM shop_effects WHERE user_id=? AND effect_type=?",
                (uid, effect_type),
            )
            return await cur.fetchone()


# ── Roles: shop_createrole ────────────────────────────────────────────────────

async def test_shop_createrole_creates_role_and_persists(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=1001, display_name="buyer")
    target = FakeMember(uid=1002, display_name="target")
    await add_balance(buyer.id, SHOP_ROLE_CREATE_COST + 1000)

    guild = FakeGuild(gid=42)
    guild.members = [buyer, target]
    new_role = FakeRole(role_id=900, name="MyRole")
    guild.create_role = AsyncMock(return_value=new_role)
    target.add_roles = AsyncMock()

    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_createrole.callback(cog, ctx, "target", "MyRole")

    assert await get_balance(buyer.id) == 1000
    assert await _read_db_balance(buyer.id) == 1000
    guild.create_role.assert_awaited_once()
    # No color param anymore — the role is created uncolored.
    assert "color" not in guild.create_role.call_args.kwargs
    target.add_roles.assert_awaited_once_with(new_role)
    assert new_role.id in _state.bot_roles


async def test_shop_createrole_rejects_admin_in_name(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=1003)
    target = FakeMember(uid=1004, display_name="target")
    await add_balance(buyer.id, SHOP_ROLE_CREATE_COST + 1000)

    guild = FakeGuild(gid=42)
    guild.members = [buyer, target]
    guild.create_role = AsyncMock()
    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_createrole.callback(cog, ctx, "target", "ADMIN of stuff")

    # Rejected before charging.
    assert await get_balance(buyer.id) == SHOP_ROLE_CREATE_COST + 1000
    guild.create_role.assert_not_called()


async def test_shop_createrole_refunds_on_forbidden(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=1007)
    target = FakeMember(uid=1008, display_name="target")
    starting = SHOP_ROLE_CREATE_COST + 500
    await add_balance(buyer.id, starting)

    guild = FakeGuild(gid=42)
    guild.members = [buyer, target]
    guild.create_role = AsyncMock(side_effect=discord.Forbidden(
        response=type("R", (), {"status": 403, "reason": "no"})(),
        message="forbidden",
    ))
    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_createrole.callback(cog, ctx, "target", "Cool")

    # Refunded back to starting balance.
    assert await get_balance(buyer.id) == starting
    assert await _read_db_balance(buyer.id) == starting


# ── Length caps: rejects oversize input before charging or hitting Discord ────

async def test_shop_nickname_rejects_oversize_name(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=1101, display_name="buyer")
    starting = SHOP_NICKNAME_SELF_COST + 1000
    await add_balance(buyer.id, starting)

    guild = FakeGuild(gid=43)
    guild.members = [buyer]
    ctx = FakeCtx(author=buyer, guild=guild)

    long_name = "x" * 33
    await cog.shop_nickname.callback(cog, ctx, long_name)

    # Rejected before charging and before any edit attempt.
    assert await get_balance(buyer.id) == starting
    buyer.edit.assert_not_called()
    assert any("Too Long" in e.title for e in ctx.sent_embeds)


async def test_shop_createrole_rejects_oversize_name(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=1102)
    target = FakeMember(uid=1103, display_name="target")
    starting = SHOP_ROLE_CREATE_COST + 1000
    await add_balance(buyer.id, starting)

    guild = FakeGuild(gid=44)
    guild.members = [buyer, target]
    guild.create_role = AsyncMock()
    ctx = FakeCtx(author=buyer, guild=guild)

    long_name = "r" * 101
    await cog.shop_createrole.callback(cog, ctx, "target", long_name)

    assert await get_balance(buyer.id) == starting
    guild.create_role.assert_not_called()
    assert any("Too Long" in e.title for e in ctx.sent_embeds)


async def test_shop_renamerole_rejects_oversize_name(db):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=1104)
    starting = SHOP_RENAME_COST + 1000
    await add_balance(buyer.id, starting)

    guild = FakeGuild(gid=45)
    role = FakeRole(role_id=901, name="OldName")
    guild.roles = [role]
    _state.bot_roles.add(role.id)
    ctx = FakeCtx(author=buyer, guild=guild)

    long_name = "r" * 101
    arg = f"<@&{role.id}> | {long_name}"
    await cog.shop_renamerole.callback(cog, ctx, *arg.split())

    assert await get_balance(buyer.id) == starting
    role.edit.assert_not_called()
    assert any("Too Long" in e.title for e in ctx.sent_embeds)


# ── Channels: shop_lockchannel + shop_unlockchannel ───────────────────────────

async def test_shop_lockchannel_charges_and_records_owner(db):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=2001)
    await add_balance(buyer.id, SHOP_LOCK_COST + 5000)

    guild = FakeGuild(gid=77)
    chan = FakeTextChannel(ch_id=8888, name="general")
    guild.channels = [chan]
    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_lockchannel.callback(cog, ctx, str(chan.id))

    assert await get_balance(buyer.id) == 5000
    assert _state.locked_channels.get(chan.id) == buyer.id
    cfg = _state.guild_settings[str(guild.id)]
    assert cfg["locked_channels"][str(chan.id)] == buyer.id


async def test_shop_lockchannel_rejects_already_locked(db):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=2002)
    await add_balance(buyer.id, SHOP_LOCK_COST + 5000)

    guild = FakeGuild(gid=78)
    chan = FakeTextChannel(ch_id=8889)
    guild.channels = [chan]
    _state.locked_channels[chan.id] = 9999  # someone else owns it

    ctx = FakeCtx(author=buyer, guild=guild)
    await cog.shop_lockchannel.callback(cog, ctx, str(chan.id))

    # Not charged.
    assert await get_balance(buyer.id) == SHOP_LOCK_COST + 5000
    # Owner unchanged.
    assert _state.locked_channels[chan.id] == 9999


async def test_shop_unlockchannel_owner_can_unlock(db):
    cog = ShopCog(bot=None)
    owner = FakeMember(uid=2003)
    guild = FakeGuild(gid=79)
    chan = FakeTextChannel(ch_id=8890)
    guild.channels = [chan]
    _state.locked_channels[chan.id] = owner.id

    ctx = FakeCtx(author=owner, guild=guild)
    await cog.shop_unlockchannel.callback(cog, ctx, str(chan.id))

    assert chan.id not in _state.locked_channels


async def test_shop_unlockchannel_non_owner_rejected(db):
    cog = ShopCog(bot=None)
    owner = FakeMember(uid=2004)
    other = FakeMember(uid=2005)
    guild = FakeGuild(gid=80)
    chan = FakeTextChannel(ch_id=8891)
    guild.channels = [chan]
    _state.locked_channels[chan.id] = owner.id

    ctx = FakeCtx(author=other, guild=guild)
    await cog.shop_unlockchannel.callback(cog, ctx, str(chan.id))

    # Still locked, still owned by `owner`.
    assert _state.locked_channels[chan.id] == owner.id


# ── Timed effects: shop_mock ──────────────────────────────────────────────────

async def test_shop_mock_charges_and_writes_state_and_db(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3001, display_name="buyer")
    target = FakeMember(uid=3002, display_name="target")
    await add_balance(buyer.id, SHOP_MOCK_COST + 1000)

    guild = FakeGuild(gid=42)
    guild.members = [buyer, target]
    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_mock.callback(cog, ctx, "target")

    assert await get_balance(buyer.id) == 1000
    assert (42, target.id) in _state.active_mocks
    entry = _state.active_mocks[(42, target.id)]
    assert entry["remaining"] == SHOP_MOCK_MESSAGES
    assert entry["started_by"] == buyer.id

    # Persisted to shop_effects.
    row = await _read_shop_effect(target.id, "mock")
    assert row is not None
    assert row[0] == target.id and row[1] == "mock"
    assert row[2] == SHOP_MOCK_MESSAGES        # remaining
    assert row[3] == buyer.id                  # started_by


# ── Timed effects: shop_curse ─────────────────────────────────────────────────

async def test_shop_curse_charges_and_persists(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3003)
    target = FakeMember(uid=3004, display_name="target")
    await add_balance(buyer.id, SHOP_CURSE_COST + 1000)

    guild = FakeGuild(gid=42)
    guild.members = [buyer, target]
    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_curse.callback(cog, ctx, "target")

    assert await get_balance(buyer.id) == 1000
    assert (42, target.id) in _state.active_curses
    assert _state.active_curses[(42, target.id)]["remaining"] == SHOP_CURSE_MESSAGES
    assert _state.active_curses[(42, target.id)]["cursed_by"] == buyer.id

    row = await _read_shop_effect(target.id, "curse")
    assert row is not None
    assert row[2] == SHOP_CURSE_MESSAGES        # remaining
    assert row[4] == buyer.id                   # cursed_by


async def test_shop_curse_self_target_rejected(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3005, display_name="buyer")
    await add_balance(buyer.id, SHOP_CURSE_COST + 1000)

    guild = FakeGuild(gid=42)
    guild.members = [buyer]
    ctx = FakeCtx(author=buyer, guild=guild)

    # Substring "buyer" matches buyer (the only member) — that's the self-target.
    await cog.shop_curse.callback(cog, ctx, "buyer")

    # Not charged.
    assert await get_balance(buyer.id) == SHOP_CURSE_COST + 1000
    assert (42, buyer.id) not in _state.active_curses


# ── Timed effects: shop_tax ───────────────────────────────────────────────────

async def test_shop_tax_default_writes_state_with_tax_type_and_emoji(
    db, force_member_converter_fallback
):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=4001, display_name="buyer")
    target = FakeMember(uid=4002, display_name="target")
    await add_balance(buyer.id, SHOP_TAX_COST + 1000)

    guild = FakeGuild(gid=42)
    guild.members = [buyer, target]
    ctx = FakeCtx(author=buyer, guild=guild)
    # invoked_with isn't an alias → defaults to ("tax", "💰")
    ctx.invoked_with = "tax"

    await cog.shop_tax.callback(cog, ctx, "target")

    assert await get_balance(buyer.id) == 1000
    assert (42, target.id) in _state.active_taxes
    entry = _state.active_taxes[(42, target.id)]
    assert entry["master"] == buyer.id
    assert entry["type"] == "tax"
    assert entry["emoji"] == "💰"

    row = await _read_shop_effect(target.id, "tax")
    assert row is not None
    assert row[5] == buyer.id     # master_id
    assert row[6] == "tax"        # tax_type
    assert row[7] == "💰"          # tax_emoji


async def test_shop_tax_alias_uses_guild_emoji(db, force_member_converter_fallback):
    """When invoked_with matches a guild-configured tax_alias, store that
    type and emoji instead of the defaults."""
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=4003)
    target = FakeMember(uid=4004, display_name="target")
    await add_balance(buyer.id, SHOP_TAX_COST + 1000)

    guild = FakeGuild(gid=42)
    guild.members = [buyer, target]
    # Configure an alias.
    _state.guild_settings[str(guild.id)] = {
        "tax_aliases": {"toll": "🛣️"},
    }

    ctx = FakeCtx(author=buyer, guild=guild)
    ctx.invoked_with = "toll"

    await cog.shop_tax.callback(cog, ctx, "target")

    entry = _state.active_taxes[(42, target.id)]
    assert entry["type"] == "toll"
    assert entry["emoji"] == "🛣️"


async def test_shop_tax_self_rejected(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=4005, display_name="onlyme")
    await add_balance(buyer.id, SHOP_TAX_COST + 1000)

    guild = FakeGuild(gid=42)
    guild.members = [buyer]
    ctx = FakeCtx(author=buyer, guild=guild)
    ctx.invoked_with = "tax"

    await cog.shop_tax.callback(cog, ctx, "onlyme")

    assert await get_balance(buyer.id) == SHOP_TAX_COST + 1000
    assert buyer.id not in _state.active_taxes


# ── Top-level alias table sanity ──────────────────────────────────────────────
# These guard against the original wrapper-typo class of bug (e.g. cmd_roledown
# silently routing to shop_roleup with no direction adjustment) by verifying the
# declarative alias table is internally consistent.

async def test_shop_top_aliases_resolve_to_real_methods():
    """Every (top_name, sub_attr, legacy_aliases) entry must point at a method
    that exists on ShopCog. A typo in sub_attr would have crashed at cog load
    time, but the test makes the guarantee explicit so refactors get a clear
    failure."""
    from src.cogs.shop_cog import _SHOP_TOP_ALIASES, ShopCog
    cog = ShopCog(bot=None)
    for top_name, sub_attr, _ in _SHOP_TOP_ALIASES:
        assert hasattr(cog, sub_attr), (
            f"!{top_name} alias points at ShopCog.{sub_attr} which does not exist"
        )


async def test_shop_roleup_and_roledown_share_handler_with_directional_dispatch():
    """!roleup and !roledown must both dispatch to shop_roleup, which uses
    ctx.invoked_with to pick direction. If a future edit splits them onto
    different handlers, the dispatch contract has to change too."""
    from src.cogs.shop_cog import _SHOP_TOP_ALIASES
    table = {top: sub for top, sub, _ in _SHOP_TOP_ALIASES}
    assert table["roleup"] == "shop_roleup"
    assert table["roledown"] == "shop_roleup", (
        "Both top-level aliases must route to shop_roleup so its "
        "ctx.invoked_with branch picks the right direction."
    )
