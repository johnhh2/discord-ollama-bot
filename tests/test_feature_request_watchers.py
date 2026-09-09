"""👀 watchers on feature requests, and the accept/reject state machine.

Covers:
- Persistence round-trip for `feature_request_watchers`.
- 👀 added / removed in a guild's `feature_request_channel` by any user
  (no admin needed) writes / drops a watcher row; silenced users, other
  channels and non-request messages are ignored.
- Notifications: completing or rejecting a request DMs the requester and
  every watcher exactly once each — from ✅ on the linked issue, 🛑 on the
  linked issue, or ❌ on the request itself.
- Un-reject: ✅ on a rejected request (rejected via ❌ on the request or 🛑
  on its linked issue) puts it back to accepted without spawning a
  duplicate issue; ❌ on an accepted request rejects the linked issue too.
"""
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.utility_cog import UtilityCog, _feature_request_excerpt
from src.guild_config import get_guild_cfg
from src.helpers import emb, C_RED, C_GOLD

from tests.fakes.discord import FakeTextChannel, FakeMessage
from tests.test_issue_reactions import (
    _FakePayload, _FakeUser, _StubBot,
    _make_msg_with_embed, _wire_channel_fetch_message,
)


pytestmark = pytest.mark.asyncio

GUILD = 42
FR_CHAN = 8888
ISSUE_CHAN = 9000
ADMIN = 7
REQUESTER = 50
WATCHER = 60


def _configure(request_msg_id: int, *, description: str = "cool idea"):
    """Guild + bot settings for a feature-request flow, plus the request
    embed in the FR channel wired for fetch. Returns (bot, fr_chan, fr_msg,
    bug_chan) — the caller inserts rows and registers users."""
    _state.bot_settings["internal_issue_channel"] = str(ISSUE_CHAN)
    _state.bot_admins.add(ADMIN)
    get_guild_cfg(GUILD)["feature_request_channel"] = str(FR_CHAN)

    fr_chan = FakeTextChannel(ch_id=FR_CHAN)
    fr_msg = _make_msg_with_embed(
        request_msg_id, fr_chan, emb("📖 Feature Request", description, C_GOLD),
    )
    _wire_channel_fetch_message(fr_chan, fr_msg)
    bug_chan = FakeTextChannel(ch_id=ISSUE_CHAN)

    bot = _StubBot()
    bot.register_channel(fr_chan)
    bot.register_channel(bug_chan)
    return bot, fr_chan, fr_msg, bug_chan


async def _linked_request(request_msg_id: int, issue_msg_id: int, *,
                          issue_status: str = "not_started",
                          request_status: str = "accepted") -> int:
    """An accepted request linked to a kind='feature' issue. Returns the
    issue id."""
    await _persistence.insert_feature_request(
        guild_id=GUILD, channel_id=FR_CHAN, message_id=request_msg_id,
        reporter_id=REQUESTER, description="linked",
    )
    await _persistence.update_feature_request_status(request_msg_id, request_status, resolved_by=ADMIN)
    issue_id = await _persistence.insert_issue(
        guild_id=GUILD, channel_id=ISSUE_CHAN, message_id=issue_msg_id,
        reporter_id=REQUESTER, report="linked", kind="feature", status=issue_status,
    )
    await _persistence.link_feature_to_request(request_msg_id, issue_id)
    return issue_id


