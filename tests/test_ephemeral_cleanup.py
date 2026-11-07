"""Ephemeral replies: the timer removes its own row, and the boot sweep
deletes only what a restart cut off (src/helpers.py send_ephemeral,
EventsCog._sweep_ephemeral_msgs)."""
import asyncio
import datetime
import time
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import src.helpers as _helpers
import src.persistence as _persistence
from src import state
from src.events import EventsCog

pytestmark = pytest.mark.asyncio


def _snowflake(seconds_ago: float) -> int:
    when = discord.utils.utcnow() - datetime.timedelta(seconds=seconds_ago)
    return discord.utils.time_snowflake(when)


async def _rows(db) -> list[int]:
    async with db.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT message_id FROM ephemeral_msgs ORDER BY message_id")
            return [r[0] for r in await cur.fetchall()]


def _http_error(cls, status):
    return cls(MagicMock(status=status, reason="x"), "x")


@pytest.fixture
def booted_now(monkeypatch):
    monkeypatch.setattr(state, "bot_start_time", time.monotonic())


class _FakeBot:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.deleted: list[int] = []

    def get_partial_messageable(self, channel_id):
        bot = self

        class _Channel:
            def get_partial_message(self, message_id):
                async def _delete():
                    bot.deleted.append(message_id)
                    outcome = bot.outcomes.get(message_id)
                    if outcome is not None:
                        raise outcome
                return MagicMock(delete=_delete)
        return _Channel()


async def test_timer_deletes_message_and_its_row(db, monkeypatch):
    monkeypatch.setattr(_helpers, "EPHEMERAL_DELETE_AFTER", 0)
    msg = MagicMock(id=_snowflake(0), channel=MagicMock(id=5), delete=AsyncMock())
    ctx = MagicMock(send=AsyncMock(return_value=msg))

    await _helpers.send_ephemeral(ctx, "hi")
    assert "delete_after" not in ctx.send.await_args.kwargs
    assert await _rows(db) == [msg.id]

    await asyncio.gather(*_helpers._ephemeral_timers)
    msg.delete.assert_awaited_once()
    assert await _rows(db) == []


async def test_timer_drops_row_when_message_already_gone(db, monkeypatch):
    monkeypatch.setattr(_helpers, "EPHEMERAL_DELETE_AFTER", 0)
    msg = MagicMock(id=_snowflake(0), channel=MagicMock(id=5),
                    delete=AsyncMock(side_effect=_http_error(discord.NotFound, 404)))
    await _helpers.send_ephemeral(MagicMock(send=AsyncMock(return_value=msg)), "hi")

    await asyncio.gather(*_helpers._ephemeral_timers)
    assert await _rows(db) == []


async def test_sweep_deletes_pre_boot_rows_newest_first(db, booted_now):
    old, older, oldest = _snowflake(600), _snowflake(1200), _snowflake(1800)
    live = _snowflake(-5)  # sent by this process; its timer is still running
    for mid in (oldest, older, old, live):
        await _persistence.add_ephemeral_msg(5, mid)
    bot = _FakeBot({
        older: _http_error(discord.NotFound, 404),
        oldest: _http_error(discord.Forbidden, 403),
    })

    await EventsCog(bot)._sweep_ephemeral_msgs()

    assert bot.deleted == [old, older, oldest]
    assert await _rows(db) == [live]


async def test_interrupted_sweep_keeps_untried_rows(db, booted_now):
    first, second, third = _snowflake(60), _snowflake(120), _snowflake(180)
    for mid in (first, second, third):
        await _persistence.add_ephemeral_msg(5, mid)
    bot = _FakeBot({second: asyncio.CancelledError()})

    with pytest.raises(asyncio.CancelledError):
        await EventsCog(bot)._sweep_ephemeral_msgs()

    assert await _rows(db) == sorted([second, third])
