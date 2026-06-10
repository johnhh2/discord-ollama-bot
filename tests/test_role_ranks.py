"""Per-guild bot-role rank: !shop roleup / !shop roledown swap adjacent
ranks deterministically, and the !roles leaderboard reads from the
stored rank rather than Discord's shared role.position.

Before the rank column existed, ranking compared role.position values
that mixed bot and non-bot roles. A bot role at Discord position 3 with
no other bot roles above it would jump straight to the top of the bot
pile in one move (visible as "#3 → #1") because the code only looked
for *bot* roles with higher position. Storing rank in the DB makes the
adjacency well-defined: each roleup swaps exactly two adjacent ranks.

These tests pin:
  - shop_createrole seeds the new role at max(rank)+1 (bottom of the
    rank ladder, lowest priority).
  - shop_roleup swaps the role with the next-higher rank, and *only*
    that one — never jumping multiple positions.
  - shop_roledown is the symmetric case.
  - Ranks are per-guild: another guild's roles never participate.
  - Boundary cases (already highest / already lowest) refuse and refund.
  - shop_deleterole drops the rank entry so it doesn't leak.
"""
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
from src.cogs.shop_cog import ShopCog
from src.economy import add_balance, get_balance
from src.config import (
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_MOVE_COST, SHOP_ROLE_DELETE_COST,
)

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeRole


pytestmark = pytest.mark.asyncio


@pytest.fixture
def force_member_converter_fallback(monkeypatch):
    """Make discord.py's built-in MemberConverter raise BadArgument so the
    project's substring-fallback path runs."""
    from discord.ext import commands as dpy_commands

    async def _bad(self, ctx, argument):
        raise dpy_commands.BadArgument(f"forced fallback for {argument!r}")
    monkeypatch.setattr(dpy_commands.MemberConverter, "convert", _bad)


def _make_ranked_guild(gid: int, ranked_roles: list[tuple[int, int, str]]) -> FakeGuild:
    """Build a FakeGuild with the given (role_id, rank, name) entries
    seeded into _state.bot_roles and _state.bot_role_ranks."""
    guild = FakeGuild(gid=gid)
    # Position only matters for the best-effort Discord mirror; give each
    # role a distinct ascending position so role.edit(position=...) calls
    # have something to swap.
    for i, (rid, rank, name) in enumerate(sorted(ranked_roles, key=lambda x: -x[1])):
        role = FakeRole(role_id=rid, name=name)
        role.position = i + 1
        guild.roles.append(role)
        _state.bot_roles.add(rid)
        _state.bot_role_ranks[(gid, rid)] = rank
    # Bot member with a top role above all bot roles (used by old
    # implementation; harmless to keep).
    me = FakeMember(uid=99999, display_name="bot")
    me.top_role = type("R", (), {"position": 999})()
    guild.me = me
    return guild


# ── shop_createrole seeds rank at bottom ──────────────────────────────────────

async def test_shop_createrole_first_role_gets_rank_1(db, force_member_converter_fallback):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=2001)
    target = FakeMember(uid=2002, display_name="target")
    await add_balance(buyer.id, SHOP_ROLE_CREATE_COST + 1000)

    guild = FakeGuild(gid=100)
    guild.members = [buyer, target]
    new_role = FakeRole(role_id=10001, name="First")
    guild.create_role = AsyncMock(return_value=new_role)
    target.add_roles = AsyncMock()
    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_createrole.callback(cog, ctx, "target", "First")

    assert _state.bot_role_ranks[(100, 10001)] == 1


