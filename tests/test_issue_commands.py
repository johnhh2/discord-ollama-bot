"""Command-flow tests for !bugreport, !issue, !issues, and !issue delete.

These tests exercise the cog's command callbacks end-to-end against the
real persistence layer (via the `db` fixture), so DB rows really get
inserted/updated. Discord I/O is mocked via the FakeCtx / FakeMessage /
FakeTextChannel helpers in tests/fakes/discord.py.

The autouse `reset_bot_state` fixture clears `bot_admins` and `command_perms`
per test; `_admin_ctx` re-seeds the caller as a bot admin and adds a
permissive `command_perms` entry so `@requires_perm` lets the call through.
"""
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.utility_cog import UtilityCog

from tests.fakes.discord import (
    FakeCtx, FakeMember, FakeGuild, FakeTextChannel, FakeMessage,
)


pytestmark = pytest.mark.asyncio


# ── Test plumbing ────────────────────────────────────────────────────────────

class _StubBot:
    """Minimal Bot stand-in for the cog. `get_channel` returns the channel
    we were given (regardless of id); `fetch_channel` is the async fallback.
    Tests that need to assert the lookup id pass a single channel and trust
    the cog to call `get_channel` exactly once."""

    def __init__(self, channel=None, user_id: int = 999):
        self._channel = channel
        self.user = type("U", (), {"id": user_id})()

    def get_channel(self, cid: int):
        return self._channel

    async def fetch_channel(self, cid: int):
        return self._channel

    def get_user(self, uid: int):
        return None

    async def fetch_user(self, uid: int):
        return None


def _admin_ctx(*, uid: int = 1, guild_id: int = 42, command: str = "bugreport") -> FakeCtx:
    """FakeCtx whose author is in state.bot_admins and whose command name is
    registered in state.command_perms — so @requires_perm always lets the
    call through, regardless of the tier the command needs."""
    author = FakeMember(uid=uid)
    ctx = FakeCtx(author=author, guild=FakeGuild(gid=guild_id))
    ctx.command.qualified_name = command
    _state.bot_admins.add(uid)
    _state.command_perms[command] = {"tier": "bot_admin", "hidden": False}
    return ctx


def _make_posted_message(message_id: int = 5000, channel_id: int = 9000) -> FakeMessage:
    """A FakeMessage standing in for the embed posted to internal_issue_channel.
    Its `.channel` carries the right id so `insert_issue` records it."""
    posted = FakeMessage(message_id=message_id)
    posted.channel = FakeTextChannel(ch_id=channel_id)
    return posted


# ── !bugreport ──────────────────────────────────────────────────────────────

async def test_bugreport_no_channel_configured(db):
    """Without internal_issue_channel set, the command tells the user it's
    unconfigured and does NOT write a row."""
    _state.bot_settings.pop("internal_issue_channel", None)
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="bugreport")

    await cog.cmd_bugreport.callback(cog, ctx, report="something is wrong")

    assert ctx.sent_embeds, "should have replied with an unconfigured embed"
    rows = await _persistence.list_issues(include_deleted=True)
    assert rows == []


async def test_bugreport_happy_path_persists_and_reacts(db):
    """When configured, the command posts the embed to internal_issue_channel,
    inserts an issues row, and seeds ❌ ⚙️ ✅ 🛑 reactions."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    posted = _make_posted_message(message_id=5000, channel_id=9000)
    log_chan = FakeTextChannel(ch_id=9000)
    log_chan.send = AsyncMock(return_value=posted)

    cog = UtilityCog(bot=_StubBot(channel=log_chan))
    ctx = _admin_ctx(command="bugreport")

    await cog.cmd_bugreport.callback(cog, ctx, report="repro: do X, get Y")

    assert log_chan.send.await_count == 1
    row = await _persistence.get_issue_by_message(5000)
    assert row is not None
    assert row["kind"] == "bug"
    assert row["status"] == "not_started"
    assert "repro: do X" in row["report"]
    reactions = [c.args[0] for c in posted.add_reaction.await_args_list]
    assert reactions == ["❌", "⚙️", "✅", "🛑"]


async def test_bugreport_empty_report_shows_usage(db):
    """An empty / whitespace-only report shows usage and writes nothing."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    cog = UtilityCog(bot=_StubBot(channel=FakeTextChannel(ch_id=9000)))
    ctx = _admin_ctx(command="bugreport")

    await cog.cmd_bugreport.callback(cog, ctx, report="   ")

    rows = await _persistence.list_issues(include_deleted=True)
    assert rows == []


