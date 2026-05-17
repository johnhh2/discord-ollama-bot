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


# ── Top-alias dispatch must bind self ─────────────────────────────────────────
#
# Regression for production crash: !unassignrole @user Rat King → AttributeError:
# 'str' object has no attribute 'guild'. Root cause was that
# `bot.add_command(commands.Command(sub_cmd.callback, name=top))` created a
# Command with cog=None. discord.py's _parse_arguments then sets
# ctx.args = [ctx] (instead of [cog, ctx]), so dispatching the wrapper
# `async def wrapper(self, ctx, *args)` calls it as wrapper(ctx, "@u", "Rat King")
# — `self` binds to the real Context and `ctx` binds to the string "@u".
# First `ctx.guild` access then crashes. This affected *every* top-level
# alias in _SHOP_TOP_ALIASES that took any user argument (~20 commands).
#
# Why the prior tests missed it:
#   - test_each_alias_command_callback_points_at_real_subcommand only
#     compared `.callback` identity — never dispatched.
#   - test_shop_subcommand_decorator_does_not_inject_phantom_ctx_param
#     inspected the *subcommand's* .params (cog-bound), not the top-level
#     alias Command's (cog=None). Different objects, different behavior.
#   - All other shop tests call cog.shop_X.callback(cog, ctx, ...) directly,
#     manually passing the cog and bypassing _parse_arguments entirely.
#
# The fix is alias_cmd.cog = self in ShopCog.__init__. The tests below assert
# both the static binding AND that dispatch actually wires arguments correctly.

async def test_every_top_alias_command_has_cog_bound():
    """Each registered top-level alias must have its .cog set to the
    ShopCog instance so discord.py prepends it to ctx.args at dispatch.
    Without this binding, the wrapper's `self` slot eats the real Context
    and downstream `.guild` access on a string crashes the command."""
    bot = _FakeBot()
    cog = ShopCog(bot=bot)
    for top_name, _ in _SHOP_TOP_ALIASES:
        registered_cmd = bot.commands_registered[top_name]
        assert registered_cmd.cog is cog, (
            f"!{top_name} alias is not bound to the cog — "
            f"dispatch will shift args and crash on the first .guild access"
        )


# ── Dispatch path: assert wrapper receives the right positionals ──────────────
#
# These tests poke discord.py's actual dispatch machinery (_parse_arguments
# + callback invocation) and assert what the wrapper actually sees. They
# would have caught the production crash; the prior identity-only tests
# could not.

class _StubGuild:
    """Just enough Guild for the wrapper's `ctx.guild` truthiness check.
    The wrapper body short-circuits on ctx.guild being None for some
    branches; we want it to look like a real guild."""
    def __init__(self, gid: int = 999):
        self.id = gid


class _StubMessage:
    def __init__(self, content: str):
        self.content = content
        self.attachments = []


class _StubAuthor:
    def __init__(self, uid: int = 1):
        self.id = uid


class _StubCtx:
    """Minimal Context substitute carrying the fields _parse_arguments and
    the wrapper actually read: view, message, args, kwargs, guild, author,
    invoked_with, current_parameter."""
    def __init__(self, content: str, *, guild_id: int = 999):
        from discord.ext.commands.view import StringView
        self.view = StringView(content)
        # _parse_arguments calls view.skip_string(command_name) implicitly
        # via the caller — for our direct invocation, pre-advance past the
        # command word so subsequent get_quoted_word() pulls user args.
        # Simulate: "!unassignrole <@1> Rat King" → skip "!unassignrole " then
        # the view points at "<@1> Rat King".
        first_space = content.find(" ")
        if first_space >= 0:
            self.view.index = first_space + 1
            self.view.previous = first_space
        else:
            self.view.index = len(content)
            self.view.previous = len(content)
        self.message = _StubMessage(content)
        self.guild = _StubGuild(guild_id)
        self.author = _StubAuthor()
        self.args: list = []
        self.kwargs: dict = {}
        self.current_parameter = None


