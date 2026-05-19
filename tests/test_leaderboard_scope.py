"""!lb scope (server vs global) + the !settings leaderboard default setter.

The currency economy is global (one shared pool keyed by user id). The
`server` scope filters that pool down to users the guild's member cache can
resolve. The per-guild `leaderboard_default_scope` setting picks the default
when `!lb` is called with no arg; an explicit `!lb server` / `!lb global`
always overrides it.
"""
import discord
import pytest

import src.state as _state
from src.cogs.economy_cog import EconomyCog
from src.cogs.settings_cog import SettingsCog

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


async def _raise_not_found(_uid):
    """FakeGuild has no fetch_member; helpers.fetch_member only swallows
    NotFound/HTTPException, so non-members must raise one of those to fall
    through to bot.fetch_user instead of an AttributeError."""
    raise discord.NotFound(_FakeResponse(), "no such member")


class _FakeResponse:
    status = 404
    reason = "Not Found"


pytestmark = pytest.mark.asyncio


class _StubBotUser:
    def __init__(self, uid: int = 999_999_999):
        self.id = uid


class _FetchedUser:
    def __init__(self, uid):
        self.id = uid
        self.display_name = f"u{uid}"


class _StubBot:
    """fetch_user backs name resolution for users the guild's member cache
    can't resolve (the global-scope case)."""
    def __init__(self):
        self.user = _StubBotUser()

    async def fetch_user(self, uid):
        return _FetchedUser(uid)


def _seed_economy(balances: dict[int, int]):
    _state.economy["users"].clear()
    for uid, bal in balances.items():
        _state.economy["users"][str(uid)] = {"balance": bal, "savings": []}


def _guild_with_members(gid: int, member_ids: list[int]) -> FakeGuild:
    guild = FakeGuild(gid=gid)
    guild.members = [FakeMember(uid=u, display_name=f"u{u}") for u in member_ids]
    guild.fetch_member = _raise_not_found
    return guild


async def test_global_scope_includes_non_members(db, monkeypatch):
    """Default (global) scope ranks the whole economy, even users who aren't
    cached members of this guild."""
    async def _no_lottery(_gid):
        return {"players": {}}
    monkeypatch.setattr("src.cogs.economy_cog.load_lottery", _no_lottery)

    _seed_economy({1: 500, 2: 300, 3: 100})
    # Guild only knows about user 2.
    guild = _guild_with_members(1, [2])
    _state.guild_settings.pop("1", None)  # no default set → global

    cog = EconomyCog(bot=_StubBot())
    ctx = FakeCtx(author=FakeMember(uid=2), guild=guild)
    ctx.bot = _StubBot()

    await cog.cmd_leaderboard.callback(cog, ctx)

    embed = ctx.sent_embeds[-1]
    assert embed.title == "🪙 Leaderboard"
    # All three users appear; ranked by balance.
    assert "u1" in embed.description
    assert "u2" in embed.description
    # Non-member name still resolves through the guild member fake or fetch;
    # the key assertion is the count of ranked lines.
    assert embed.description.count("🪙") >= 3


async def test_server_scope_filters_to_members(db, monkeypatch):
    """`!lb server` drops users the guild's member cache can't resolve."""
    async def _no_lottery(_gid):
        return {"players": {}}
    monkeypatch.setattr("src.cogs.economy_cog.load_lottery", _no_lottery)

    _seed_economy({1: 500, 2: 300, 3: 100})
    # Guild knows users 1 and 3 only — user 2 (the runner-up globally) is filtered out.
    guild = _guild_with_members(1, [1, 3])

    cog = EconomyCog(bot=_StubBot())
    ctx = FakeCtx(author=FakeMember(uid=1), guild=guild)
    ctx.bot = _StubBot()

    await cog.cmd_leaderboard.callback(cog, ctx, "server")

    embed = ctx.sent_embeds[-1]
    assert embed.title == "🪙 Server Leaderboard"
    assert "u1" in embed.description
    assert "u3" in embed.description
    assert "u2" not in embed.description


async def test_default_scope_setting_drives_no_arg_lb(db, monkeypatch):
    """With the guild default set to `server`, a bare `!lb` filters to members."""
    async def _no_lottery(_gid):
        return {"players": {}}
    monkeypatch.setattr("src.cogs.economy_cog.load_lottery", _no_lottery)

    _seed_economy({1: 500, 2: 300})
    guild = _guild_with_members(1, [1])  # user 2 not a member
    _state.guild_settings["1"] = {"leaderboard_default_scope": "server"}

    cog = EconomyCog(bot=_StubBot())
    ctx = FakeCtx(author=FakeMember(uid=1), guild=guild)
    ctx.bot = _StubBot()

    await cog.cmd_leaderboard.callback(cog, ctx)  # no scope arg

    embed = ctx.sent_embeds[-1]
    assert embed.title == "🪙 Server Leaderboard"
    assert "u1" in embed.description
    assert "u2" not in embed.description


async def test_explicit_arg_overrides_default(db, monkeypatch):
    """`!lb global` overrides a `server` guild default."""
    async def _no_lottery(_gid):
        return {"players": {}}
    monkeypatch.setattr("src.cogs.economy_cog.load_lottery", _no_lottery)

    _seed_economy({1: 500, 2: 300})
    guild = _guild_with_members(1, [1])
    _state.guild_settings["1"] = {"leaderboard_default_scope": "server"}

    cog = EconomyCog(bot=_StubBot())
    ctx = FakeCtx(author=FakeMember(uid=1), guild=guild)
    ctx.bot = _StubBot()

    await cog.cmd_leaderboard.callback(cog, ctx, "global")

    embed = ctx.sent_embeds[-1]
    assert embed.title == "🪙 Leaderboard"
    assert "u2" in embed.description  # non-member shows in global scope


async def test_settings_leaderboard_persists(db):
    cog = SettingsCog(bot=None)
    author = FakeMember(uid=1, administrator=True)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42))
    ctx.command.qualified_name = "settings leaderboard"

    await cog.settings_leaderboard.callback(cog, ctx, "server")

    assert _state.guild_settings["42"]["leaderboard_default_scope"] == "server"


async def test_settings_leaderboard_invalid_no_mutation(db):
    cog = SettingsCog(bot=None)
    author = FakeMember(uid=1, administrator=True)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=43))
    ctx.command.qualified_name = "settings leaderboard"

    await cog.settings_leaderboard.callback(cog, ctx, "banana")

    assert "leaderboard_default_scope" not in _state.guild_settings.get("43", {})