# ── !issue <kind> <desc> ────────────────────────────────────────────────────

async def test_issue_bug_routes_to_submit_issue(db):
    """`!issue bug <desc>` is equivalent to `!bugreport <desc>` — kind='bug',
    same persistence + reactions."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    posted = _make_posted_message(message_id=5100, channel_id=9000)
    log_chan = FakeTextChannel(ch_id=9000)
    log_chan.send = AsyncMock(return_value=posted)

    cog = UtilityCog(bot=_StubBot(channel=log_chan))
    ctx = _admin_ctx(command="issue")

    await cog.cmd_issue.callback(cog, ctx, kind="bug", rest="from !issue bug")
    row = await _persistence.get_issue_by_message(5100)
    assert row is not None
    assert row["kind"] == "bug"


async def test_issue_feature_sets_kind_feature(db):
    """`!issue feature <desc>` writes a kind='feature' row."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    posted = _make_posted_message(message_id=5101, channel_id=9000)
    log_chan = FakeTextChannel(ch_id=9000)
    log_chan.send = AsyncMock(return_value=posted)

    cog = UtilityCog(bot=_StubBot(channel=log_chan))
    ctx = _admin_ctx(command="issue")

    await cog.cmd_issue.callback(cog, ctx, kind="feature", rest="cool idea")
    row = await _persistence.get_issue_by_message(5101)
    assert row is not None
    assert row["kind"] == "feature"


async def test_issue_unknown_kind_shows_usage(db):
    """An unrecognized kind (e.g. `!issue banana ...`) shows usage and
    writes nothing — including no DB row, no embed to the log channel."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    log_chan = FakeTextChannel(ch_id=9000)
    log_chan.send = AsyncMock(return_value=_make_posted_message())
    cog = UtilityCog(bot=_StubBot(channel=log_chan))
    ctx = _admin_ctx(command="issue")

    await cog.cmd_issue.callback(cog, ctx, kind="banana", rest="x")
    assert log_chan.send.await_count == 0
    rows = await _persistence.list_issues(include_deleted=True)
    assert rows == []


# ── !issues listing ─────────────────────────────────────────────────────────

async def _seed_three_issues(reporter_id: int = 1) -> list[int]:
    """Seed not_started / wip / completed rows. Returns the ids in insertion order.

    The first row keeps the seeded default ('not_started'); the other two
    are re-stamped via update_issue_status.
    """
    ids = []
    for i, status in enumerate(["not_started", "wip", "completed"]):
        iid = await _persistence.insert_issue(
            guild_id=42, channel_id=9000, message_id=7000 + i,
            reporter_id=reporter_id, report=f"row {i}",
        )
        if status != "not_started":
            await _persistence.update_issue_status(7000 + i, status, resolved_by=99)
        ids.append(iid)
    return ids


async def test_issues_default_filter_hides_completed(db):
    """Default `!issues` (no arg) shows open/not_started/wip; completed and
    rejected don't appear. The cog caches the displayed ids on the caller."""
    ids = await _seed_three_issues()
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issues")

    await cog.cmd_issues.callback(cog, ctx, filt=None)
    cached = cog._issues_listing_by_user.get(ctx.author.id)
    assert cached is not None
    assert ids[2] not in cached  # completed
    assert set(cached) == {ids[0], ids[1]}  # open + wip


