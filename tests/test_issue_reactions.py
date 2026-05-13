"""Tests for `on_raw_reaction_add` / `on_raw_reaction_remove` on UtilityCog.

Covers:
- Issue triage (❌/⚙️/✅/🛑) in `internal_issue_channel`: status updates +
  embed re-render; admin-only gate; soft-deleted-row no-op.
- 🔇 mute / unmute for `kind='error'` issues.
- Feature-request triage (✅/❌) in a guild's `feature_request_channel`:
  accept spawns a linked feature issue, reject just flips status.
- Status propagation: completing a spawned feature issue mirrors the
  status back to its originating feature_request embed.
- DM-on-completion: bug + linked-feature DM their reporter; admin-filed
  feature (no link) doesn't.
"""
from unittest.mock import AsyncMock

import discord
import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.utility_cog import UtilityCog
from src.guild_config import get_guild_cfg
from src.helpers import emb, C_RED, C_GOLD

from tests.fakes.discord import (
    FakeTextChannel, FakeMessage,
)


pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────────────

class _FakePayload:
    """Duck-typed stand-in for discord.RawReactionActionEvent.

    The cog only reads .emoji (as `str(...)`), .user_id, .channel_id,
    .message_id, .guild_id — so a plain attribute bag works.
    """
    def __init__(self, *, emoji: str, user_id: int, channel_id: int,
                 message_id: int, guild_id: int | None = None):
        self.emoji = emoji
        self.user_id = user_id
        self.channel_id = channel_id
        self.message_id = message_id
        self.guild_id = guild_id


class _FakeUser:
    """Minimal stand-in for discord.User — only needs `.send`."""
    def __init__(self, uid: int):
        self.id = uid
        self.send = AsyncMock()


class _StubBot:
    """Bot stand-in with a registry of channels and users.

    Tests register their channels/users up front so the cog's `get_channel`
    / `fetch_channel` / `get_user` lookups resolve to the right fakes.
    """
    def __init__(self, *, user_id: int = 999):
        self.user = type("U", (), {"id": user_id})()
        self._channels: dict[int, object] = {}
        self._users: dict[int, _FakeUser] = {}

    def register_channel(self, ch):
        self._channels[int(ch.id)] = ch

    def register_user(self, user):
        self._users[int(user.id)] = user

    def get_channel(self, cid):
        return self._channels.get(int(cid))

    async def fetch_channel(self, cid):
        return self._channels.get(int(cid))

    def get_user(self, uid):
        return self._users.get(int(uid))

    async def fetch_user(self, uid):
        return self._users.get(int(uid))


def _make_msg_with_embed(message_id: int, channel, embed: discord.Embed) -> FakeMessage:
    """A FakeMessage carrying a real discord.Embed (so the renderer can read
    .description / .title from it). `.edit` is the AsyncMock from FakeMessage."""
    msg = FakeMessage(message_id=message_id)
    msg.channel = channel
    msg.embeds = [embed]
    return msg


def _wire_channel_fetch_message(channel: FakeTextChannel, msg: FakeMessage):
    """Attach a `fetch_message` AsyncMock to a FakeTextChannel so the cog's
    `await channel.fetch_message(payload.message_id)` resolves."""
    channel.fetch_message = AsyncMock(return_value=msg)


# ── Issue triage ─────────────────────────────────────────────────────────────

async def test_status_reaction_updates_db_and_rerenders_embed(db):
    """Admin reacts ⚙️ in internal_issue_channel → status persists as 'wip' and
    the embed's description gets the Status footer appended."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)

    await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5000,
        reporter_id=99, report="x",
    )
    bug_chan = FakeTextChannel(ch_id=9000)
    original_embed = emb("⚠️ Bug Report", "**Description:** x", C_RED)
    issue_msg = _make_msg_with_embed(5000, bug_chan, original_embed)
    _wire_channel_fetch_message(bug_chan, issue_msg)

    bot = _StubBot()
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="⚙️", user_id=7, channel_id=9000, message_id=5000)
    await cog.on_raw_reaction_add(payload)

    # DB row updated
    row = await _persistence.get_issue_by_message(5000)
    assert row["status"] == "wip"
    assert row["resolved_by"] == 7
    # Embed edited with the WIP status footer
    issue_msg.edit.assert_awaited_once()
    new_embed = issue_msg.edit.await_args.kwargs["embed"]
    assert "Status:" in new_embed.description
    assert "Work in progress" in new_embed.description


async def test_reaction_from_non_admin_is_ignored(db):
    """Non-bot-admin reacting to an issue embed → no DB change."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    # No bot_admins set → reactor 7 is not an admin

    await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5001,
        reporter_id=99, report="x",
    )
    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(5001, bug_chan, emb("⚠️ Bug Report", "x", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)

    bot = _StubBot()
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="⚙️", user_id=7, channel_id=9000, message_id=5001)
    await cog.on_raw_reaction_add(payload)

    row = await _persistence.get_issue_by_message(5001)
    assert row["status"] == "not_started"
    issue_msg.edit.assert_not_awaited()


