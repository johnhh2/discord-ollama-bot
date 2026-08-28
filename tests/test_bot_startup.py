"""Bot-startup smoke test against a real discord.py commands.Bot.

This is the closest we can get to "does the bot boot?" without a Discord
gateway connection. We:
  1. Build a real `commands.Bot` via `create_bot()`.
  2. Call `_load_extensions(bot)` so every cog's `setup()` runs.
  3. Assert no exception, and assert every command we expect is actually
     registered on the bot.

What this catches that direct cog-instantiation tests miss:
- CommandRegistrationError: two cogs (or two declarative aliases inside
  one cog) try to register the same command name. ShopCog's
  _SHOP_TOP_ALIASES has 25 names; if any collide with a top-level
  command in another cog, the bot would crash during setup.
- Command import errors: a cog file that fails to import (NameError,
  missing module, etc.) would raise here. Round-2's leveling/persistence/
  shop split had ~18 importer rewrites; this is the gate that catches
  any one we missed.
- decorator-order regressions on @requires_perm: discord.py's command
  introspection reads `__wrapped__` to resolve converters; if the
  decorator order broke, register-time validation would fail.
"""
import pytest

from src.core import create_bot, EXTENSIONS, _load_extensions


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def loaded_bot():
    """Build a real commands.Bot and load every cog. Yields the bot.

    NOTE: discord.py's Bot() constructor does not open a network
    connection; only .run() / .start() does. So we can construct
    and load extensions safely in a test.

    discord.py's bot.load_extension('src.X') REBINDS sys.modules['src.X']
    to a freshly-imported module object. Anything that imported src.X at
    test-collection time (e.g. `import src.events as _events` at the top
    of test_schedulers.py) is left holding the OLD module reference, while
    new `from src.X import Y` statements after our fixture get the NEW
    module's symbols. Module-private state (like
    src.events._SOUNDBOARD_TIMESTAMPS) ends up bifurcated.

    Cleanup snapshots sys.modules['src.*'] BEFORE loading and restores it
    after. Combined with cancelling the leveling voice-tick loop (which
    would otherwise raise about a missing client session), this keeps
    the test isolated from the rest of the suite.
    """
    import sys
    snapshot = {k: sys.modules[k] for k in list(sys.modules) if k.startswith("src.")}

    bot = create_bot()
    await _load_extensions(bot)
    # Stop the leveling voice-tick loop before it tries to wait_until_ready.
    leveling = bot.get_cog("LevelingCog")
    if leveling is not None:
        leveling._voice_task.cancel()
    yield bot
    # Restore sys.modules to its pre-load state so other tests' module-level
    # imports remain pointed at the right module objects.
    for k, original in snapshot.items():
        sys.modules[k] = original


# ── Smoke ─────────────────────────────────────────────────────────────────────

async def test_bot_loads_every_extension_without_error(loaded_bot):
    """If any setup() function raised, the fixture would have errored out
    before reaching this assertion. Reaching here is the test."""
    # The fixture already loaded EXTENSIONS; just confirm the count.
    assert loaded_bot.cogs, "no cogs registered after _load_extensions"


async def test_bot_loads_every_named_extension(loaded_bot):
    """Every entry in EXTENSIONS must produce at least one registered cog
    OR at least one registered command. If a cog file has no setup(),
    discord.py raises during load — caught by the fixture itself.

    This test is a sanity for EXTENSIONS additions: a misnamed module path
    would also raise during load."""
    # All EXTENSIONS got loaded. We can also check `bot.extensions` is the
    # set of dotted names we passed in.
    assert set(loaded_bot.extensions.keys()) == set(EXTENSIONS), (
        f"extensions list out of sync: loaded={set(loaded_bot.extensions)}, "
        f"expected={set(EXTENSIONS)}"
    )


# ── Command registration: catch CommandRegistrationError surface ──────────────

async def test_no_duplicate_top_level_command_names(loaded_bot):
    """If two cogs (or one cog's declarative alias loop) try to register the
    same top-level command name, discord.py raises CommandRegistrationError
    during the second add_command. The fixture would have failed.

    This test makes the implicit guarantee explicit: count distinct command
    names, ensure no dupes snuck through."""
    names = [cmd.name for cmd in loaded_bot.commands]
    duplicates = {n for n in names if names.count(n) > 1}
    assert duplicates == set(), f"duplicate top-level commands: {duplicates}"