async def test_issues_all_filter_includes_completed(db):
    ids = await _seed_three_issues()
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issues")

    await cog.cmd_issues.callback(cog, ctx, filt="all")
    cached = cog._issues_listing_by_user[ctx.author.id]
    assert set(cached) == set(ids)


async def test_issues_explicit_status_filter(db):
    ids = await _seed_three_issues()
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issues")

    await cog.cmd_issues.callback(cog, ctx, filt="completed")
    cached = cog._issues_listing_by_user[ctx.author.id]
    assert cached == [ids[2]]


async def test_issues_unknown_filter_shows_usage(db):
    """An invalid filter argument shows usage and does NOT populate the
    listing cache."""
    await _seed_three_issues()
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issues")

    await cog.cmd_issues.callback(cog, ctx, filt="banana")
    assert cog._issues_listing_by_user.get(ctx.author.id) is None


async def test_issues_empty_result_resets_cache(db):
    """When no issues match the filter, the cog still resets the caller's
    cache to [] so a stale `!issue delete 1` doesn't target a previous row."""
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issues")

    await cog.cmd_issues.callback(cog, ctx, filt=None)
    assert cog._issues_listing_by_user[ctx.author.id] == []


# ── !issue delete <N> ───────────────────────────────────────────────────────

async def test_issue_delete_without_prior_listing_warns(db):
    """`!issue delete 1` with no prior `!issues` call replies with a hint
    instead of nuking a row by guesswork."""
    iid = await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=8000,
        reporter_id=1, report="x",
    )
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issue")

    await cog.cmd_issue.callback(cog, ctx, kind="delete", rest="1")
    row = await _persistence.get_issue_by_id(iid)
    assert row["deleted"] is False


async def test_issue_delete_soft_deletes_nth_row(db):
    """After `!issues`, `!issue delete N` soft-deletes the right row and
    drops it from the cache so N+1 still lines up."""
    ids = await _seed_three_issues()
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issues")
    await cog.cmd_issues.callback(cog, ctx, filt="all")
    cached_before = list(cog._issues_listing_by_user[ctx.author.id])
    target_id = cached_before[0]  # 1st row in listing

    await cog.cmd_issue.callback(cog, ctx, kind="delete", rest="1")

    row = await _persistence.get_issue_by_id(target_id)
    assert row["deleted"] is True
    cached_after = cog._issues_listing_by_user[ctx.author.id]
    assert target_id not in cached_after
    assert len(cached_after) == len(cached_before) - 1
    # Make sure the surviving order matches the original listing minus index 0.
    assert cached_after == cached_before[1:]
    # Sanity: the remaining ids weren't deleted.
    for surviving in cached_after:
        srow = await _persistence.get_issue_by_id(surviving)
        assert srow["deleted"] is False
    # Avoid unused-var warning on ids
    assert set(ids) == set(cached_before)


async def test_issue_delete_out_of_range_rejects(db):
    """A number larger than the listing is rejected — no row touched."""
    await _seed_three_issues()
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issues")
    await cog.cmd_issues.callback(cog, ctx, filt="all")
    cached = list(cog._issues_listing_by_user[ctx.author.id])

    await cog.cmd_issue.callback(cog, ctx, kind="delete", rest="99")

    # No row deleted, cache untouched.
    for iid in cached:
        row = await _persistence.get_issue_by_id(iid)
        assert row["deleted"] is False
    assert cog._issues_listing_by_user[ctx.author.id] == cached


async def test_issue_remove_alias_works_too(db):
    """`!issue remove N` is a synonym for `!issue delete N`."""
    await _seed_three_issues()
    cog = UtilityCog(bot=_StubBot())
    ctx = _admin_ctx(command="issues")
    await cog.cmd_issues.callback(cog, ctx, filt="all")
    target_id = cog._issues_listing_by_user[ctx.author.id][0]

    await cog.cmd_issue.callback(cog, ctx, kind="remove", rest="1")

    row = await _persistence.get_issue_by_id(target_id)
    assert row["deleted"] is True