async def test_shop_createrole_subsequent_role_gets_bottom_rank(db, force_member_converter_fallback):
    """A second role in the same guild lands at max(rank)+1 — bottom of
    the ladder, not the top. Users earn their way up via roleup."""
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=2003)
    target = FakeMember(uid=2004, display_name="target")
    await add_balance(buyer.id, SHOP_ROLE_CREATE_COST + 1000)

    # Pre-existing role at rank 1.
    _state.bot_roles.add(7777)
    _state.bot_role_ranks[(100, 7777)] = 1

    guild = FakeGuild(gid=100)
    guild.members = [buyer, target]
    new_role = FakeRole(role_id=10002, name="Second")
    guild.create_role = AsyncMock(return_value=new_role)
    target.add_roles = AsyncMock()
    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_createrole.callback(cog, ctx, "target", "Second")

    assert _state.bot_role_ranks[(100, 10002)] == 2, (
        "New role must land at the bottom (max(rank)+1), not displace existing ranks"
    )
    # Existing rank untouched.
    assert _state.bot_role_ranks[(100, 7777)] == 1


# ── shop_roleup swaps one position at a time ──────────────────────────────────

async def test_shop_roleup_swaps_with_immediate_neighbor_not_jumping(db):
    """The #3 → #1 bug: pre-fix, a bot role with no other bot roles
    above it would jump straight to #1. Fixed: roleup swaps with the
    role at the next-lower rank number (#3 ↔ #2)."""
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3001)
    await add_balance(buyer.id, SHOP_ROLE_MOVE_COST + 1000)

    guild = _make_ranked_guild(200, [
        (501, 1, "Top"),
        (502, 2, "Middle"),
        (503, 3, "Bottom"),
    ])
    ctx = FakeCtx(author=buyer, guild=guild)
    ctx.invoked_with = "roleup"

    # Roleup the bottom (#3) role. It must become #2, not #1.
    await cog.shop_roleup.callback(cog, ctx, "<@&503>")

    assert _state.bot_role_ranks[(200, 503)] == 2, "#3 must move to #2 (swap), not #1"
    assert _state.bot_role_ranks[(200, 502)] == 3, "#2 must move down to fill #3"
    assert _state.bot_role_ranks[(200, 501)] == 1, "#1 must be untouched"


async def test_shop_roleup_at_top_refuses_and_refunds(db):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3002)
    starting = SHOP_ROLE_MOVE_COST + 1000
    await add_balance(buyer.id, starting)

    guild = _make_ranked_guild(201, [(601, 1, "Only Top")])
    # Need at least one other role to have a rank ladder.
    guild.roles.append(FakeRole(role_id=602, name="Bottom"))
    guild.roles[-1].position = 0
    _state.bot_roles.add(602)
    _state.bot_role_ranks[(201, 602)] = 2

    ctx = FakeCtx(author=buyer, guild=guild)
    ctx.invoked_with = "roleup"
    await cog.shop_roleup.callback(cog, ctx, "<@&601>")

    assert await get_balance(buyer.id) == starting, "no charge on already-highest"
    assert _state.bot_role_ranks[(201, 601)] == 1, "rank untouched"


# ── shop_roledown is the symmetric case ──────────────────────────────────────

async def test_shop_roledown_swaps_with_immediate_neighbor(db):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3003)
    await add_balance(buyer.id, SHOP_ROLE_MOVE_COST + 1000)

    guild = _make_ranked_guild(202, [
        (701, 1, "Top"),
        (702, 2, "Middle"),
        (703, 3, "Bottom"),
    ])
    ctx = FakeCtx(author=buyer, guild=guild)
    # Need to set invoked_with for direction dispatch.
    ctx.invoked_with = "roledown"

    await cog.shop_roleup.callback(cog, ctx, "<@&701>")

    assert _state.bot_role_ranks[(202, 701)] == 2, "Top must drop to #2"
    assert _state.bot_role_ranks[(202, 702)] == 1, "Middle must rise to #1"
    assert _state.bot_role_ranks[(202, 703)] == 3, "Bottom untouched"


async def test_shop_roledown_at_bottom_refuses_and_refunds(db):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3004)
    starting = SHOP_ROLE_MOVE_COST + 1000
    await add_balance(buyer.id, starting)

    guild = _make_ranked_guild(203, [
        (801, 1, "Top"),
        (802, 2, "Bottom"),
    ])
    ctx = FakeCtx(author=buyer, guild=guild)
    ctx.invoked_with = "roledown"

    await cog.shop_roleup.callback(cog, ctx, "<@&802>")

    assert await get_balance(buyer.id) == starting
    assert _state.bot_role_ranks[(203, 802)] == 2