async def test_reaction_in_wrong_channel_ignored(db):
    """Reaction outside `internal_issue_channel` and outside any
    `feature_request_channel` is a no-op."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)

    await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5002,
        reporter_id=99, report="x",
    )
    bot = _StubBot()
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="⚙️", user_id=7, channel_id=12345, message_id=5002)
    await cog.on_raw_reaction_add(payload)

    row = await _persistence.get_issue_by_message(5002)
    assert row["status"] == "not_started"


async def test_reaction_on_soft_deleted_issue_is_noop(db):
    """Once an issue is soft-deleted, reactions stop having effect."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)

    iid = await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5003,
        reporter_id=99, report="x",
    )
    await _persistence.soft_delete_issue(iid)
    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(5003, bug_chan, emb("⚠️ Bug Report", "x", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="⚙️", user_id=7, channel_id=9000, message_id=5003)
    await cog.on_raw_reaction_add(payload)

    row = await _persistence.get_issue_by_message(5003)
    assert row["status"] == "not_started"
    issue_msg.edit.assert_not_awaited()


# ── 🔇 mute / unmute ────────────────────────────────────────────────────────

async def test_mute_emoji_on_error_issue_persists(db):
    """🔇 on a kind='error' issue → mute_key added to state.error_mutes
    AND persisted to error_mutes table."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)
    _state.error_mutes.clear()

    mute_key = "flip:ValueError:boom"
    await _persistence.insert_issue(
        guild_id=None, channel_id=9000, message_id=5100,
        reporter_id=99, report="err",
        kind="error", mute_key=mute_key,
    )
    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(5100, bug_chan, emb("⚠️ Command Error", "err", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="🔇", user_id=7, channel_id=9000, message_id=5100)
    await cog.on_raw_reaction_add(payload)

    assert mute_key in _state.error_mutes
    persisted = await _persistence.load_error_mutes()
    assert mute_key in persisted


async def test_mute_emoji_on_non_error_issue_noop(db):
    """🔇 on a kind='bug' issue does nothing — no mute_key exists to toggle."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)
    _state.error_mutes.clear()

    await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5101,
        reporter_id=99, report="bug",
    )
    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(5101, bug_chan, emb("⚠️ Bug Report", "x", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="🔇", user_id=7, channel_id=9000, message_id=5101)
    await cog.on_raw_reaction_add(payload)

    persisted = await _persistence.load_error_mutes()
    assert persisted == set()


async def test_unmute_via_raw_reaction_remove(db):
    """Removing the 🔇 reaction → mute_key removed from state AND DB."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)

    mute_key = "ping:RuntimeError:err"
    await _persistence.insert_issue(
        guild_id=None, channel_id=9000, message_id=5102,
        reporter_id=99, report="x",
        kind="error", mute_key=mute_key,
    )
    # Pre-mute it so the remove handler has something to undo.
    _state.error_mutes.add(mute_key)
    await _persistence.insert_error_mute(mute_key, muted_by=7)

    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(5102, bug_chan, emb("⚠️ Command Error", "x", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="🔇", user_id=7, channel_id=9000, message_id=5102)
    await cog.on_raw_reaction_remove(payload)

    assert mute_key not in _state.error_mutes
    persisted = await _persistence.load_error_mutes()
    assert mute_key not in persisted


# ── Feature-request triage ──────────────────────────────────────────────────

async def test_feature_request_accept_spawns_linked_feature_issue(db):
    """✅ on a request in the per-guild feature_request_channel:
    - request status → 'accepted'
    - a kind='feature' issue is posted to internal_issue_channel
    - the spawned issue is linked back via feature_issue_id
    - the request embed gets re-rendered with the linked status footer."""
    guild_id = 42
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)
    cfg = get_guild_cfg(guild_id)
    cfg["feature_request_channel"] = "8888"

    await _persistence.insert_feature_request(
        guild_id=guild_id, channel_id=8888, message_id=6000,
        reporter_id=50, description="cool idea",
    )
    fr_chan = FakeTextChannel(ch_id=8888)
    fr_msg = _make_msg_with_embed(6000, fr_chan, emb("📖 Feature Request", "cool idea", C_GOLD))
    _wire_channel_fetch_message(fr_chan, fr_msg)

    bug_chan = FakeTextChannel(ch_id=9000)
    spawned = FakeMessage(message_id=7777)
    spawned.channel = FakeTextChannel(ch_id=9000)
    bug_chan.send = AsyncMock(return_value=spawned)

    bot = _StubBot()
    bot.register_channel(fr_chan)
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(
        emoji="✅", user_id=7, channel_id=8888, message_id=6000, guild_id=guild_id,
    )
    await cog.on_raw_reaction_add(payload)

    # 1. request status moved to 'accepted'
    request = await _persistence.get_feature_request_by_message(6000)
    assert request["status"] == "accepted"
    assert request["resolved_by"] == 7
    # 2. spawned feature issue exists in DB with kind='feature'
    spawned_row = await _persistence.get_issue_by_message(7777)
    assert spawned_row is not None
    assert spawned_row["kind"] == "feature"
    # 3. feature_issue_id link recorded on the request
    assert request["feature_issue_id"] == spawned_row["id"]
    # 4. request embed re-rendered
    fr_msg.edit.assert_awaited()


async def test_feature_request_reject_no_spawn(db):
    """❌ on a request: status → 'rejected', NO spawned feature issue."""
    guild_id = 42
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)
    cfg = get_guild_cfg(guild_id)
    cfg["feature_request_channel"] = "8888"

    await _persistence.insert_feature_request(
        guild_id=guild_id, channel_id=8888, message_id=6001,
        reporter_id=50, description="nope",
    )
    fr_chan = FakeTextChannel(ch_id=8888)
    fr_msg = _make_msg_with_embed(6001, fr_chan, emb("📖 Feature Request", "nope", C_GOLD))
    _wire_channel_fetch_message(fr_chan, fr_msg)

    bot = _StubBot()
    bot.register_channel(fr_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(
        emoji="❌", user_id=7, channel_id=8888, message_id=6001, guild_id=guild_id,
    )
    await cog.on_raw_reaction_add(payload)

    request = await _persistence.get_feature_request_by_message(6001)
    assert request["status"] == "rejected"
    assert request["feature_issue_id"] is None


# ── Status propagation: feature issue → request embed ───────────────────────

async def test_feature_issue_status_change_mirrors_to_request_embed(db):
    """When a spawned feature's status changes, the originating
    feature_request embed gets re-edited with the new feature_status."""
    guild_id = 42
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)
    cfg = get_guild_cfg(guild_id)
    cfg["feature_request_channel"] = "8888"

    # Set up: a feature_request already accepted + linked to a kind='feature'
    # issue. (We skip the accept flow and wire it up directly.)
    await _persistence.insert_feature_request(
        guild_id=guild_id, channel_id=8888, message_id=6010,
        reporter_id=50, description="linked",
    )
    await _persistence.update_feature_request_status(6010, "accepted", resolved_by=7)
    feature_id = await _persistence.insert_issue(
        guild_id=guild_id, channel_id=9000, message_id=7100,
        reporter_id=50, report="linked", kind="feature",
    )
    await _persistence.link_feature_to_request(6010, feature_id)

    # The feature issue in internal_issue_channel — admin reacts ⚙️ on it.
    bug_chan = FakeTextChannel(ch_id=9000)
    feature_msg = _make_msg_with_embed(7100, bug_chan, emb("📖 Feature", "linked", C_RED))
    _wire_channel_fetch_message(bug_chan, feature_msg)
    # The request in feature_request_channel — should get re-rendered with
    # the new feature_status.
    fr_chan = FakeTextChannel(ch_id=8888)
    fr_msg = _make_msg_with_embed(6010, fr_chan, emb("📖 Feature Request", "linked", C_GOLD))
    _wire_channel_fetch_message(fr_chan, fr_msg)

    bot = _StubBot()
    bot.register_channel(bug_chan)
    bot.register_channel(fr_chan)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="⚙️", user_id=7, channel_id=9000, message_id=7100)
    await cog.on_raw_reaction_add(payload)

    feature_msg.edit.assert_awaited()
    # The request embed should also have been re-edited with the WIP label.
    fr_msg.edit.assert_awaited()
    last_edit = fr_msg.edit.await_args.kwargs["embed"]
    assert "Work in progress" in last_edit.description


# ── DM-on-completion ────────────────────────────────────────────────────────

async def test_completed_bug_dms_reporter(db):
    """Admin marks a kind='bug' issue completed → bot DMs the reporter."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)

    # source_* point at the user-visible channel where the !bugreport was
    # typed; channel_id/message_id are the admin-only bug-report embed.
    await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5200,
        reporter_id=99, report="x",
        source_channel_id=4242, source_message_id=8484,
    )
    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(5200, bug_chan, emb("⚠️ Bug Report", "x", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)

    reporter = _FakeUser(uid=99)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    bot.register_user(reporter)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="✅", user_id=7, channel_id=9000, message_id=5200)
    await cog.on_raw_reaction_add(payload)

    reporter.send.assert_awaited_once()
    body = reporter.send.await_args.kwargs["embed"].description
    assert "completed" in body.lower()
    # DM links to the user-visible source command, NOT the admin-only embed.
    assert "/4242/8484" in body
    assert "/9000/5200" not in body


