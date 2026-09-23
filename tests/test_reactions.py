"""src/reactions.py — reaction buttons that never miss an early click.

`ReactionCollector` must queue a reaction that lands while the bot is still
seeding the rest of the buttons (the click `bot.wait_for` used to drop), and
the invite helpers built on it must count such a click. `seed_reactions`
is best-effort: it skips a transient failure and stops on a permission one.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.reactions import ReactionCollector, seed_reactions
from tests.fakes.discord import (
    FakeListenerBot, FakeMember, FakeMessage, raw_reaction,
)

pytestmark = pytest.mark.asyncio


def _http_error(cls, status: int):
    return cls(SimpleNamespace(status=status, reason="test"), "test")


def _click_during_seeding(bot: FakeListenerBot, msg: FakeMessage, *clicks):
    """Make `msg.add_reaction` deliver each of `clicks` — (emoji, user) pairs
    — while the *first* button is still being added, i.e. before seeding
    has finished. Returns the mock so tests can inspect the seeding calls."""
    async def _add(emoji):
        if _add.calls == 0:
            for click_emoji, user in clicks:
                await bot.dispatch("raw_reaction_add", raw_reaction(msg.id, click_emoji, user))
        _add.calls += 1
    _add.calls = 0
    msg.add_reaction = AsyncMock(side_effect=_add)
    return msg.add_reaction


# ── ReactionCollector ─────────────────────────────────────────────────────────

async def test_collector_queues_a_click_that_lands_during_seeding():
    bot = FakeListenerBot()
    msg = FakeMessage(message_id=50)
    user = FakeMember(uid=7, display_name="early")
    _click_during_seeding(bot, msg, ("✅", user))

    async with ReactionCollector(bot, msg) as reactions:
        assert await seed_reactions(msg, ["✅", "❌"], what="t") is True
        emoji, who = await reactions.next(timeout=0.1)

    assert (emoji, who) == ("✅", user)
    # Listener is gone once the block exits.
    assert bot.listeners["on_raw_reaction_add"] == []


async def test_collector_ignores_the_bot_other_bots_and_other_messages():
    bot = FakeListenerBot()
    msg = FakeMessage(message_id=50)
    other_bot = FakeMember(uid=8)
    other_bot.bot = True
    human = FakeMember(uid=9)

    async with ReactionCollector(bot, msg) as reactions:
        await bot.dispatch("raw_reaction_add", raw_reaction(50, "✅", bot.user))
        await bot.dispatch("raw_reaction_add", raw_reaction(50, "✅", other_bot))
        await bot.dispatch("raw_reaction_add", raw_reaction(51, "✅", human))
        with pytest.raises(asyncio.TimeoutError):
            await reactions.next(timeout=0.01)


async def test_collector_resolves_dm_reactors_through_the_bot_and_drops_unknowns():
    """A DM payload carries no Member; the collector falls back to
    bot.get_user / fetch_user and drops the reaction if neither knows them."""
    bot = FakeListenerBot()
    msg = FakeMessage(message_id=50)
    known = FakeMember(uid=11)
    unknown = FakeMember(uid=12)
    bot.users[known.id] = known

    async with ReactionCollector(bot, msg) as reactions:
        await bot.dispatch("raw_reaction_add", raw_reaction(50, "❌", unknown, guild_id=None, member=False))
        await bot.dispatch("raw_reaction_add", raw_reaction(50, "✅", known, guild_id=None, member=False))
        assert await reactions.next(timeout=0.1) == ("✅", known)
        with pytest.raises(asyncio.TimeoutError):
            await reactions.next(timeout=0.01)


async def test_collector_start_is_idempotent_and_stop_is_safe_twice():
    bot = FakeListenerBot()
    collector = ReactionCollector(bot, FakeMessage(message_id=50))
    collector.start()
    collector.start()
    assert len(bot.listeners["on_raw_reaction_add"]) == 1
    collector.stop()
    collector.stop()
    assert bot.listeners["on_raw_reaction_add"] == []


# ── seed_reactions ────────────────────────────────────────────────────────────

async def test_seed_reactions_skips_a_transient_failure_and_reports_it():
    msg = FakeMessage()
    msg.add_reaction = AsyncMock(side_effect=[None, _http_error(discord.HTTPException, 500), None])

    assert await seed_reactions(msg, ["1", "2", "3"], what="t") is False
    assert [c.args[0] for c in msg.add_reaction.await_args_list] == ["1", "2", "3"]


async def test_seed_reactions_stops_at_a_permission_error():
    """No Add Reactions permission fails every later call the same way —
    don't burn four more HTTP calls finding that out."""
    msg = FakeMessage()
    msg.add_reaction = AsyncMock(side_effect=[None, _http_error(discord.Forbidden, 403), None])

    assert await seed_reactions(msg, ["1", "2", "3"], what="t") is False
    assert msg.add_reaction.await_count == 2


async def test_seed_reactions_all_landed():
    msg = FakeMessage()
    assert await seed_reactions(msg, ["1", "2"], what="t") is True
    assert msg.add_reaction.await_count == 2
