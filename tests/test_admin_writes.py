"""Tier C: admin commands that mutate persistent state.

Read-path coverage already exists in test_permissions.py; these are the
write-path counterparts:
- !setperm   — writes state.command_perms + persists.
- !admingive — moves coins (works as positive credit, negative debit, or
  routes to guild_house when target is the bot user).
- !godmode  — toggles state.godmode_users + persists.

!reverse and !adminunlock are out of scope here — !reverse needs an async
channel.history(limit=100) iterator that's heavier than the testable
state mutation in it; !adminunlock follows the !shop unlock patterns
already covered.
"""
import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
from src.cogs.admin_cog import AdminCog
from src.cogs.economy_cog import EconomyCog

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio


class _StubBotUser:
    def __init__(self, uid: int = 999_999_999):
        self.id = uid


class _StubBot:
    def __init__(self):
        self.user = _StubBotUser()


def _admin_ctx(uid: int = 1, guild_id: int = 42) -> FakeCtx:
    """Build a FakeCtx whose author is in state.bot_admins so
    check_command_permission lets the command through."""
    author = FakeMember(uid=uid)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=guild_id))
    ctx.bot = _StubBot()
    _state.bot_admins.add(uid)
    return ctx


async def _read_db_balance(uid: int) -> int | None:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT balance FROM economy_users WHERE user_id=?", (uid,))
            row = await cur.fetchone()
    return row[0] if row else None


# ── !setperm ──────────────────────────────────────────────────────────────────

async def test_setperm_writes_state_and_persists(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "setperm"

    await cog.cmd_setperm.callback(
        cog, ctx, command_name="godmode", tier="bot_admin", hidden="true"
    )

    assert _state.command_perms["godmode"] == {"tier": "bot_admin", "hidden": True}
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT tier, hidden FROM command_perms WHERE command_name=?", ("godmode",)
            )
            row = await cur.fetchone()
    assert row == ("bot_admin", 1)


async def test_setperm_hidden_default_is_false(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "setperm"

    await cog.cmd_setperm.callback(
        cog, ctx, command_name="something", tier="everyone"
    )

    assert _state.command_perms["something"] == {"tier": "everyone", "hidden": False}


async def test_setperm_invalid_tier_does_not_write(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "setperm"

    await cog.cmd_setperm.callback(
        cog, ctx, command_name="thing", tier="moderator"
    )

    assert "thing" not in _state.command_perms
    assert any("Invalid Tier" in (e.title or "") for e in ctx.sent_embeds)


async def test_setperm_overwrites_existing_entry(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "setperm"

    _state.command_perms["thing"] = {"tier": "everyone", "hidden": False}
    await cog.cmd_setperm.callback(
        cog, ctx, command_name="thing", tier="server_admin", hidden="true"
    )

    assert _state.command_perms["thing"] == {"tier": "server_admin", "hidden": True}


async def test_setperm_blocked_when_caller_is_not_bot_admin(db):
    """Without bot_admins membership AND with a `bot_admin` tier configured,
    check_command_permission denies the call before any state mutation."""
    cog = AdminCog(bot=_StubBot())
    # Configure setperm itself as bot_admin (matches production command_perms.json).
    _state.command_perms["setperm"] = {"tier": "bot_admin", "hidden": True}
    # Note: NOT adding to bot_admins.
    ctx = FakeCtx(author=FakeMember(uid=99), guild=FakeGuild())
    ctx.bot = _StubBot()
    ctx.command.qualified_name = "setperm"

    await cog.cmd_setperm.callback(
        cog, ctx, command_name="thing", tier="everyone"
    )

    assert "thing" not in _state.command_perms


# ── !admingive ────────────────────────────────────────────────────────────────

async def test_admingive_positive_credits_target(db):
    cog = EconomyCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "admingive"
    target = FakeMember(uid=500, display_name="recipient")

    await cog.cmd_give.callback(cog, ctx, target=target, amount="1500")

    assert await _economy.get_balance(target.id) == 1500
    assert await _read_db_balance(target.id) == 1500


async def test_admingive_negative_debits_target(db):
    cog = EconomyCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "admingive"
    target = FakeMember(uid=501, display_name="victim")
    await _economy.add_balance(target.id, 1000)

    await cog.cmd_give.callback(cog, ctx, target=target, amount="-300")

    assert await _economy.get_balance(target.id) == 700
    assert await _read_db_balance(target.id) == 700


async def test_admingive_negative_clamps_at_zero(db):
    """A negative amount larger than balance is clamped so the user doesn't go negative."""
    cog = EconomyCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "admingive"
    target = FakeMember(uid=502)
    await _economy.add_balance(target.id, 100)

    await cog.cmd_give.callback(cog, ctx, target=target, amount="-99999")

    assert await _economy.get_balance(target.id) == 0


async def test_admingive_zero_amount_rejected(db):
    cog = EconomyCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "admingive"
    target = FakeMember(uid=503)
    await _economy.add_balance(target.id, 1000)

    await cog.cmd_give.callback(cog, ctx, target=target, amount="0")

    # 0 is treated as invalid — balance untouched.
    assert await _economy.get_balance(target.id) == 1000
    assert any("Invalid Amount" in (e.title or "") for e in ctx.sent_embeds)


async def test_admingive_to_bot_user_routes_to_guild_house(db):
    cog = EconomyCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=77)
    ctx.command.qualified_name = "admingive"
    bot_target = FakeMember(uid=999_999_999)  # matches _StubBotUser.id

    await cog.cmd_give.callback(cog, ctx, target=bot_target, amount="2500")

    assert _economy.get_guild_house_balance(77) == 2500
    # Bot user has no normal balance row.
    assert await _read_db_balance(bot_target.id) is None


async def test_admingive_negative_to_bot_user_drains_guild_house(db):
    cog = EconomyCog(bot=_StubBot())
    ctx = _admin_ctx(guild_id=78)
    ctx.command.qualified_name = "admingive"
    await _economy.add_guild_house(78, 1000)

    bot_target = FakeMember(uid=999_999_999)
    await cog.cmd_give.callback(cog, ctx, target=bot_target, amount="-400")

    assert _economy.get_guild_house_balance(78) == 600


# ── !godmode ──────────────────────────────────────────────────────────────────

async def test_godmode_toggles_user_into_set(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "godmode"
    target = FakeMember(uid=600)

    await cog.cmd_godmode.callback(cog, ctx, user=target)

    assert target.id in _state.godmode_users
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM godmode_users WHERE user_id=?", (target.id,))
            assert await cur.fetchone() is not None


async def test_godmode_second_invocation_toggles_off(db):
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx()
    ctx.command.qualified_name = "godmode"
    target = FakeMember(uid=601)
    _state.godmode_users.add(target.id)

    await cog.cmd_godmode.callback(cog, ctx, user=target)

    assert target.id not in _state.godmode_users
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM godmode_users WHERE user_id=?", (target.id,))
            assert await cur.fetchone() is None


async def test_godmode_no_user_arg_targets_caller(db):
    """`!godmode` with no arg toggles the caller themselves."""
    cog = AdminCog(bot=_StubBot())
    ctx = _admin_ctx(uid=700)
    ctx.command.qualified_name = "godmode"

    await cog.cmd_godmode.callback(cog, ctx, user=None)

    assert 700 in _state.godmode_users
