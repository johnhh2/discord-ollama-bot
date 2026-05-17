"""Unit tests for _bump_board (chess board delete-and-resend on each move).

Lives in its own file so the autouse _bump_board-stub fixture in
test_chess.py doesn't shadow the real function under test.

_bump_board sends TWO messages on each bump (embed first, then board image)
so the image renders BELOW the embed in Discord. Both message IDs are
tracked in the game dict: embed_msg_id + board_msg_id.
"""
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.games.chess import _bump_board


_aio = pytest.mark.asyncio


@_aio
async def test_bump_board_deletes_old_pair_and_sends_new_pair():
    """Fetch + delete prior embed AND image messages, then send a fresh
    embed-then-image pair. Both new ids land in the game dict."""
    deleted_ids: list[int] = []

    def _fake_fetch(msg_id):
        m = MagicMock()
        m.delete = AsyncMock(side_effect=lambda: deleted_ids.append(msg_id))
        return m

    embed_msg = MagicMock()
    embed_msg.id = 9001
    image_msg = MagicMock()
    image_msg.id = 9002

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=_fake_fetch)
    channel.send = AsyncMock(side_effect=[embed_msg, image_msg])

    fake_file = MagicMock(spec=discord.File)
    game = {"embed_msg_id": 111, "board_msg_id": 222}
    await _bump_board(channel, game, MagicMock(), file=fake_file)

    # Both prior messages deleted (image first per reverse-send order,
    # then embed).
    assert deleted_ids == [222, 111]
    # Two new sends: embed then file.
    assert channel.send.await_count == 2
    embed_call_kwargs = channel.send.call_args_list[0].kwargs
    image_call_kwargs = channel.send.call_args_list[1].kwargs
    assert "embed" in embed_call_kwargs and "file" not in embed_call_kwargs
    assert "file" in image_call_kwargs and "embed" not in image_call_kwargs
    # New ids tracked.
    assert game["embed_msg_id"] == 9001
    assert game["board_msg_id"] == 9002


@_aio
async def test_bump_board_tolerates_already_deleted_old_messages():
    """If either prior message is gone (user deleted it, channel pruned),
    swallow NotFound and still send the new pair."""
    embed_msg = MagicMock()
    embed_msg.id = 8001
    image_msg = MagicMock()
    image_msg.id = 8002

    channel = MagicMock()
    channel.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
    channel.send = AsyncMock(side_effect=[embed_msg, image_msg])

    fake_file = MagicMock(spec=discord.File)
    game = {"embed_msg_id": 111, "board_msg_id": 222}
    await _bump_board(channel, game, MagicMock(), file=fake_file)

    # Still attempted both fetches; both raised NotFound and were swallowed.
    assert channel.fetch_message.await_count == 2
    assert channel.send.await_count == 2
    assert game["embed_msg_id"] == 8001
    assert game["board_msg_id"] == 8002


@_aio
async def test_bump_board_first_send_no_prior_ids():
    """On the very first bump (no prior ids), skip the delete step."""
    embed_msg = MagicMock()
    embed_msg.id = 7001
    image_msg = MagicMock()
    image_msg.id = 7002

    channel = MagicMock()
    channel.fetch_message = AsyncMock()  # should not be called
    channel.send = AsyncMock(side_effect=[embed_msg, image_msg])

    fake_file = MagicMock(spec=discord.File)
    game = {"embed_msg_id": None, "board_msg_id": None}
    await _bump_board(channel, game, MagicMock(), file=fake_file)

    channel.fetch_message.assert_not_awaited()
    assert game["embed_msg_id"] == 7001
    assert game["board_msg_id"] == 7002


@_aio
async def test_bump_board_no_file_skips_image_message():
    """If render failed (file is None), only the embed is sent; board_msg_id
    is cleared so a future bump doesn't try to delete a nonexistent image."""
    embed_msg = MagicMock()
    embed_msg.id = 6001

    channel = MagicMock()
    channel.fetch_message = AsyncMock()
    channel.send = AsyncMock(return_value=embed_msg)

    game = {"embed_msg_id": None, "board_msg_id": None}
    await _bump_board(channel, game, MagicMock(), file=None)

    assert channel.send.await_count == 1
    assert game["embed_msg_id"] == 6001
    assert game["board_msg_id"] is None