def _wire_issue_message(bug_chan: FakeTextChannel, issue_msg_id: int) -> FakeMessage:
    issue_msg = _make_msg_with_embed(issue_msg_id, bug_chan, emb("📖 Feature", "linked", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)
    return issue_msg


def _dm_body(user: _FakeUser) -> str:
    return user.send.await_args.kwargs["embed"].description


# ── Persistence ─────────────────────────────────────────────────────────────

async def test_watcher_rows_round_trip(db):
    await _persistence.add_feature_request_watcher(6000, 60)
    await _persistence.add_feature_request_watcher(6000, 61)
    await _persistence.add_feature_request_watcher(6000, 60)  # idempotent
    await _persistence.add_feature_request_watcher(6001, 62)  # other request

    assert await _persistence.list_feature_request_watchers(6000) == [60, 61]

    await _persistence.remove_feature_request_watcher(6000, 60)
    assert await _persistence.list_feature_request_watchers(6000) == [61]
    assert await _persistence.list_feature_request_watchers(6001) == [62]
    assert await _persistence.list_feature_request_watchers(9999) == []


async def test_excerpt_collapses_whitespace_and_truncates():
    assert _feature_request_excerpt("  add\n\n  a   thing ") == "add a thing"
    assert _feature_request_excerpt(None) == ""
    long = "x" * 500
    out = _feature_request_excerpt(long)
    assert out.endswith("…")
    assert len(out) == 201


# ── 👀 add / remove ─────────────────────────────────────────────────────────

async def test_eyes_reaction_from_non_admin_adds_watcher(db):
    bot, *_ = _configure(6100)
    await _persistence.insert_feature_request(
        guild_id=GUILD, channel_id=FR_CHAN, message_id=6100,
        reporter_id=REQUESTER, description="x",
    )
    cog = UtilityCog(bot=bot)

    # WATCHER is not in bot_admins.
    await cog.on_raw_reaction_add(_FakePayload(
        emoji="👀", user_id=WATCHER, channel_id=FR_CHAN, message_id=6100, guild_id=GUILD,
    ))
    assert await _persistence.list_feature_request_watchers(6100) == [WATCHER]

    await cog.on_raw_reaction_remove(_FakePayload(
        emoji="👀", user_id=WATCHER, channel_id=FR_CHAN, message_id=6100, guild_id=GUILD,
    ))
    assert await _persistence.list_feature_request_watchers(6100) == []


async def test_eyes_reaction_ignored_when_silenced_wrong_channel_or_not_a_request(db):
    bot, *_ = _configure(6101)
    await _persistence.insert_feature_request(
        guild_id=GUILD, channel_id=FR_CHAN, message_id=6101,
        reporter_id=REQUESTER, description="x",
    )
    cog = UtilityCog(bot=bot)

    # Banned in this guild → no row.
    _state.blocklist[(GUILD, WATCHER)] = True
    await cog.on_raw_reaction_add(_FakePayload(
        emoji="👀", user_id=WATCHER, channel_id=FR_CHAN, message_id=6101, guild_id=GUILD,
    ))
    _state.blocklist.pop((GUILD, WATCHER), None)
    assert await _persistence.list_feature_request_watchers(6101) == []

    # Same message id reacted from a different channel → not the FR channel.
    await cog.on_raw_reaction_add(_FakePayload(
        emoji="👀", user_id=WATCHER, channel_id=1234, message_id=6101, guild_id=GUILD,
    ))
    assert await _persistence.list_feature_request_watchers(6101) == []

    # 👀 on a message in the FR channel that isn't a persisted request
    # (the pinned hint, chatter) → nothing.
    await cog.on_raw_reaction_add(_FakePayload(
        emoji="👀", user_id=WATCHER, channel_id=FR_CHAN, message_id=6199, guild_id=GUILD,
    ))
    assert await _persistence.list_feature_request_watchers(6199) == []


# ── Notifications ───────────────────────────────────────────────────────────

async def test_completing_linked_feature_dms_requester_and_watchers_once_each(db):
    """✅ on the linked issue → requester + every watcher DM'd. A requester
    who also reacted 👀 gets one DM, not two."""
    bot, fr_chan, fr_msg, bug_chan = _configure(6200)
    await _linked_request(6200, 7200)
    _wire_issue_message(bug_chan, 7200)
    await _persistence.add_feature_request_watcher(6200, WATCHER)
    await _persistence.add_feature_request_watcher(6200, REQUESTER)

    requester, watcher = _FakeUser(REQUESTER), _FakeUser(WATCHER)
    bot.register_user(requester)
    bot.register_user(watcher)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="✅", user_id=ADMIN, channel_id=ISSUE_CHAN, message_id=7200,
    ))

    requester.send.assert_awaited_once()
    assert "Your feature request" in _dm_body(requester)
    assert "completed" in _dm_body(requester)
    watcher.send.assert_awaited_once()
    body = _dm_body(watcher)
    assert "watching" in body
    assert "completed" in body
    assert "> linked" in body                 # description excerpt
    assert f"/{FR_CHAN}/6200" in body         # jumplink to the request, not the issue
    assert f"/{ISSUE_CHAN}/7200" not in body


