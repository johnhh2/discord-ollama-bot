"""Permission tier gate tests for check_command_permission."""
import pytest

import src.state as _state
from src.permissions import check_command_permission

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio


def _set_perm(cmd: str, tier: str, hidden: bool = False) -> None:
    _state.command_perms[cmd] = {"tier": tier, "hidden": hidden}


# ── everyone ──────────────────────────────────────────────────────────────────

async def test_everyone_tier_allows_non_admin():
    _set_perm("greet", "everyone")
    ctx = FakeCtx(author=FakeMember(uid=1, administrator=False), command_name="greet")
    ok = await check_command_permission(ctx)
    assert ok is True
    assert ctx.sent_embeds == []


async def test_unlisted_command_defaults_to_everyone():
    # Don't add to perms — get_command_perm returns the default {"tier": "everyone"}.
    ctx = FakeCtx(author=FakeMember(uid=1), command_name="someundefinedcmd")
    ok = await check_command_permission(ctx)
    assert ok is True


# ── server_admin ──────────────────────────────────────────────────────────────

async def test_server_admin_blocks_non_admin_and_sends_denied_embed():
    _set_perm("admincmd", "server_admin")
    ctx = FakeCtx(
        author=FakeMember(uid=10, administrator=False),
        guild=FakeGuild(),
        command_name="admincmd",
    )
    ok = await check_command_permission(ctx)
    assert ok is False
    assert len(ctx.sent_embeds) == 1
    assert "No Permission" in ctx.sent_embeds[0].title


async def test_server_admin_allows_administrator():
    _set_perm("admincmd", "server_admin")
    ctx = FakeCtx(
        author=FakeMember(uid=10, administrator=True),
        guild=FakeGuild(),
        command_name="admincmd",
    )
    ok = await check_command_permission(ctx)
    assert ok is True


async def test_server_admin_allows_bot_admin_too():
    """can_manage_settings = is_admin OR is_server_admin, so bot_admins also pass."""
    _set_perm("admincmd", "server_admin")
    _state.bot_admins.add(99)
    ctx = FakeCtx(
        author=FakeMember(uid=99, administrator=False),
        guild=FakeGuild(),
        command_name="admincmd",
    )
    ok = await check_command_permission(ctx)
    assert ok is True


# ── bot_admin ─────────────────────────────────────────────────────────────────

async def test_bot_admin_blocks_server_admin():
    """Server admins do NOT count for bot_admin tier — only the bot_admins set."""
    _set_perm("godmode", "bot_admin")
    ctx = FakeCtx(
        author=FakeMember(uid=20, administrator=True),
        guild=FakeGuild(),
        command_name="godmode",
    )
    ok = await check_command_permission(ctx)
    assert ok is False


async def test_bot_admin_allows_when_uid_in_state_bot_admins():
    _set_perm("godmode", "bot_admin")
    _state.bot_admins.add(777)
    ctx = FakeCtx(
        author=FakeMember(uid=777),
        guild=FakeGuild(),
        command_name="godmode",
    )
    ok = await check_command_permission(ctx)
    assert ok is True


# ── hidden flag ───────────────────────────────────────────────────────────────

async def test_hidden_denied_command_is_silent():
    """When hidden=True, denied commands send no embed at all."""
    _set_perm("godmode", "bot_admin", hidden=True)
    ctx = FakeCtx(
        author=FakeMember(uid=30, administrator=False),
        guild=FakeGuild(),
        command_name="godmode",
    )
    ok = await check_command_permission(ctx)
    assert ok is False
    # No "No Permission" embed — silent denial.
    assert ctx.sent_embeds == []


async def test_hidden_does_not_affect_allowed_command():
    """hidden flag only matters on denial; allowed runs proceed normally."""
    _set_perm("godmode", "bot_admin", hidden=True)
    _state.bot_admins.add(40)
    ctx = FakeCtx(author=FakeMember(uid=40), command_name="godmode")
    ok = await check_command_permission(ctx)
    assert ok is True


# ── per-guild user overrides (state.user_perm_overrides) ─────────────────────

async def test_override_bot_admin_allows_gated_command():
    _set_perm("godmode", "bot_admin")
    _state.user_perm_overrides[(42, 100)] = "bot_admin"
    ctx = FakeCtx(
        author=FakeMember(uid=100, administrator=False),
        guild=FakeGuild(gid=42),
        command_name="godmode",
    )
    ok = await check_command_permission(ctx)
    assert ok is True


async def test_override_server_admin_does_not_grant_bot_admin():
    """A server_admin override must NOT pass a bot_admin tier check."""
    _set_perm("godmode", "bot_admin")
    _state.user_perm_overrides[(42, 100)] = "server_admin"
    ctx = FakeCtx(
        author=FakeMember(uid=100, administrator=False),
        guild=FakeGuild(gid=42),
        command_name="godmode",
    )
    ok = await check_command_permission(ctx)
    assert ok is False


async def test_override_is_per_guild_and_does_not_leak_across_guilds():
    """An override in guild A must not apply in guild B."""
    _set_perm("godmode", "bot_admin")
    _state.user_perm_overrides[(42, 100)] = "bot_admin"
    # Same user, different guild — no override row for this (guild, user).
    ctx = FakeCtx(
        author=FakeMember(uid=100, administrator=False),
        guild=FakeGuild(gid=999),
        command_name="godmode",
    )
    ok = await check_command_permission(ctx)
    assert ok is False


async def test_override_does_not_revoke_env_bot_admin():
    """Removing/missing an override row never demotes someone in BOT_ADMIN_IDS."""
    _set_perm("godmode", "bot_admin")
    _state.bot_admins.add(100)
    # No override row — the env-driven bot_admin must still pass.
    ctx = FakeCtx(
        author=FakeMember(uid=100, administrator=False),
        guild=FakeGuild(gid=42),
        command_name="godmode",
    )
    ok = await check_command_permission(ctx)
    assert ok is True


async def test_override_does_not_revoke_discord_server_admin():
    """A user with Discord administrator role keeps server_admin access even if no
    override exists. Overrides are additive only — they cannot demote."""
    _set_perm("admincmd", "server_admin")
    # No override row for this user — they only have Discord administrator.
    ctx = FakeCtx(
        author=FakeMember(uid=100, administrator=True),
        guild=FakeGuild(gid=42),
        command_name="admincmd",
    )
    ok = await check_command_permission(ctx)
    assert ok is True


async def test_override_in_dm_context_is_ignored():
    """ctx.guild is None → no override lookup. The bot_admin tier still gates."""
    _set_perm("godmode", "bot_admin")
    # Pretend the override existed for some guild_id; in a DM ctx.guild is None
    # so no key match is even attempted.
    _state.user_perm_overrides[(42, 100)] = "bot_admin"
    ctx = FakeCtx(
        author=FakeMember(uid=100, administrator=False),
        command_name="godmode",
    )
    ctx.guild = None  # FakeCtx replaces None in __init__, so set it after.
    ok = await check_command_permission(ctx)
    assert ok is False


async def test_override_bot_admin_grants_server_admin_too():
    """bot_admin is strictly higher than server_admin, so a bot_admin override
    must also pass a server_admin tier check."""
    _set_perm("admincmd", "server_admin")
    _state.user_perm_overrides[(42, 100)] = "bot_admin"
    ctx = FakeCtx(
        author=FakeMember(uid=100, administrator=False),
        guild=FakeGuild(gid=42),
        command_name="admincmd",
    )
    ok = await check_command_permission(ctx)
    assert ok is True
