"""Command-flow tests for !featurerequest.

Verifies the everyone-tier submission path: per-guild
`feature_request_channel` lookup, embed posting, persistence, and ✅/❌
reaction seeding. Reject + accept reaction handling is covered separately
in tests/test_issue_reactions.py.
"""
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.utility_cog import UtilityCog
from src.guild_config import get_guild_cfg

from tests.fakes.discord import (
    FakeCtx, FakeMember, FakeGuild, FakeTextChannel, FakeMessage,
)


pytestmark = pytest.mark.asyncio


class _StubBot:
    def __init__(self, channel=None, user_id: int = 999):
        self._channel = channel
        self.user = type("U", (), {"id": user_id})()

    def get_channel(self, cid: int):
        return self._channel

    async def fetch_channel(self, cid: int):
        return self._channel


def _user_ctx(*, uid: int = 50, guild_id: int = 42) -> FakeCtx:
    """`!featurerequest` is everyone-tier, so the caller doesn't need to be
    a bot admin — but command_perms still needs an entry so the decorator
    can look up a tier."""
    author = FakeMember(uid=uid)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=guild_id))
    ctx.command.qualified_name = "featurerequest"
    _state.command_perms["featurerequest"] = {"tier": "everyone", "hidden": False}
    return ctx


async def test_featurerequest_no_channel_configured_warns(db):
    """Server hasn't run `!settings-channel feature-request #ch` → the
    command tells the user and writes no row."""
    cog = UtilityCog(bot=_StubBot())
    ctx = _user_ctx(guild_id=42)
    # Make sure no leftover config from a previous test.
    cfg = get_guild_cfg(42)
    cfg.pop("feature_request_channel", None)

    await cog.cmd_featurerequest.callback(cog, ctx, description="add X")

    # No feature_request row inserted (the table starts empty in the db fixture).
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM feature_requests")
            count = (await cur.fetchone())[0]
    assert count == 0


async def test_featurerequest_happy_path_persists_and_reacts(db):
    """Configured channel → embed posted, row persisted with kind/status
    open, and ✅/❌ reactions seeded in that order."""
    posted = FakeMessage(message_id=6000)
    posted.channel = FakeTextChannel(ch_id=8888)
    log_chan = FakeTextChannel(ch_id=8888)
    log_chan.send = AsyncMock(return_value=posted)

    cog = UtilityCog(bot=_StubBot(channel=log_chan))
    ctx = _user_ctx(guild_id=42, uid=50)
    cfg = get_guild_cfg(42)
    cfg["feature_request_channel"] = "8888"

    await cog.cmd_featurerequest.callback(cog, ctx, description="please add X")

    row = await _persistence.get_feature_request_by_message(6000)
    assert row is not None
    assert row["status"] == "open"
    assert row["reporter_id"] == 50
    assert row["guild_id"] == 42
    assert row["description"] == "please add X"
    assert row["feature_issue_id"] is None
    reactions = [c.args[0] for c in posted.add_reaction.await_args_list]
    assert reactions == ["✅", "❌"]


async def test_featurerequest_empty_description_shows_usage(db):
    """Whitespace-only description → usage embed, no DB write."""
    cog = UtilityCog(bot=_StubBot())
    ctx = _user_ctx(guild_id=42)
    cfg = get_guild_cfg(42)
    cfg["feature_request_channel"] = "8888"

    await cog.cmd_featurerequest.callback(cog, ctx, description="   ")

    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM feature_requests")
            count = (await cur.fetchone())[0]
    assert count == 0


async def test_featurerequest_dm_invocation_rejects(db):
    """!featurerequest in a DM (no guild) → friendly error, no row."""
    cog = UtilityCog(bot=_StubBot())
    author = FakeMember(uid=50)
    ctx = FakeCtx(author=author, guild=None)
    ctx.command.qualified_name = "featurerequest"
    _state.command_perms["featurerequest"] = {"tier": "everyone", "hidden": False}

    await cog.cmd_featurerequest.callback(cog, ctx, description="hi")

    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM feature_requests")
            count = (await cur.fetchone())[0]
    assert count == 0
