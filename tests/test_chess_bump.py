"""Unit tests for _bump_board (chess board delete-and-resend on each move).

Lives in its own file so the autouse _bump_board-stub fixture in
test_chess.py doesn't shadow the real function under test.
"""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.games.chess import _bump_board


_aio = pytest.mark.asyncio


@_aio
async def test_bump_board_deletes_old_and_sends_new():
    """Fetch + delete prior message, send a fresh one, write the new id."""
    deleted = []
    old_msg = MagicMock()
    old_msg.delete = AsyncMock(side_effect=lambda: deleted.append(True))

    sent_msg = MagicMock()
    sent_msg.id = 9999

    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=old_msg)
    channel.send = AsyncMock(return_value=sent_msg)

    game = {"board_msg_id": 111}
    await _bump_board(channel, game, MagicMock())

    assert deleted == [True]
    assert channel.send.await_count == 1
    assert game["board_msg_id"] == 9999


@_aio
async def test_bump_board_tolerates_already_deleted_old_message():
    """If the prior message is gone (user deleted it, channel pruned),
    swallow NotFound and still send the new one."""
    sent_msg = MagicMock()
    sent_msg.id = 8888

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
    channel.send = AsyncMock(return_value=sent_msg)

    game = {"board_msg_id": 222}
    await _bump_board(channel, game, MagicMock())

    assert channel.send.await_count == 1
    assert game["board_msg_id"] == 8888


@_aio
async def test_bump_board_first_send_no_prior_id():
    """game['board_msg_id'] is None on the very first board send — skip the
    delete step entirely."""
    sent_msg = MagicMock()
    sent_msg.id = 7777

    channel = MagicMock()
    channel.fetch_message = AsyncMock()  # should not be called
    channel.send = AsyncMock(return_value=sent_msg)

    game = {"board_msg_id": None}
    await _bump_board(channel, game, MagicMock())

    channel.fetch_message.assert_not_awaited()
    assert game["board_msg_id"] == 7777


@_aio
async def test_bump_board_forwards_file_to_send():
    """Passing file= should reach channel.send."""
    sent_msg = MagicMock()
    sent_msg.id = 6666

    channel = MagicMock()
    channel.fetch_message = AsyncMock()
    channel.send = AsyncMock(return_value=sent_msg)

    fake_file = MagicMock(spec=discord.File)
    game = {"board_msg_id": None}
    await _bump_board(channel, game, MagicMock(), file=fake_file)

    _, send_kwargs = channel.send.call_args
    assert send_kwargs.get("file") is fake_file
