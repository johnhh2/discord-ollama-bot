"""Chess cosmetics end-to-end: equipping owned piece sets / board colors via
`!chess <name>` / `!chess inventory`, persistence, and the render plumbing
that feeds equipped cosmetics into render_board_png.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
import src.games.chess as _chess_mod
from src.chess_shop import equipped_cosmetics
from src.games.chess import ChessCog, _render_file_for_game

from tests.fakes.discord import FakeChannel, FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio


def _ctx(uid: int) -> FakeCtx:
    return FakeCtx(author=FakeMember(uid=uid))


def _unlock(uid: int, *item_ids: str):
    _state.chess_unlocks.setdefault(uid, set()).update(item_ids)


async def _read_db_equipped(uid: int):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT piece_set, board_theme FROM chess_equipped WHERE user_id=?",
                (uid,),
            )
            return await cur.fetchone()


# ── !chess <name>: equip ──────────────────────────────────────────────────────

async def test_equip_owned_piece_set(db):
    uid = 7001
    _unlock(uid, "pieces:rhosgfx")
    cog = ChessCog(bot=None)
    ctx = _ctx(uid)

    await cog.cmd_chess.callback(cog, ctx, "wood")

    assert equipped_cosmetics(uid) == ("rhosgfx", "default")
    assert ctx.sent_embeds[-1].title == "♟️ Equipped"
    assert await _read_db_equipped(uid) == ("rhosgfx", "default")


async def test_equip_owned_board_color(db):
    uid = 7002
    _unlock(uid, "board:blue")
    cog = ChessCog(bot=None)
    ctx = _ctx(uid)

    await cog.cmd_chess.callback(cog, ctx, "blue")

    assert equipped_cosmetics(uid) == ("cburnett", "blue")
    assert await _read_db_equipped(uid) == ("cburnett", "blue")


async def test_equip_unowned_rejected(db):
    uid = 7003
    cog = ChessCog(bot=None)
    ctx = _ctx(uid)

    await cog.cmd_chess.callback(cog, ctx, "pixel")

    assert equipped_cosmetics(uid) == ("cburnett", "default")
    assert "Not Unlocked" in ctx.sent_embeds[-1].title
    assert "chess shop" in ctx.sent_embeds[-1].description


async def test_equip_default_switches_back(db):
    uid = 7004
    _unlock(uid, "pieces:rhosgfx")
    cog = ChessCog(bot=None)

    await cog.cmd_chess.callback(cog, _ctx(uid), "wood")
    assert equipped_cosmetics(uid)[0] == "rhosgfx"

    await cog.cmd_chess.callback(cog, _ctx(uid), "classic")
    assert equipped_cosmetics(uid) == ("cburnett", "default")
    assert await _read_db_equipped(uid) == ("cburnett", "default")


async def test_equip_default_when_nothing_equipped_is_noop(db):
    uid = 7005
    cog = ChessCog(bot=None)
    ctx = _ctx(uid)

    await cog.cmd_chess.callback(cog, ctx, "classic")

    assert "Already Equipped" in ctx.sent_embeds[-1].title
    assert await _read_db_equipped(uid) is None


async def test_equip_hyphenated_name(db):
    uid = 7006
    _unlock(uid, "pieces:kiwen-suwi")
    cog = ChessCog(bot=None)
    ctx = _ctx(uid)

    await cog.cmd_chess.callback(cog, ctx, "kiwen-suwi")

    assert equipped_cosmetics(uid)[0] == "kiwen-suwi"


async def test_bare_number_is_not_an_equip(db, monkeypatch):
    """`!chess 5` must stay an (invalid) game arg, never equip item #5."""
    uid = 7007
    _unlock(uid, "pieces:fantasy")

    async def _blocked(ctx):
        return True  # pretend wrong channel so the game path exits quietly
    monkeypatch.setattr(_chess_mod, "check_chess_channel", _blocked)

    cog = ChessCog(bot=None)
    ctx = _ctx(uid)
    await cog.cmd_chess.callback(cog, ctx, "5")

    assert equipped_cosmetics(uid) == ("cburnett", "default")
    assert not any(e.title == "♟️ Equipped" for e in ctx.sent_embeds)


async def test_equipped_reloads_from_db(db):
    uid = 7008
    _unlock(uid, "pieces:totoy", "board:charcoal")
    cog = ChessCog(bot=None)
    await cog.cmd_chess.callback(cog, _ctx(uid), "geometric")
    await cog.cmd_chess.callback(cog, _ctx(uid), "charcoal")

    _state.chess_equipped = {}
    await _persistence.init_db_state()

    assert equipped_cosmetics(uid) == ("totoy", "charcoal")


# ── !chess inventory ──────────────────────────────────────────────────────────