async def test_rejecting_request_with_x_dms_requester_and_watchers(db):
    """❌ on an open request → status 'rejected', requester + watcher DM'd."""
    bot, fr_chan, fr_msg, bug_chan = _configure(6201)
    await _persistence.insert_feature_request(
        guild_id=GUILD, channel_id=FR_CHAN, message_id=6201,
        reporter_id=REQUESTER, description="nope",
    )
    await _persistence.add_feature_request_watcher(6201, WATCHER)
    requester, watcher = _FakeUser(REQUESTER), _FakeUser(WATCHER)
    bot.register_user(requester)
    bot.register_user(watcher)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="❌", user_id=ADMIN, channel_id=FR_CHAN, message_id=6201, guild_id=GUILD,
    ))

    request = await _persistence.get_feature_request_by_message(6201)
    assert request["status"] == "rejected"
    for user in (requester, watcher):
        user.send.assert_awaited_once()
        assert "rejected" in _dm_body(user)
        assert user.send.await_args.kwargs["embed"].title.endswith("Rejected")
    assert "Your feature request" in _dm_body(requester)
    assert "watching" in _dm_body(watcher)
    # Re-reacting ❌ on an already rejected request is a no-op — no second DM.
    await cog.on_raw_reaction_add(_FakePayload(
        emoji="❌", user_id=ADMIN, channel_id=FR_CHAN, message_id=6201, guild_id=GUILD,
    ))
    requester.send.assert_awaited_once()


async def test_rejecting_linked_issue_with_stop_sign_dms_requester_and_watchers(db):
    """🛑 on the linked issue in the issue channel → request embed shows
    Rejected and requester + watcher are DM'd."""
    bot, fr_chan, fr_msg, bug_chan = _configure(6202)
    await _linked_request(6202, 7202)
    _wire_issue_message(bug_chan, 7202)
    await _persistence.add_feature_request_watcher(6202, WATCHER)
    requester, watcher = _FakeUser(REQUESTER), _FakeUser(WATCHER)
    bot.register_user(requester)
    bot.register_user(watcher)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="🛑", user_id=ADMIN, channel_id=ISSUE_CHAN, message_id=7202,
    ))

    assert "Rejected" in fr_msg.edit.await_args.kwargs["embed"].description
    for user in (requester, watcher):
        user.send.assert_awaited_once()
        assert "rejected" in _dm_body(user)


async def test_wip_on_linked_issue_does_not_dm(db):
    """Only completed / rejected notify — ⚙️ (wip) is silent."""
    bot, fr_chan, fr_msg, bug_chan = _configure(6203)
    await _linked_request(6203, 7203)
    _wire_issue_message(bug_chan, 7203)
    await _persistence.add_feature_request_watcher(6203, WATCHER)
    requester, watcher = _FakeUser(REQUESTER), _FakeUser(WATCHER)
    bot.register_user(requester)
    bot.register_user(watcher)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="⚙️", user_id=ADMIN, channel_id=ISSUE_CHAN, message_id=7203,
    ))

    requester.send.assert_not_awaited()
    watcher.send.assert_not_awaited()


# ── Un-reject via ✅ ────────────────────────────────────────────────────────

async def test_approve_after_x_reject_without_issue_spawns_and_accepts(db):
    """❌-rejected request that never had a ticket: ✅ accepts it and spawns
    the feature issue like a fresh accept."""
    bot, fr_chan, fr_msg, bug_chan = _configure(6300)
    await _persistence.insert_feature_request(
        guild_id=GUILD, channel_id=FR_CHAN, message_id=6300,
        reporter_id=REQUESTER, description="x",
    )
    await _persistence.update_feature_request_status(6300, "rejected", resolved_by=ADMIN)
    spawned = FakeMessage(message_id=7300)
    spawned.channel = FakeTextChannel(ch_id=ISSUE_CHAN)
    bug_chan.send = AsyncMock(return_value=spawned)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="✅", user_id=ADMIN, channel_id=FR_CHAN, message_id=6300, guild_id=GUILD,
    ))

    request = await _persistence.get_feature_request_by_message(6300)
    assert request["status"] == "accepted"
    spawned_row = await _persistence.get_issue_by_message(7300)
    assert spawned_row is not None and spawned_row["kind"] == "feature"
    assert request["feature_issue_id"] == spawned_row["id"]
    assert "Not started" in fr_msg.edit.await_args.kwargs["embed"].description


