"""Regression tests for the non-blocklist findings of the 2026-08 audit.

Covers, in order:

- `global_admin` tier — commands whose effect has no guild dimension
  (`godmode`, `rig`, `globalban`, `admingive`, `setperm`) must ignore the
  guild-scoped `!setperm` override, or the scoping is fictional.
- Fail-closed tier dispatch — an unrecognized tier used to be allowed.
- `shop_payout` — godmode plays free, so it must not be *credited* either;
  crediting an uncharged bet made godmode an unbounded money printer into the
  shared (global) economy.
- NSFW request building — credentials must survive user-supplied tags without
  the tag being able to inject query parameters, and must never reach a user.

The blocklist bypasses live in test_blocklist_enforcement.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

import src.state as _state
from src.permissions import (
    VALID_TIERS, check_command_permission, is_admin, is_global_admin,
    is_bot_admin_id,
)
from src.helpers import shop_payout, shop_charge

pytestmark = pytest.mark.asyncio

GUILD = 900
ENV_ADMIN = 1
GRANTED = 2
NOBODY = 3


def _ctx(uid: int, guild_id: "int | None", command: str):
    """Minimal Context stand-in for the permission gate."""
    return SimpleNamespace(
        author=SimpleNamespace(id=uid, guild_permissions=SimpleNamespace(administrator=False)),
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        command=SimpleNamespace(qualified_name=command),
        send=AsyncMock(),
    )


# ── global_admin tier ─────────────────────────────────────────────────────────

async def test_setperm_override_grants_bot_admin_but_not_global_admin():
    _state.bot_admins.add(ENV_ADMIN)
    _state.user_perm_overrides[(GUILD, GRANTED)] = "bot_admin"

    granted = _ctx(GRANTED, GUILD, "godmode")
    assert is_admin(granted) is True          # still a bot_admin in this guild
    assert is_global_admin(granted) is False  # but not for global-state commands

    env = _ctx(ENV_ADMIN, GUILD, "godmode")
    assert is_global_admin(env) is True


@pytest.mark.parametrize(
    "command", ["godmode", "admingive", "rig", "globalban", "globalunban", "setperm"],
)
async def test_global_state_commands_reject_a_guild_scoped_grant(command):
    """The audit's F-04: a bot_admin override in one guild could mint coins,
    rig another player's jackpot and ban bot-wide, because that state is keyed
    by user id alone."""
    _state.command_perms[command] = {"tier": "global_admin", "hidden": True}
    _state.user_perm_overrides[(GUILD, GRANTED)] = "bot_admin"
    _state.bot_admins.add(ENV_ADMIN)

    assert await check_command_permission(_ctx(GRANTED, GUILD, command)) is False
    assert await check_command_permission(_ctx(ENV_ADMIN, GUILD, command)) is True


async def test_guild_scoped_commands_still_honour_the_override():
    """The override must keep working for commands whose blast radius really
    is the granting guild."""
    _state.command_perms["adminjailbreak"] = {"tier": "bot_admin", "hidden": True}
    _state.user_perm_overrides[(GUILD, GRANTED)] = "bot_admin"

    assert await check_command_permission(_ctx(GRANTED, GUILD, "adminjailbreak")) is True
    # …and only inside the granting guild.
    assert await check_command_permission(_ctx(GRANTED, 12345, "adminjailbreak")) is False


async def test_is_bot_admin_id_matches_is_admin_for_raw_events():
    """Raw reaction handlers have no Context; the id-based form must agree."""
    _state.bot_admins.add(ENV_ADMIN)
    _state.user_perm_overrides[(GUILD, GRANTED)] = "bot_admin"

    assert is_bot_admin_id(ENV_ADMIN, GUILD) is True
    assert is_bot_admin_id(ENV_ADMIN, None) is True
    assert is_bot_admin_id(GRANTED, GUILD) is True
    assert is_bot_admin_id(GRANTED, 12345) is False
    assert is_bot_admin_id(NOBODY, GUILD) is False


# ── fail-closed tier dispatch ─────────────────────────────────────────────────

async def test_unknown_tier_denies_instead_of_allowing():
    """The audit's F-08: `else: allowed = True` turned a typo in
    command_perms.json into a silently public command."""
    _state.command_perms["typo_cmd"] = {"tier": "bot-admin", "hidden": False}
    assert await check_command_permission(_ctx(NOBODY, GUILD, "typo_cmd")) is False


async def test_unlisted_commands_still_default_to_everyone():
    """Fail-closed applies to a *bad* tier, not to an absent entry."""
    _state.command_perms.clear()
    assert await check_command_permission(_ctx(NOBODY, GUILD, "daily")) is True


async def test_shipped_command_perms_only_uses_known_tiers():
    """Mirrors the boot-time validation in init_db_state, so a bad tier fails
    in CI rather than at deploy."""
    import json
    from src.config import COMMAND_PERMS_FILE

    with open(COMMAND_PERMS_FILE, encoding="utf-8") as f:
        perms = json.load(f)
    bad = {c: d.get("tier") for c, d in perms.items() if d.get("tier") not in VALID_TIERS}
    assert bad == {}, f"unknown tier(s) in {COMMAND_PERMS_FILE}: {bad}"


# ── shop_payout / godmode symmetry ────────────────────────────────────────────

async def test_godmode_is_charged_nothing_and_credited_nothing():
    """The audit's F-05. shop_charge waives the bet; crediting the win anyway
    let `!flip 1m 100000` mint billions into the shared economy."""
    from src.economy import get_balance, add_balance

    uid = 616
    _state.godmode_users.add(uid)
    await add_balance(uid, 500)
    start = await get_balance(uid)

    assert await shop_charge(_ctx(uid, GUILD, "flip"), uid, 1_000_000) is True
    assert await get_balance(uid) == start, "godmode must not be charged"

    await shop_payout(uid, 2_000_000)
    assert await get_balance(uid) == start, "godmode must not be credited either"


async def test_normal_players_are_charged_and_paid_as_before():
    from src.economy import get_balance, add_balance

    uid = 617
    await add_balance(uid, 1_000)
    assert await shop_charge(_ctx(uid, GUILD, "flip"), uid, 400) is True
    assert await get_balance(uid) == 600

    assert await shop_payout(uid, 800) is not None
    assert await get_balance(uid) == 1_400


async def test_shop_payout_ignores_non_positive_amounts():
    from src.economy import get_balance, add_balance

    uid = 618
    await add_balance(uid, 100)
    await shop_payout(uid, 0)
    await shop_payout(uid, -50)
    assert await get_balance(uid) == 100


# ── NSFW request building ─────────────────────────────────────────────────────

def _build_nsfw_url(search_tags: str, key="SECRET_KEY", user="4242") -> str:
    """Mirror of _fetch_pid's URL construction in src/cogs/fun_cog.py."""
    from urllib.parse import urlencode
    params = {
        "page": "dapi", "s": "post", "q": "index",
        "json": "1", "limit": "100", "pid": "0",
        "tags": search_tags.replace("+", " "),
    }
    params["api_key"] = key
    params["user_id"] = user
    return f"https://api.example.test/index.php?{urlencode(params)}"


