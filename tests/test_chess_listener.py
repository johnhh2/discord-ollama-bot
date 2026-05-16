"""ChessCog.on_message listener: bare-move shortcut in chess channels."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.state as _state
import src.cogs.ai_cog as _ai_cog
from src.games import chess_engine
from src.games.chess import ChessCog, _initial_pgn


_aio = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_state():
    yield
    _state.active_chess_games.clear()


@pytest.fixture(autouse=True)
def _stub_helpers(monkeypatch):
    """Same shape as the test_chess.py autouse stub but lighter — no DM
    state. Stubs _bump_board so the listener can apply moves without real I/O."""
    import src.games.chess as _chess_mod
    bump_calls = []
    async def _stub_bump(channel, game, embed, *, file=None):
        game["board_msg_id"] = (game.get("board_msg_id") or 0) + 1
        bump_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_chess_mod, "_bump_board", _stub_bump)

    async def _noop(_msg):
        return None
    monkeypatch.setattr(_chess_mod, "_delete_after", _noop)

    edit_calls = []
    async def _stub_edit(channel, game, embed, *, file=None):
        edit_calls.append((channel, game, embed, file))
    monkeypatch.setattr(_ai_cog, "_edit_board", _stub_edit)
    monkeypatch.setattr(_ai_cog, "_bump_chess_board", _stub_bump)
    return bump_calls


@pytest.fixture
def _allow_all_chess_channels(monkeypatch):
    """Default: no chess_channels configured (empty list) — listener treats
    every channel as allowed per the falsy check in on_message."""
    import src.games.chess as _chess_mod
    monkeypatch.setattr(_chess_mod, "get_guild_cfg", lambda _gid: {})


def _make_cog() -> ChessCog:
    fake_bot = SimpleNamespace(user=SimpleNamespace(id=999_000_001))
    return ChessCog(bot=fake_bot)


def _fake_msg(author_id: int, content: str, channel_id: int, *, is_bot: bool = False,
              guild_id: int = 42):
    """A minimal discord.Message-shaped object for the listener."""
    author = MagicMock()
    author.id = author_id
    author.bot = is_bot
    author.display_name = "TestPlayer"
    channel = MagicMock()
    channel.id = channel_id
    channel.send = AsyncMock()
    guild = MagicMock()
    guild.id = guild_id
    guild.get_member = MagicMock(return_value=None)
    msg = MagicMock()
    msg.author = author
    msg.content = content
    msg.channel = channel
    msg.guild = guild
    msg.delete = AsyncMock()
    return msg


def _seed_game(channel_id: int, white_id: int, black_id: int):
    _state.active_chess_games[channel_id] = {
        "fen": chess_engine.STARTING_FEN,
        "pgn": _initial_pgn("W", "B", None),
        "white_id": white_id,
        "black_id": black_id,
        "current_id": white_id,
        "amount": 0,
        "last_move": "",
        "board_msg_id": 1,
    }


@_aio
async def test_listener_applies_valid_bare_move(db, _stub_helpers, _allow_all_chess_channels):
    """Plain `e4` from the current player in a chess-game channel applies
    the move and deletes the trigger message."""
    cog = _make_cog()
    white_id, black_id = 3000, 3001
    _seed_game(2000, white_id, black_id)
    msg = _fake_msg(white_id, "e4", channel_id=2000)

    await cog.on_message(msg)

    # Move applied.
    g = _state.active_chess_games[2000]
    assert g["current_id"] == black_id
    assert " e4" in g["pgn"]
    # Trigger message deleted.
    msg.delete.assert_awaited_once()


@_aio
async def test_listener_ignores_invalid_text(db, _stub_helpers, _allow_all_chess_channels):
    """Random chat ('hello there') in a chess channel is left alone."""
    cog = _make_cog()
    white_id, black_id = 3002, 3003
    _seed_game(2001, white_id, black_id)
    msg = _fake_msg(white_id, "hello there", channel_id=2001)

    await cog.on_message(msg)

    # No state change.
    g = _state.active_chess_games[2001]
    assert g["current_id"] == white_id
    assert g["fen"] == chess_engine.STARTING_FEN
    # Message NOT deleted.
    msg.delete.assert_not_awaited()


@_aio
async def test_listener_ignores_wrong_turn(db, _stub_helpers, _allow_all_chess_channels):
    """Black plays 'e5' when it's white's turn — silently ignored."""
    cog = _make_cog()
    white_id, black_id = 3004, 3005
    _seed_game(2002, white_id, black_id)
    msg = _fake_msg(black_id, "e5", channel_id=2002)

    await cog.on_message(msg)

    g = _state.active_chess_games[2002]
    assert g["current_id"] == white_id  # still white
    assert g["fen"] == chess_engine.STARTING_FEN
    msg.delete.assert_not_awaited()


@_aio
async def test_listener_ignores_command_prefixed(db, _stub_helpers, _allow_all_chess_channels):
    """`!move e4` must NOT be caught by the listener — it's the command path."""
    cog = _make_cog()
    white_id, black_id = 3006, 3007
    _seed_game(2003, white_id, black_id)
    msg = _fake_msg(white_id, "!move e4", channel_id=2003)

    await cog.on_message(msg)

    g = _state.active_chess_games[2003]
    assert g["current_id"] == white_id  # unchanged
    msg.delete.assert_not_awaited()


