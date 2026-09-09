"""Embed colors for issue statuses.

An untouched issue (`not_started`, and the legacy `open`) renders grey so
it reads differently from a rejected one at a glance — both used to be
red. Fresh issue posts (`!issue` / `!bugreport`, and the feature issue
spawned from an accepted request) start grey for the same reason.
"""
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
from src.cogs.utility_cog import (
    UtilityCog, _render_issue_status_embed, _ISSUE_STATUS_TO_COLOR,
)
from src.guild_config import get_guild_cfg
from src.helpers import emb, C_RED, C_GREY, C_GREEN, C_GOLD

from tests.fakes.discord import FakeTextChannel, FakeMessage
from tests.test_issue_reactions import (
    _FakePayload, _StubBot, _make_msg_with_embed, _wire_channel_fetch_message,
)


pytestmark = pytest.mark.asyncio


async def test_status_colors_distinguish_not_started_from_rejected():
    original = emb("⚠️ Bug Report", "x", C_RED)
    assert _render_issue_status_embed(original, "not_started").color.value == C_GREY
    assert _render_issue_status_embed(original, "rejected").color.value == C_RED
    assert _render_issue_status_embed(original, "wip").color.value == C_GOLD
    assert _render_issue_status_embed(original, "completed").color.value == C_GREEN
    assert _ISSUE_STATUS_TO_COLOR["open"] == C_GREY


async def test_reset_to_not_started_turns_rejected_issue_grey(db):
    """❌ on a rejected issue re-renders it grey, not red."""
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)
    await _persistence.insert_issue(
        guild_id=42, channel_id=9000, message_id=5500,
        reporter_id=99, report="x", status="rejected",
    )
    bug_chan = FakeTextChannel(ch_id=9000)
    issue_msg = _make_msg_with_embed(
        5500, bug_chan, emb("⚠️ Bug Report", "x\n\n**Status:** Rejected", C_RED),
    )
    _wire_channel_fetch_message(bug_chan, issue_msg)
    bot = _StubBot()
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="❌", user_id=7, channel_id=9000, message_id=5500,
    ))

    new_embed = issue_msg.edit.await_args.kwargs["embed"]
    assert new_embed.color.value == C_GREY
    assert "Not started" in new_embed.description


async def test_spawned_feature_issue_starts_grey(db):
    """The feature issue posted when a request is accepted is a fresh
    not-started ticket, so it takes the not_started color."""
    guild_id = 42
    _state.bot_settings["internal_issue_channel"] = "9000"
    _state.bot_admins.add(7)
    get_guild_cfg(guild_id)["feature_request_channel"] = "8888"
    await _persistence.insert_feature_request(
        guild_id=guild_id, channel_id=8888, message_id=6500,
        reporter_id=50, description="idea",
    )
    fr_chan = FakeTextChannel(ch_id=8888)
    fr_msg = _make_msg_with_embed(6500, fr_chan, emb("📖 Feature Request", "idea", C_GOLD))
    _wire_channel_fetch_message(fr_chan, fr_msg)
    bug_chan = FakeTextChannel(ch_id=9000)
    spawned = FakeMessage(message_id=7500)
    spawned.channel = FakeTextChannel(ch_id=9000)
    bug_chan.send = AsyncMock(return_value=spawned)
    bot = _StubBot()
    bot.register_channel(fr_chan)
    bot.register_channel(bug_chan)
    cog = UtilityCog(bot=bot)

    await cog.on_raw_reaction_add(_FakePayload(
        emoji="✅", user_id=7, channel_id=8888, message_id=6500, guild_id=guild_id,
    ))

    bug_chan.send.assert_awaited_once()
    assert bug_chan.send.await_args.kwargs["embed"].color.value == C_GREY
