"""@requires_perm decorator behavior.

`requires_perm` (src/permissions.py) wraps a cog command so it short-circuits
when the author lacks the configured tier. It's applied to 52 sites across
6 cogs; tests there cover those sites *indirectly*. This file covers the
decorator's contract directly so a future maintainer changing it can't
silently break those 52 sites.

Contract:
- For tier=everyone: call always proceeds, decorator passes args/kwargs through.
- For tier=server_admin: passes for admin guild_permissions OR bot_admin uid;
  blocks otherwise.
- For tier=bot_admin: passes only for bot_admin uid.
- When blocked + hidden=False: a "❌ No Permission" embed is sent via ctx.send.
- When blocked + hidden=True: silently returns; no embed sent.
- Decorator preserves the underlying function's args/kwargs (functools.wraps).
"""
import pytest

import src.state as _state
from src.permissions import requires_perm

from tests.fakes.discord import FakeMember, FakeGuild, FakeCtx


pytestmark = pytest.mark.asyncio


class _DummyCog:
    """A minimal cog-like object so `wrapper(self, ctx, *args)` has a `self`."""
    def __init__(self):
        self.calls: list[tuple] = []

    @requires_perm
    async def cmd_protected(self, ctx, *args, **kwargs):
        """Records that we made it past the gate."""
        self.calls.append((args, kwargs))
        return "called"


def _ctx_with_perm(command_name: str, *, admin: bool = False) -> FakeCtx:
    """FakeCtx whose .command.qualified_name matches the perm-table key."""
    author = FakeMember(uid=1, administrator=admin)
    guild = FakeGuild(gid=42)
    ctx = FakeCtx(author=author, guild=guild, command_name=command_name)
    return ctx


# ── tier=everyone ─────────────────────────────────────────────────────────────

async def test_everyone_tier_lets_anyone_through():
    _state.command_perms["mycmd"] = {"tier": "everyone", "hidden": False}
    cog = _DummyCog()
    ctx = _ctx_with_perm("mycmd", admin=False)

    result = await cog.cmd_protected.__wrapped__(cog, ctx) if False else await cog.cmd_protected(ctx)

    assert result == "called"
    assert cog.calls == [((), {})]


async def test_command_not_in_table_defaults_to_everyone():
    """get_command_perm returns {tier: everyone} for unknown commands."""
    cog = _DummyCog()
    ctx = _ctx_with_perm("never-registered-cmd", admin=False)

    await cog.cmd_protected(ctx)
    assert cog.calls == [((), {})]


# ── tier=server_admin ─────────────────────────────────────────────────────────

async def test_server_admin_tier_blocks_non_admin_with_visible_embed():
    _state.command_perms["adminonly"] = {"tier": "server_admin", "hidden": False}
    cog = _DummyCog()
    ctx = _ctx_with_perm("adminonly", admin=False)

    await cog.cmd_protected(ctx)

    # Body never executed.
    assert cog.calls == []
    # "No Permission" embed sent.
    assert ctx.sent_embeds, "expected a No Permission embed"
    embed = ctx.sent_embeds[0]
    assert "No Permission" in embed.title


async def test_server_admin_tier_allows_administrator_member():
    _state.command_perms["adminonly2"] = {"tier": "server_admin", "hidden": False}
    cog = _DummyCog()
    ctx = _ctx_with_perm("adminonly2", admin=True)

    await cog.cmd_protected(ctx)

    assert cog.calls == [((), {})]
    assert ctx.sent_embeds == []


async def test_server_admin_tier_allows_bot_admin_uid():
    """is_admin() returns True for any uid in state.bot_admins, even if the
    Discord member isn't a server admin."""
    _state.command_perms["adminonly3"] = {"tier": "server_admin", "hidden": False}
    cog = _DummyCog()
    ctx = _ctx_with_perm("adminonly3", admin=False)
    _state.bot_admins.add(ctx.author.id)

    await cog.cmd_protected(ctx)

    assert cog.calls == [((), {})]


# ── tier=bot_admin ────────────────────────────────────────────────────────────

async def test_bot_admin_tier_blocks_server_admin():
    """server_admin permissions are NOT enough for a bot_admin command."""
    _state.command_perms["botonly"] = {"tier": "bot_admin", "hidden": False}
    cog = _DummyCog()
    ctx = _ctx_with_perm("botonly", admin=True)  # admin perms but NOT in bot_admins

    await cog.cmd_protected(ctx)

    assert cog.calls == []
    assert ctx.sent_embeds, "expected a No Permission embed"


async def test_bot_admin_tier_allows_bot_admin_uid():
    _state.command_perms["botonly2"] = {"tier": "bot_admin", "hidden": False}
    cog = _DummyCog()
    ctx = _ctx_with_perm("botonly2", admin=False)
    _state.bot_admins.add(ctx.author.id)

    await cog.cmd_protected(ctx)

    assert cog.calls == [((), {})]


# ── hidden flag ───────────────────────────────────────────────────────────────

async def test_hidden_true_blocks_silently_no_embed():
    """When hidden=True, denied callers get no feedback at all (the
    command should appear to not exist)."""
    _state.command_perms["secret"] = {"tier": "bot_admin", "hidden": True}
    cog = _DummyCog()
    ctx = _ctx_with_perm("secret", admin=False)

    await cog.cmd_protected(ctx)

    assert cog.calls == []
    assert ctx.sent_embeds == [], "hidden=True should not send an embed"


async def test_hidden_true_does_not_affect_allowed_callers():
    """hidden only changes the *blocked* path; allowed callers still work."""
    _state.command_perms["secret2"] = {"tier": "bot_admin", "hidden": True}
    cog = _DummyCog()
    ctx = _ctx_with_perm("secret2", admin=False)
    _state.bot_admins.add(ctx.author.id)

    await cog.cmd_protected(ctx)

    assert cog.calls == [((), {})]


# ── arg/kwarg passthrough ─────────────────────────────────────────────────────

async def test_decorator_forwards_positional_and_keyword_args():
    """If functools.wraps were dropped or args mishandled, MemberConverter
    arguments would silently disappear. Lock that in."""
    _state.command_perms["forward"] = {"tier": "everyone", "hidden": False}
    cog = _DummyCog()
    ctx = _ctx_with_perm("forward")

    await cog.cmd_protected(ctx, "pos1", "pos2", kw1="value", kw2=42)

    assert cog.calls == [(("pos1", "pos2"), {"kw1": "value", "kw2": 42})]


async def test_decorator_returns_underlying_function_value():
    """If the wrapper swallowed the return value, callers expecting it would
    silently see None. Lock that in."""
    _state.command_perms["return"] = {"tier": "everyone", "hidden": False}
    cog = _DummyCog()
    ctx = _ctx_with_perm("return")

    result = await cog.cmd_protected(ctx)
    assert result == "called"


# ── functools.wraps preservation ──────────────────────────────────────────────

async def test_decorator_preserves_function_name_and_signature():
    """discord.py's command introspection reads __name__ and __wrapped__
    via functools.wraps. If those are missing, parameter parsing breaks."""
    cog = _DummyCog()
    method = cog.cmd_protected
    # Bound method has __func__; either __wrapped__ is present (functools.wraps)
    # or __name__ matches the original.
    assert method.__func__.__name__ == "cmd_protected"
    # functools.wraps sets __wrapped__ to the original function.
    assert hasattr(method.__func__, "__wrapped__")