@_aio
async def test_listener_ignores_bot_authors(db, _stub_helpers, _allow_all_chess_channels):
    """Messages from other bots (or this bot) don't trigger the listener."""
    cog = _make_cog()
    white_id, black_id = 3008, 3009
    _seed_game(2004, white_id, black_id)
    msg = _fake_msg(white_id, "e4", channel_id=2004, is_bot=True)

    await cog.on_message(msg)

    g = _state.active_chess_games[2004]
    assert g["current_id"] == white_id
    msg.delete.assert_not_awaited()


@_aio
async def test_listener_ignores_dm_no_guild(db, _stub_helpers, _allow_all_chess_channels):
    """DMs (no guild) are never chess channels."""
    cog = _make_cog()
    white_id, black_id = 3010, 3011
    _seed_game(2005, white_id, black_id)
    msg = _fake_msg(white_id, "e4", channel_id=2005)
    msg.guild = None

    await cog.on_message(msg)

    g = _state.active_chess_games[2005]
    assert g["current_id"] == white_id
    msg.delete.assert_not_awaited()


@_aio
async def test_listener_ignores_no_active_game(db, _stub_helpers, _allow_all_chess_channels):
    """Channel has no active chess game — text is left alone."""
    cog = _make_cog()
    msg = _fake_msg(3012, "e4", channel_id=2006)

    await cog.on_message(msg)

    msg.delete.assert_not_awaited()


@_aio
async def test_listener_respects_chess_channel_whitelist(db, _stub_helpers, monkeypatch):
    """When chess_channels is configured and the message channel isn't in it,
    the listener bails even with an active game."""
    import src.games.chess as _chess_mod
    # 9999 is the allowed channel; 2007 (where the message arrives) isn't.
    monkeypatch.setattr(_chess_mod, "get_guild_cfg", lambda _gid: {"chess_channels": [9999]})

    cog = _make_cog()
    white_id, black_id = 3013, 3014
    _seed_game(2007, white_id, black_id)
    msg = _fake_msg(white_id, "e4", channel_id=2007)

    await cog.on_message(msg)

    g = _state.active_chess_games[2007]
    assert g["current_id"] == white_id
    msg.delete.assert_not_awaited()


@_aio
async def test_listener_accepts_in_whitelisted_channel(db, _stub_helpers, monkeypatch):
    """When the message channel IS in chess_channels, the listener applies."""
    import src.games.chess as _chess_mod
    monkeypatch.setattr(_chess_mod, "get_guild_cfg", lambda _gid: {"chess_channels": [2008]})

    cog = _make_cog()
    white_id, black_id = 3015, 3016
    _seed_game(2008, white_id, black_id)
    msg = _fake_msg(white_id, "e4", channel_id=2008)

    await cog.on_message(msg)

    g = _state.active_chess_games[2008]
    assert g["current_id"] == black_id
    msg.delete.assert_awaited_once()


@_aio
async def test_listener_falls_back_to_game_channels(db, _stub_helpers, monkeypatch):
    """If chess_channels empty but game_channels configured, the listener
    uses game_channels — matches check_chess_channel's behavior."""
    import src.games.chess as _chess_mod
    monkeypatch.setattr(_chess_mod, "get_guild_cfg", lambda _gid: {"game_channels": [2009]})

    cog = _make_cog()
    white_id, black_id = 3017, 3018
    _seed_game(2009, white_id, black_id)
    msg = _fake_msg(white_id, "e4", channel_id=2009)

    await cog.on_message(msg)

    g = _state.active_chess_games[2009]
    assert g["current_id"] == black_id
    msg.delete.assert_awaited_once()


@_aio
async def test_listener_ignores_overly_long_content(db, _stub_helpers, _allow_all_chess_channels):
    """The length guard (10 chars) avoids parsing entire chat sentences."""
    cog = _make_cog()
    white_id, black_id = 3019, 3020
    _seed_game(2010, white_id, black_id)
    msg = _fake_msg(white_id, "e4 was a great move btw", channel_id=2010)

    await cog.on_message(msg)

    g = _state.active_chess_games[2010]
    assert g["current_id"] == white_id
    msg.delete.assert_not_awaited()