# ── Per-guild scoping ────────────────────────────────────────────────────────

async def test_shop_roleup_only_considers_same_guild_roles(db):
    """Two guilds have independent rank ladders. Roleup in guild A must
    never touch ranks in guild B."""
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3005)
    await add_balance(buyer.id, SHOP_ROLE_MOVE_COST + 1000)

    # Guild A: 901 (#1), 902 (#2). Guild B: 911 (#1), 912 (#2).
    guild_a = _make_ranked_guild(300, [
        (901, 1, "A-Top"),
        (902, 2, "A-Bottom"),
    ])
    # Seed guild B's ranks (no need to mock its guild object — we never
    # use it; just want to ensure the call doesn't see B's roles).
    _state.bot_roles.add(911)
    _state.bot_roles.add(912)
    _state.bot_role_ranks[(301, 911)] = 1
    _state.bot_role_ranks[(301, 912)] = 2

    ctx = FakeCtx(author=buyer, guild=guild_a)
    ctx.invoked_with = "roleup"
    await cog.shop_roleup.callback(cog, ctx, "<@&902>")

    # Guild A swap happened.
    assert _state.bot_role_ranks[(300, 902)] == 1
    assert _state.bot_role_ranks[(300, 901)] == 2
    # Guild B untouched.
    assert _state.bot_role_ranks[(301, 911)] == 1
    assert _state.bot_role_ranks[(301, 912)] == 2


# ── shop_deleterole cleans up rank ───────────────────────────────────────────

async def test_shop_deleterole_drops_rank_entry(db):
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3006)
    await add_balance(buyer.id, SHOP_ROLE_DELETE_COST + 1000)

    guild = _make_ranked_guild(400, [
        (1101, 1, "Doomed"),
        (1102, 2, "Survivor"),
    ])
    # shop_deleterole iterates role.members for the insurance check; no
    # members in this test means it skips that branch cleanly.
    for r in guild.roles:
        r.members = []
    ctx = FakeCtx(author=buyer, guild=guild)

    await cog.shop_deleterole.callback(cog, ctx, "<@&1101>")

    assert 1101 not in _state.bot_roles
    assert (400, 1101) not in _state.bot_role_ranks, (
        "Rank entry must be dropped when the role is deleted"
    )
    # Other role's rank survives (gaps are allowed).
    assert _state.bot_role_ranks[(400, 1102)] == 2


# ── Discord mirror is best-effort ────────────────────────────────────────────

async def test_shop_roleup_keeps_db_rank_even_when_discord_mirror_fails(db):
    """The DB rank is the source of truth. If role.edit(position=...)
    raises Forbidden, the rank swap in DB must stick — the Discord
    sidebar may look stale, but the bot's view stays consistent."""
    cog = ShopCog(bot=None)
    buyer = FakeMember(uid=3007)
    await add_balance(buyer.id, SHOP_ROLE_MOVE_COST + 1000)

    guild = _make_ranked_guild(500, [
        (1201, 1, "Top"),
        (1202, 2, "Bottom"),
    ])
    # Make role.edit raise Forbidden for both roles.
    forbidden = discord.Forbidden(
        response=type("R", (), {"status": 403, "reason": "no"})(),
        message="no perms",
    )
    for r in guild.roles:
        r.edit = AsyncMock(side_effect=forbidden)

    ctx = FakeCtx(author=buyer, guild=guild)
    ctx.invoked_with = "roleup"
    await cog.shop_roleup.callback(cog, ctx, "<@&1202>")

    # DB swap stuck despite Discord rejection.
    assert _state.bot_role_ranks[(500, 1202)] == 1
    assert _state.bot_role_ranks[(500, 1201)] == 2
