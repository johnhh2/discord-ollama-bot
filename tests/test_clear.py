"""!clear: a purge() that Discord rejects with 50034 (a message at the 14-day
bulk-delete boundary) falls back to a split purge instead of erroring."""
from types import SimpleNamespace

import discord
import pytest

import src.state as _state
from src.cogs import moderation_cog
from src.cogs.moderation_cog import ModerationCog

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeTextChannel


pytestmark = pytest.mark.asyncio


def _http_error(status: int, code: int, cls=discord.HTTPException):
    resp = SimpleNamespace(status=status, reason="err")
    return cls(resp, {"code": code, "message": "err"})


class _PurgeChannel(FakeTextChannel):
    """First purge() raises `first_exc`; later calls delete what's asked."""

    def __init__(self, first_exc, young: int):
        super().__init__(ch_id=7000)
        self.first_exc = first_exc
        self.young = young  # messages newer than the safe-bulk cutoff
        self.calls: list[dict] = []

    async def purge(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise self.first_exc
        if "after" in kwargs:
            return [object()] * min(self.young, kwargs["limit"])
        return [object()] * kwargs["limit"]


def _ctx(channel) -> FakeCtx:
    author = FakeMember(uid=1)
    _state.bot_admins.add(author.id)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=42), channel=channel)
    ctx.command.qualified_name = "clear"
    return ctx


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_secs):
        return None
    monkeypatch.setattr(moderation_cog.asyncio, "sleep", _instant)


async def test_clear_falls_back_when_bulk_delete_hits_14_day_limit(db):
    channel = _PurgeChannel(_http_error(400, 50034), young=20)
    ctx = _ctx(channel)
    cog = ModerationCog(bot=None)

    await cog.cmd_clearall.callback(cog, ctx, "50")

    _, bulk, single = channel.calls
    assert bulk["limit"] == 51 and bulk["oldest_first"] is False and "after" in bulk
    assert single == {"limit": 31, "bulk": False}
    assert "Deleted 50 messages" in ctx.sent_embeds[-1].description


async def test_clear_other_http_errors_still_propagate(db):
    channel = _PurgeChannel(_http_error(400, 50035), young=0)
    ctx = _ctx(channel)
    cog = ModerationCog(bot=None)

    with pytest.raises(discord.HTTPException):
        await cog.cmd_clearall.callback(cog, ctx, "50")
    assert len(channel.calls) == 1


async def test_clear_forbidden_in_fallback_reports_no_permission(db):
    channel = _PurgeChannel(_http_error(400, 50034), young=0)

    async def _forbidden(**kwargs):
        channel.calls.append(kwargs)
        if len(channel.calls) == 1:
            raise channel.first_exc
        raise _http_error(403, 50013, discord.Forbidden)
    channel.purge = _forbidden
    ctx = _ctx(channel)
    cog = ModerationCog(bot=None)

    await cog.cmd_clearall.callback(cog, ctx, "50")

    assert ctx.sent_embeds[-1].title == "❌ No Permission"
