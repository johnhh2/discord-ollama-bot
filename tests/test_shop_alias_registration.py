"""Round-1 commit 0f0fadd refactored 30 hand-written cmd_<name> wrappers
into a declarative _SHOP_TOP_ALIASES table iterated in ShopCog.__init__.
The registration creates `commands.Command` objects on the bot for each
top-level alias and binds them to the underlying shop_X subcommand
callback. cog_unload removes them.

Existing tests in test_shop_extended.py cover the table's *consistency*
(every entry resolves to a real method; roleup/roledown share shop_roleup).
This file covers the *registration mechanism*: when ShopCog is loaded,
the bot ends up with a top-level `!nickname` (etc.) command pointing at
the right callback; when unloaded, it disappears.
"""

import pytest

from discord.ext import commands as dpy_commands

from src.cogs.shop_cog import ShopCog, _SHOP_TOP_ALIASES


pytestmark = pytest.mark.asyncio


class _FakeBot:
    """Minimal bot stub that records add_command / remove_command calls
    in a dict keyed by command name."""
    def __init__(self):
        self.commands_registered: dict[str, dpy_commands.Command] = {}

    def add_command(self, cmd: dpy_commands.Command):
        if cmd.name in self.commands_registered:
            raise dpy_commands.CommandRegistrationError(cmd.name)
        self.commands_registered[cmd.name] = cmd

    def remove_command(self, name: str):
        return self.commands_registered.pop(name, None)


# ── Registration ──────────────────────────────────────────────────────────────

async def test_shop_cog_init_registers_every_top_alias_on_the_bot():
    """Every (top_name, sub_attr) entry in _SHOP_TOP_ALIASES becomes a
    top-level commands.Command on the bot."""
    bot = _FakeBot()
    ShopCog(bot=bot)
    expected_names = {top for top, _ in _SHOP_TOP_ALIASES}
    assert set(bot.commands_registered) == expected_names


async def test_each_alias_command_callback_points_at_real_subcommand():
    """!nickname's callback should be ShopCog.shop_nickname's callback,
    not some other random function. Catches a typo in the table."""
    bot = _FakeBot()
    cog = ShopCog(bot=bot)
    for top_name, sub_attr in _SHOP_TOP_ALIASES:
        registered_cmd = bot.commands_registered[top_name]
        sub_cmd = getattr(cog, sub_attr)
        assert registered_cmd.callback is sub_cmd.callback, (
            f"!{top_name} → ShopCog.{sub_attr} mismatch"
        )


async def test_roleup_and_roledown_share_the_same_callback():
    """Both top-level aliases route to shop_roleup, which uses
    ctx.invoked_with internally to pick direction."""
    bot = _FakeBot()
    ShopCog(bot=bot)
    roleup = bot.commands_registered["roleup"]
    roledown = bot.commands_registered["roledown"]
    assert roleup.callback is roledown.callback


async def test_init_with_bot_none_skips_registration():
    """Tests instantiate ShopCog(bot=None) to call subcommand callbacks
    directly. The init guard must not crash on None."""
    cog = ShopCog(bot=None)
    # Doesn't raise; the loop body is skipped.
    assert cog.bot is None


# ── Unload ────────────────────────────────────────────────────────────────────

async def test_cog_unload_removes_every_top_alias_from_the_bot():
    """When the cog reloads, the registered top-level commands must be
    removed; otherwise the next load will hit CommandRegistrationError."""
    bot = _FakeBot()
    cog = ShopCog(bot=bot)
    assert bot.commands_registered, "sanity: registration happened"

    cog.cog_unload()

    assert bot.commands_registered == {}, (
        "cog_unload should remove all top-level aliases"
    )


async def test_cog_unload_with_bot_none_is_a_noop():
    """cog_unload must guard against bot=None for the same test-time path
    that __init__ guards."""
    cog = ShopCog(bot=None)
    # Doesn't raise.
    cog.cog_unload()


async def test_cog_can_be_loaded_unloaded_loaded_again():
    """Reload cycle: load → unload → load. The second load must succeed
    without CommandRegistrationError, proving unload cleaned up properly."""
    bot = _FakeBot()
    cog = ShopCog(bot=bot)
    cog.cog_unload()
    # Second load — would raise if any aliases lingered.
    ShopCog(bot=bot)
    expected_names = {top for top, _ in _SHOP_TOP_ALIASES}
    assert set(bot.commands_registered) == expected_names


# ── Lockstep with !shop subcommand surface ────────────────────────────────────

async def test_every_shop_subcommand_in_the_table_actually_exists():
    """If a refactor renames or deletes a shop_X subcommand, the table
    will silently keep an entry that AttributeError's at registration.
    This guards against the typo."""
    cog = ShopCog(bot=None)
    for top_name, sub_attr in _SHOP_TOP_ALIASES:
        attr = getattr(cog, sub_attr, None)
        assert attr is not None, f"_SHOP_TOP_ALIASES references missing attr {sub_attr!r}"
        # Must be a discord.py Command (has a .callback attribute).
        assert hasattr(attr, "callback"), f"{sub_attr} is not a Command"


# ── _shop_subcommand decorator must preserve method signature ─────────────────
#
# Regression for the bug where _shop_subcommand copied __name__ and __wrapped__
# but not __qualname__. discord.py's get_signature_parameters calls
# is_inside_class(callback), which checks __qualname__ for a class component.
# Without it, discord.py only skips one leading param instead of two (self+ctx),
# so `ctx` is treated as a user-supplied arg — and every wrapped subcommand
# 400s with MissingRequiredArgument or BadArgument("Converting to Context failed").
# Tests that call `cog.shop_X.callback(cog, ctx)` directly bypass this entire
# code path and won't catch it; the surface we need to assert against is
# discord.py's view of the Command's .params dict.

async def test_shop_subcommand_decorator_does_not_inject_phantom_ctx_param():
    """No-arg !shop subcommands must have zero user params. If
    _shop_subcommand breaks signature inspection, `ctx` shows up here and
    the command dies at dispatch with MissingRequiredArgument."""
    cog = ShopCog(bot=None)
    assert list(cog.shop_insurance.params) == []
    assert list(cog.shop_removenickname.params) == []


async def test_shop_subcommand_decorator_preserves_varargs_signature():
    """!shop subcommands declared `*args` must expose exactly one user
    param (`args`), not `[ctx, args]`. The phantom `ctx` slot would
    consume the real Context and trip BadArgument on first positional."""
    cog = ShopCog(bot=None)
    assert list(cog.shop_roleup.params) == ["args"]
    assert list(cog.shop_nickname.params) == ["args"]
    assert list(cog.shop_mock.params) == ["args"]