async def test_known_top_level_commands_are_registered(loaded_bot):
    """Spot-check that the surface every test relies on actually exists.
    A typo in @commands.command(name=...) would silently rename the command
    and break user invocations without breaking tests."""
    expected_subset = {
        # AI / ai_cog
        "ask", "story", "continue", "tldr", "roleplay", "rpg", "stop", "reverse", "invite",
        # Economy / economy_cog
        "daily", "balance", "leaderboard", "steal", "mug", "jailbreak",
        "admingive", "admingivexp", "event",
        # Shop top-level aliases (registered via the _SHOP_TOP_ALIASES table).
        # Canonical names after the role*/channel* rename; legacy verb-first
        # names (createrole, …) survive as aliases.
        "nickname", "rolecreate", "rolecolor", "ragebait", "mock",
        "insurance", "tax", "curse", "mute", "roleup", "roledown", "buyxp",
        # Settings / settings_cog
        "settings", "model", "roleplaymodel", "codingmodel", "vramtext",
        "setprompt", "clearprompt",
        # Admin / admin_cog
        "godmode", "say", "restart", "setperm",
        # Moderation / moderation_cog
        "audit", "clear",
        # Utility / utility_cog
        "ai", "saved", "puzzle", "gambler-role",
        # Games
        "hangman", "ttt", "c4", "chess", "race", "blackjack",
        # Gambling — `scratch` is an alias of `scratchoff`; canonical name here.
        "slots", "flip", "scratchoff",
        # Lottery / leveling
        "lottery", "lvl", "levels",
    }
    actual = {cmd.name for cmd in loaded_bot.commands}
    missing = expected_subset - actual
    assert missing == set(), f"expected commands missing: {missing}"


async def test_shop_top_aliases_all_registered_at_top_level(loaded_bot):
    """The _SHOP_TOP_ALIASES declarative table runs at ShopCog __init__
    time. If add_command failed silently for any of them, they wouldn't
    appear here."""
    from src.cogs.shop_cog import _SHOP_TOP_ALIASES
    actual = {cmd.name for cmd in loaded_bot.commands}
    for top_name, _sub_attr, _legacy in _SHOP_TOP_ALIASES:
        assert top_name in actual, (
            f"!{top_name} (from _SHOP_TOP_ALIASES) not registered on the bot"
        )


# ── @requires_perm + discord.py introspection regression ────────────────────────

async def test_requires_perm_does_not_break_discord_py_introspection(loaded_bot):
    """discord.py reads function signatures at command-registration time
    to resolve converters (e.g. MemberConverter). If @requires_perm
    weren't using functools.wraps correctly, every decorated command
    would have an empty signature, and Member-typed args would silently
    fail to resolve.

    Spot-check a command that takes a MemberConverter argument: cmd_give
    in EconomyCog (signature: target: MemberConverter = None, amount: str = None)."""
    cmd_give = loaded_bot.get_command("admingive")
    assert cmd_give is not None
    # discord.py exposes the resolved Parameter list via .clean_params.
    params = list(cmd_give.clean_params.keys())
    assert "target" in params and "amount" in params, (
        f"@requires_perm appears to have broken discord.py's signature "
        f"introspection for !admingive. Actual params: {params}"
    )


async def test_command_perms_subset_match_registered_commands(loaded_bot):
    """Every command_perms.json key should map to a real command — a key
    pointing at a renamed-or-deleted command silently does nothing,
    which is hard to debug."""
    import json
    from pathlib import Path
    perms = json.loads(Path("src/command_perms.json").read_text(encoding="utf-8"))

    actual_commands = {cmd.name for cmd in loaded_bot.commands}
    actual_qualified = set()
    for cmd in loaded_bot.commands:
        if hasattr(cmd, "commands"):  # Group: walk subcommands
            for sub in cmd.commands:
                actual_qualified.add(sub.qualified_name)

    valid = actual_commands | actual_qualified
    stale = set(perms.keys()) - valid
    assert stale == set(), (
        f"command_perms.json has entries for commands that don't exist: {stale}. "
        f"They silently do nothing — either rename in the JSON or remove."
    )


async def test_bot_blocks_everyone_and_role_mentions_by_default(loaded_bot):
    """Hardening: shop effects (mock/curse/ragebait/tax) echo user input back
    via channel.send. Without a global allowed_mentions default, a user could
    type @everyone and the bot would ping. Verify the bot is constructed with
    everyone+role pings disabled."""
    am = loaded_bot.allowed_mentions
    assert am is not None, "bot must define an allowed_mentions default"
    assert am.everyone is False
    assert am.roles is False