async def test_top_alias_dispatch_passes_cog_as_self_not_context():
    """End-to-end through _parse_arguments: after parsing, ctx.args[0] must
    be the cog instance (not the Context). If alias.cog is None, args[0]
    would be the Context and args[1] would be the first user-supplied
    string — which is exactly the production bug."""
    bot = _FakeBot()
    cog = ShopCog(bot=bot)
    alias_cmd = bot.commands_registered["unassignrole"]

    ctx = _StubCtx("!unassignrole <@1> Rat King")
    await alias_cmd._parse_arguments(ctx)

    assert ctx.args[0] is cog, (
        f"ctx.args[0] should be the ShopCog instance, got "
        f"{type(ctx.args[0]).__name__}={ctx.args[0]!r}. "
        f"This means dispatch will call wrapper(self=Context, ctx=<str>) "
        f"and crash on ctx.guild — the exact production bug."
    )
    # args = [cog, ctx, *user_args]; *args is gathered into one tuple.
    assert ctx.args[1] is ctx, "ctx.args[1] should be the Context itself"


async def test_top_alias_dispatch_wrapper_sees_correct_self_and_ctx():
    """Actually invoke the wrapper through dispatch and intercept what
    `self` and `ctx` resolve to. The wrapper's first line that touches
    `ctx.guild` would crash with 'str' has no attribute 'guild' if the
    cog binding were missing."""
    bot = _FakeBot()
    cog = ShopCog(bot=bot)
    alias_cmd = bot.commands_registered["unassignrole"]

    captured: dict = {}

    async def spy(self, ctx, *args, **kwargs):
        captured["self"] = self
        captured["ctx"] = ctx
        captured["args"] = args
        # Touch ctx.guild — the exact attribute access that crashed in prod.
        captured["guild_id"] = ctx.guild.id

    # Swap in our spy as the callback; preserves the same dispatch shape.
    alias_cmd._callback = spy
    # __qualname__ matters for is_inside_class — keep it cog-shaped.
    spy.__qualname__ = "ShopCog.shop_unassignrole"

    ctx = _StubCtx("!unassignrole <@1> Rat King")
    await alias_cmd._parse_arguments(ctx)
    await alias_cmd.callback(*ctx.args, **ctx.kwargs)

    assert captured["self"] is cog, (
        f"wrapper's `self` should be the ShopCog instance, got "
        f"{type(captured['self']).__name__}"
    )
    assert captured["ctx"] is ctx, "wrapper's `ctx` should be the Context"
    assert captured["args"] == ("<@1>", "Rat", "King"), (
        f"user args should pass through unchanged, got {captured['args']!r}"
    )
    assert captured["guild_id"] == 999, (
        "ctx.guild.id access succeeded — the crash is fixed"
    )


async def test_top_alias_dispatch_does_not_crash_on_str_guild_access():
    """Tightest possible reproduction: bare !unassignrole @user <name>
    through the dispatch pipeline. If anyone ever drops alias_cmd.cog=self,
    this test fails with AttributeError: 'str' object has no attribute 'guild',
    matching the original production stack trace verbatim."""
    bot = _FakeBot()
    ShopCog(bot=bot)
    alias_cmd = bot.commands_registered["unassignrole"]

    ctx = _StubCtx("!unassignrole <@393568333644955648> Rat King")
    await alias_cmd._parse_arguments(ctx)

    # If the bug regresses, args[0] is the Context, args[1] is the string
    # "<@...>", and the wrapper's `ctx.guild` access raises. We don't need
    # to fully execute the wrapper — proving args[0] is the cog is enough,
    # because that is exactly what dispatch will pass to callback(*ctx.args).
    first = ctx.args[0]
    assert hasattr(first, "shop_unassignrole"), (
        f"First positional must be the cog (which has shop_unassignrole on it). "
        f"Got {type(first).__name__}. Bug: alias_cmd.cog is not set, dispatch "
        f"will call wrapper(self=Context, ctx='<@...>') and crash on .guild."
    )