async def test_nsfw_tags_cannot_inject_query_parameters():
    """The audit's F-03 (second half): tags were spliced into the URL raw, so
    `!nsfw a&limit=1` added parameters to the upstream call."""
    q = parse_qs(urlparse(_build_nsfw_url("cat&limit=1&json=0")).query)
    assert q["limit"] == ["100"]
    assert q["json"] == ["1"]
    assert q["tags"] == ["cat&limit=1&json=0"]


async def test_nsfw_credentials_survive_a_hostile_tag():
    """A `#` used to truncate the URL at the fragment, dropping the key."""
    for hostile in ("cat#", "cat%zz", "cat&api_key=stolen", "cat ", "☃"):
        q = parse_qs(urlparse(_build_nsfw_url(hostile)).query)
        assert q["api_key"] == ["SECRET_KEY"], hostile
        assert q["user_id"] == ["4242"], hostile


async def test_nsfw_plus_still_separates_tags():
    """`+` is the wire form of a space, not a literal — encoding it as %2B
    would make "cat+girl" one tag named "cat+girl"."""
    q = parse_qs(urlparse(_build_nsfw_url("cat+girl+-dog")).query)
    assert q["tags"] == ["cat girl -dog"]


async def test_nsfw_failure_never_puts_the_api_key_in_front_of_a_user(monkeypatch):
    """The audit's F-03 (first half). Five aiohttp exception classes embed the
    full request URL — which carries api_key/user_id — in str(e), and the
    handler interpolated it straight into a channel embed. A Cloudflare
    challenge or a moved endpoint was enough to publish the credentials."""
    import aiohttp
    from yarl import URL
    import src.cogs.fun_cog as fun_cog
    from tests.fakes.discord import FakeCtx, FakeGuild

    key = "SUPER_SECRET_KEY"
    leaky_url = URL(f"https://api.example.test/index.php?tags=cat&api_key={key}&user_id=42")
    # Sanity-check the premise before asserting the fix.
    assert key in str(aiohttp.InvalidURL(leaky_url))

    monkeypatch.setattr(fun_cog, "NSFW_API_URL", "https://api.example.test/index.php")

    async def _boom(*a, **kw):
        raise aiohttp.InvalidURL(leaky_url)
    monkeypatch.setattr(fun_cog, "_nsfw_fetch", _boom)

    guild = FakeGuild(GUILD)
    _state.guild_settings[str(GUILD)] = {"nsfw_enabled": True}
    ctx = FakeCtx(guild=guild, command_name="nsfw")
    ctx.invoked_with = "nsfw"

    await fun_cog.FunCog(None).cmd_nsfw.callback(fun_cog.FunCog(None), ctx, tags="cat")

    surfaced = " ".join(
        (e.title or "") + " " + (e.description or "") for e in ctx.sent_embeds
    ) + " ".join(ctx.sent_messages)
    assert key not in surfaced, f"API key leaked to the channel: {surfaced!r}"
    assert "api_key" not in surfaced
    assert surfaced.strip(), "user should still get an error message"