async def test_approve_after_issue_rejected_reopens_ticket_without_duplicate(db):
    """Request accepted, then its ticket 🛑-rejected in the issue channel:
    ✅ on the request puts the *same* ticket back to Not started, re-renders
    both embeds, and spawns nothing new."""
    bot, fr_chan, fr_msg, bug_chan = _configure(6301)
    issue_id = await _linked_request(6301, 7301, issue_status="rejected")
    issue_msg = _wire_issue_message(bug_chan, 7301)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="✅", user_id=ADMIN, channel_id=FR_CHAN, message_id=6301, guild_id=GUILD,
    ))

    issue = await _persistence.get_issue_by_id(issue_id)
    assert issue["status"] == "not_started"
    assert issue["resolved_by"] == ADMIN
    assert "Not started" in issue_msg.edit.await_args.kwargs["embed"].description
    request = await _persistence.get_feature_request_by_message(6301)
    assert request["status"] == "accepted"
    assert request["feature_issue_id"] == issue_id
    assert "Not started" in fr_msg.edit.await_args.kwargs["embed"].description
    bug_chan.send.assert_not_awaited()


async def test_approve_after_x_reject_with_linked_issue_reuses_it(db):
    """Request that was accepted, then ❌-rejected on the request itself:
    ✅ un-rejects using the existing ticket — no duplicate spawn."""
    bot, fr_chan, fr_msg, bug_chan = _configure(6302)
    issue_id = await _linked_request(
        6302, 7302, issue_status="rejected", request_status="rejected",
    )
    _wire_issue_message(bug_chan, 7302)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="✅", user_id=ADMIN, channel_id=FR_CHAN, message_id=6302, guild_id=GUILD,
    ))

    request = await _persistence.get_feature_request_by_message(6302)
    assert request["status"] == "accepted"
    assert request["feature_issue_id"] == issue_id
    assert (await _persistence.get_issue_by_id(issue_id))["status"] == "not_started"
    bug_chan.send.assert_not_awaited()


async def test_approve_on_already_accepted_request_is_noop(db):
    bot, fr_chan, fr_msg, bug_chan = _configure(6303)
    await _linked_request(6303, 7303)
    _wire_issue_message(bug_chan, 7303)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="✅", user_id=ADMIN, channel_id=FR_CHAN, message_id=6303, guild_id=GUILD,
    ))

    fr_msg.edit.assert_not_awaited()
    bug_chan.send.assert_not_awaited()


# ── ❌ keeps the linked ticket in step ──────────────────────────────────────

async def test_reject_on_accepted_request_rejects_linked_issue_too(db):
    bot, fr_chan, fr_msg, bug_chan = _configure(6400)
    issue_id = await _linked_request(6400, 7400)
    issue_msg = _wire_issue_message(bug_chan, 7400)
    requester = _FakeUser(REQUESTER)
    bot.register_user(requester)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="❌", user_id=ADMIN, channel_id=FR_CHAN, message_id=6400, guild_id=GUILD,
    ))

    assert (await _persistence.get_feature_request_by_message(6400))["status"] == "rejected"
    assert (await _persistence.get_issue_by_id(issue_id))["status"] == "rejected"
    assert "Rejected" in issue_msg.edit.await_args.kwargs["embed"].description
    assert "Rejected" in fr_msg.edit.await_args.kwargs["embed"].description
    requester.send.assert_awaited_once()
    assert "rejected" in _dm_body(requester)


async def test_reject_on_completed_request_is_noop(db):
    """A shipped feature can't be rejected from the request embed."""
    bot, fr_chan, fr_msg, bug_chan = _configure(6401)
    issue_id = await _linked_request(6401, 7401, issue_status="completed")
    _wire_issue_message(bug_chan, 7401)
    requester = _FakeUser(REQUESTER)
    bot.register_user(requester)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="❌", user_id=ADMIN, channel_id=FR_CHAN, message_id=6401, guild_id=GUILD,
    ))

    assert (await _persistence.get_feature_request_by_message(6401))["status"] == "accepted"
    assert (await _persistence.get_issue_by_id(issue_id))["status"] == "completed"
    fr_msg.edit.assert_not_awaited()
    requester.send.assert_not_awaited()