async def test_inventory_lists_owned_equipped_and_locked(db):
    uid = 7101
    _unlock(uid, "pieces:rhosgfx", "board:blue")
    _state.chess_equipped[uid] = {"pieces": "rhosgfx", "board": "default"}
    cog = ChessCog(bot=None)
    ctx = _ctx(uid)

    await cog.cmd_chess.callback(cog, ctx, "inventory")

    e = ctx.sent_embeds[-1]
    assert "Chess Inventory" in e.title
    assert "✦ **Wood** — equipped" in e.description
    assert "✦ **Default** — equipped" in e.description
    assert "✅ Blue" in e.description
    assert "🔒 Pixel — 20,000" in e.description
    assert "!chess <name>" in e.description


async def test_help_mentions_cosmetics_commands(db):
    cog = ChessCog(bot=None)
    ctx = _ctx(7102)

    await cog.cmd_chess.callback(cog, ctx, "help")

    desc = ctx.sent_embeds[-1].description
    assert "!chess inventory" in desc
    assert "!chess shop" in desc


# ── Render plumbing ───────────────────────────────────────────────────────────

def _capture_render(monkeypatch) -> dict:
    captured: dict = {}

    def _fake_render(board, **kwargs):
        captured.update(kwargs)
        return b"\x89PNG\r\n\x1a\nfake"
    monkeypatch.setattr(_chess_mod.chess_render, "render_board_png", _fake_render)
    return captured


def _game(white_id: int, black_id: int) -> dict:
    import src.games.chess_engine as chess_engine
    return {
        "fen": chess_engine.STARTING_FEN, "pgn": "",
        "white_id": white_id, "black_id": black_id,
        "current_id": white_id, "amount": 0, "last_move": "",
    }


async def test_render_uses_pov_players_cosmetics(db, monkeypatch):
    white, black = 7201, 7202
    _state.chess_equipped[white] = {"pieces": "pixel", "board": "ice"}
    _state.chess_equipped[black] = {"pieces": "merida", "board": "green"}
    captured = _capture_render(monkeypatch)

    _render_file_for_game(_game(white, black), orientation_for_uid=black)
    assert captured["piece_set"] == "merida"
    assert captured["theme"] == "green"

    _render_file_for_game(_game(white, black), orientation_for_uid=white)
    assert captured["piece_set"] == "pixel"
    assert captured["theme"] == "ice"


async def test_render_defaults_to_white_when_no_pov(db, monkeypatch):
    white, black = 7203, 7204
    _state.chess_equipped[white] = {"pieces": "celtic", "board": "brown"}
    captured = _capture_render(monkeypatch)

    _render_file_for_game(_game(white, black))

    assert captured["piece_set"] == "celtic"
    assert captured["theme"] == "brown"


async def test_render_explicit_cosmetics_uid_overrides_pov(db, monkeypatch):
    white, black = 7205, 7206
    _state.chess_equipped[white] = {"pieces": "spatial", "board": "coffee"}
    captured = _capture_render(monkeypatch)

    _render_file_for_game(
        _game(white, black), orientation_for_uid=black, cosmetics_uid=white,
    )

    assert captured["piece_set"] == "spatial"
    assert captured["theme"] == "coffee"


async def test_render_unequipped_user_gets_defaults(db, monkeypatch):
    captured = _capture_render(monkeypatch)

    _render_file_for_game(_game(7207, 7208), orientation_for_uid=7207)

    assert captured["piece_set"] == "cburnett"
    assert captured["theme"] == "default"


async def test_bot_turn_render_uses_human_cosmetics(db, monkeypatch):
    """When the next player is the bot, the board keeps the human's
    cosmetics instead of flip-flopping to defaults each bot turn."""
    human, bot_id = 7209, 999_000
    _state.chess_equipped[human] = {"pieces": "fantasy", "board": "purple"}
    seen: dict = {}

    def _fake_rffg(game, *, orientation_for_uid=None, cosmetics_uid=None):
        seen["cosmetics_uid"] = cosmetics_uid
        return None
    monkeypatch.setattr(_chess_mod, "_render_file_for_game", _fake_rffg)

    async def _noop_bump(*a, **k):
        return None
    monkeypatch.setattr(_chess_mod, "_bump_board", _noop_bump)
    # chess.py binds save_chess_game at import time — patch the module-local
    # name, not src.persistence (see CLAUDE.md on module-local imports).
    monkeypatch.setattr(_chess_mod, "save_chess_game", AsyncMock())

    cog = ChessCog(bot=SimpleNamespace(user=SimpleNamespace(id=bot_id)))
    game = _game(human, bot_id)
    game["board_msg_id"] = None
    channel = FakeChannel(guild=FakeGuild())
    import chess as _chess
    await cog._render_and_bump_after_move(
        channel, 800, game, _chess.Board(), opponent_id=bot_id,
    )

    assert seen["cosmetics_uid"] == human