async def test_completed_bug_without_source_coords_dms_without_link(db):
    """Legacy rows that predate the source_* columns get a plain DM with no
    jumplink rather than a link into the admin-only bug-report channel."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)

    await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5201,
        reporter_id=99, report="x",
        # No source_channel_id / source_message_id → fall back to no link.
    )
    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(5201, bug_chan, emb("⚠️ Bug Report", "x", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)

    reporter = _FakeUser(uid=99)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    bot.register_user(reporter)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="✅", user_id=7, channel_id=9000, message_id=5201)
    await cog.on_raw_reaction_add(payload)

    reporter.send.assert_awaited_once()
    body = reporter.send.await_args.kwargs["embed"].description
    assert "completed" in body.lower()
    # No jumplink anywhere — neither to source nor to the admin embed.
    assert "https://discord.com/channels/" not in body


async def test_completed_linked_feature_dms_requester(db):
    """Completing a feature issue spawned from a !featurerequest → DM goes
    to the original requester with a jumplink to the request embed."""
    guild_id = 42
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)

    await _persistence.insert_feature_request(
        guild_id=guild_id, channel_id=8888, message_id=6020,
        reporter_id=50, description="x",
    )
    await _persistence.update_feature_request_status(6020, "accepted", resolved_by=7)
    feature_id = await _persistence.insert_issue(
        guild_id=guild_id, channel_id=9000, message_id=7200,
        reporter_id=50, report="x", kind="feature",
    )
    await _persistence.link_feature_to_request(6020, feature_id)

    bug_chan = FakeTextChannel(ch_id=9000)
    feature_msg = _make_msg_with_embed(7200, bug_chan, emb("📖 Feature", "x", C_RED))
    _wire_channel_fetch_message(bug_chan, feature_msg)
    fr_chan = FakeTextChannel(ch_id=8888)
    fr_msg = _make_msg_with_embed(6020, fr_chan, emb("📖 Feature Request", "x", C_GOLD))
    _wire_channel_fetch_message(fr_chan, fr_msg)

    requester = _FakeUser(uid=50)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    bot.register_channel(fr_chan)
    bot.register_user(requester)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="✅", user_id=7, channel_id=9000, message_id=7200)
    await cog.on_raw_reaction_add(payload)

    requester.send.assert_awaited_once()
    body = requester.send.await_args.kwargs["embed"].description
    # Jumplink points to the request, not the feature issue.
    assert "/8888/6020" in body


async def test_completed_admin_filed_feature_does_not_dm(db):
    """A kind='feature' row with NO linked feature_request (admin-filed via
    `!issue feature`) → no DM."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)

    await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5300,
        reporter_id=99, report="x", kind="feature",
    )
    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(5300, bug_chan, emb("📖 Feature", "x", C_RED))
    _wire_channel_fetch_message(bug_chan, issue_msg)

    reporter = _FakeUser(uid=99)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    bot.register_user(reporter)
    cog = UtilityCog(bot=bot)

    payload = _FakePayload(emoji="✅", user_id=7, channel_id=9000, message_id=5300)
    await cog.on_raw_reaction_add(payload)

    reporter.send.assert_not_awaited()
