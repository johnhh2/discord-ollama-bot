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
