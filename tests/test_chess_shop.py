"""Chess shop (!chess shop): Elo actually moves, unlocks persist, and the
purchase is race-safe.

The shop lives under !chess shop (ChessCog); drive the dispatcher via
.callback with fake ctx objects. confirm_prompt is auto-accepted by the
conftest stub; tests exercising decline/drift override it per-test.
"""
import asyncio

import pytest

import src.state as _state
import src.persistence as _persistence
import src.games.chess as _chess_mod
from src.chess_shop import (
    BOARD_ITEMS, CHESS_SHOP_ITEMS, PIECE_SET_ITEMS,
    find_chess_item, has_chess_unlock,
)
from src.games.chess import ChessCog
from src.games.bot_chess_rewards import (
    chess_elo_balance, refund_chess_elo, spend_chess_elo,
)
from src.games.chess_render import PIECE_SETS_DIR

from tests.fakes.discord import FakeCtx, FakeMember

pytestmark = pytest.mark.asyncio


def _give_elo(uid: int, total: int, spent: int = 0, max_elo: int = 1500):
    _state.chess_user_stats[str(uid)] = {
        "max_elo_defeated": max_elo,
        "total_elo_defeated": total,
        "bonus_bins": set(),
        "elo_spent": spent,
    }


async def _read_db_unlocks(uid: int) -> set[str]:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT item_id FROM chess_unlocks WHERE user_id=?", (uid,)
            )
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def _read_db_elo_spent(uid: int) -> int | None:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT elo_spent FROM chess_user_stats WHERE user_id=?", (uid,)
            )
            row = await cur.fetchone()
    return row[0] if row else None


# ── Catalog ───────────────────────────────────────────────────────────────────

async def test_catalog_costs_match_spec():
    costs = {it["name"]: it["cost"] for it in CHESS_SHOP_ITEMS}
    assert costs["Classic"] == 0          # cburnett, default
    assert costs["Fantasy"] == 5_000
    assert costs["Celtic"] == 5_000
    assert costs["Wood"] == 10_000        # rhosgfx
    assert costs["Spatial"] == 10_000
    assert costs["Merida"] == 15_000
    assert costs["Kiwen-Suwi"] == 20_000
    assert costs["Geometric"] == 20_000   # totoy
    assert costs["Pixel"] == 20_000
    # Boards: 10k except purple/ice/charcoal/coffee at 20k.
    assert costs["Default"] == 0
    for name in ("Brown", "Blue", "Green"):
        assert costs[name] == 10_000, name
    for name in ("Purple", "Ice", "Charcoal", "Coffee"):
        assert costs[name] == 20_000, name


async def test_every_paid_piece_set_is_vendored():
    for it in PIECE_SET_ITEMS:
        if it["cost"] > 0:
            assert (PIECE_SETS_DIR / f"{it['key']}.json").is_file(), it["key"]


async def test_find_chess_item_by_number_name_and_key():
    assert find_chess_item("1")["name"] == "Classic"
    assert find_chess_item(str(len(CHESS_SHOP_ITEMS)))["name"] == "Charcoal"
    assert find_chess_item("wood")["key"] == "rhosgfx"
    assert find_chess_item("RHOSGFX")["name"] == "Wood"
    assert find_chess_item("geometric")["key"] == "totoy"
    assert find_chess_item("0") is None
    assert find_chess_item(str(len(CHESS_SHOP_ITEMS) + 1)) is None
    assert find_chess_item("nope") is None


async def test_default_items_owned_by_everyone():
    assert has_chess_unlock(42, "pieces:cburnett")
    assert has_chess_unlock(42, "board:default")
    assert not has_chess_unlock(42, "pieces:fantasy")


# ── Elo balance / spend ───────────────────────────────────────────────────────

async def test_elo_balance_spend_and_refund():
    uid = 6001
    assert chess_elo_balance(uid) == 0
    assert spend_chess_elo(uid, 1) is False

    _give_elo(uid, 12_000, spent=2_000)
    assert chess_elo_balance(uid) == 10_000
    assert spend_chess_elo(uid, 10_001) is False
    assert spend_chess_elo(uid, 10_000) is True
    assert chess_elo_balance(uid) == 0
    refund_chess_elo(uid, 10_000)
    assert chess_elo_balance(uid) == 10_000
    # Lifetime total untouched throughout.
    assert _state.chess_user_stats[str(uid)]["total_elo_defeated"] == 12_000


async def test_spend_rejects_negative():
    uid = 6002
    _give_elo(uid, 5_000)
    assert spend_chess_elo(uid, -5) is False
    assert chess_elo_balance(uid) == 5_000


# ── Listing ───────────────────────────────────────────────────────────────────

async def test_listing_shows_items_and_balance(db):
    uid = 6101
    _give_elo(uid, 45_300, spent=5_000)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop")

    assert len(ctx.sent_embeds) == 1
    e = ctx.sent_embeds[0]
    assert "Chess Shop" in e.title
    assert "40,300" in e.description          # spendable
    assert "45,300" in e.description          # lifetime
    for name in ("Wood", "Fantasy", "Geometric", "Brown", "Charcoal"):
        assert name in e.description
    assert "✅ default" in e.description       # Classic + Default board


# ── Purchases ─────────────────────────────────────────────────────────────────

async def test_buy_success_unlocks_and_spends(db):
    uid = 6201
    _give_elo(uid, 10_000)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "fantasy")

    assert has_chess_unlock(uid, "pieces:fantasy")
    assert chess_elo_balance(uid) == 5_000
    assert ctx.sent_embeds[-1].title == "♟️ Unlocked"
    # Persisted: unlock row + spent Elo both hit the DB.
    assert await _read_db_unlocks(uid) == {"pieces:fantasy"}
    assert await _read_db_elo_spent(uid) == 5_000


async def test_buy_by_number(db):
    uid = 6202
    _give_elo(uid, 25_000)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    # Item 2 in the combined list is Fantasy (1 is the Classic default).
    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "2")

    assert has_chess_unlock(uid, "pieces:fantasy")
    assert chess_elo_balance(uid) == 20_000


async def test_buy_board_color(db):
    uid = 6203
    _give_elo(uid, 20_000)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "charcoal")

    assert has_chess_unlock(uid, "board:charcoal")
    assert chess_elo_balance(uid) == 0
    assert await _read_db_unlocks(uid) == {"board:charcoal"}


async def test_buy_insufficient_elo_rejected(db):
    uid = 6204
    _give_elo(uid, 4_999)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "fantasy")

    assert not has_chess_unlock(uid, "pieces:fantasy")
    assert chess_elo_balance(uid) == 4_999
    assert "Not Enough Elo" in ctx.sent_embeds[-1].title
    assert await _read_db_unlocks(uid) == set()


async def test_buy_already_owned_rejected(db):
    uid = 6205
    _give_elo(uid, 50_000)
    _state.chess_unlocks[uid] = {"pieces:fantasy"}
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "fantasy")

    assert chess_elo_balance(uid) == 50_000   # nothing spent
    assert "Already Yours" in ctx.sent_embeds[-1].title


async def test_buy_default_item_rejected(db):
    uid = 6206
    _give_elo(uid, 50_000)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "classic")

    assert chess_elo_balance(uid) == 50_000
    assert "Already Yours" in ctx.sent_embeds[-1].title


async def test_buy_declined_confirm_no_charge(db, monkeypatch):
    uid = 6207
    _give_elo(uid, 10_000)

    async def _no_confirm(*a, **k):
        return False
    monkeypatch.setattr(_chess_mod, "confirm_prompt", _no_confirm)

    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))
    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "fantasy")

    assert not has_chess_unlock(uid, "pieces:fantasy")
    assert chess_elo_balance(uid) == 10_000


async def test_buy_godmode_is_free(db):
    uid = 6208
    _give_elo(uid, 0)
    _state.godmode_users.add(uid)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "pixel")

    assert has_chess_unlock(uid, "pieces:pixel")
    assert chess_elo_balance(uid) == 0


async def test_concurrent_buys_charge_once(db, monkeypatch):
    """Two concurrent buys of the same item: the confirm await is a real
    yield point, so both pass the pre-confirm checks — the post-confirm
    re-check + synchronous spend must still resolve to one unlock, one
    charge (see CLAUDE.md on per-user command races)."""
    uid = 6209
    _give_elo(uid, 20_000)

    async def _yielding_confirm(*a, **k):
        await asyncio.sleep(0)
        return True
    monkeypatch.setattr(_chess_mod, "confirm_prompt", _yielding_confirm)

    cog = ChessCog(bot=None)
    ctx1 = FakeCtx(author=FakeMember(uid=uid))
    ctx2 = FakeCtx(author=FakeMember(uid=uid))
    await asyncio.gather(
        cog.cmd_chess.callback(cog, ctx1, "shop", "buy", "merida"),
        cog.cmd_chess.callback(cog, ctx2, "shop", "buy", "merida"),
    )

    assert has_chess_unlock(uid, "pieces:merida")
    assert chess_elo_balance(uid) == 5_000     # charged exactly once
    assert await _read_db_elo_spent(uid) == 15_000
    titles = [e.title for e in ctx1.sent_embeds + ctx2.sent_embeds]
    assert titles.count("♟️ Unlocked") == 1


async def test_prestige_gate_marks_all_20k_items():
    for it in CHESS_SHOP_ITEMS:
        expected = 1_100 if it["cost"] >= 20_000 else 0
        assert it["req_max_elo"] == expected, it["name"]


async def test_buy_20k_item_requires_1100_bot_win(db):
    uid = 6301
    _give_elo(uid, 50_000, max_elo=1_000)   # rich, but never beat 1100+
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "pixel")

    assert not has_chess_unlock(uid, "pieces:pixel")
    assert chess_elo_balance(uid) == 50_000
    assert "Elo Locked" in ctx.sent_embeds[-1].title


async def test_buy_20k_item_at_exact_1100_allowed(db):
    uid = 6302
    _give_elo(uid, 50_000, max_elo=1_100)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "pixel")

    assert has_chess_unlock(uid, "pieces:pixel")
    assert chess_elo_balance(uid) == 30_000


async def test_cheaper_items_ignore_prestige_gate(db):
    uid = 6303
    _give_elo(uid, 50_000, max_elo=400)     # low ceiling, lots of grinding
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "merida")

    assert has_chess_unlock(uid, "pieces:merida")


async def test_listing_strikes_through_gated_items(db):
    uid = 6304
    _give_elo(uid, 50_000, max_elo=1_000)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))

    await cog.cmd_chess.callback(cog, ctx, "shop")

    desc = ctx.sent_embeds[0].description
    assert "Beat a 1,100+ Elo bot" in desc
    assert "~~" in desc
    # Ungated items are not struck through: Merida's line has no strikes.
    merida_line = next(ln for ln in desc.split("\n") if "Merida" in ln)
    assert "~~" not in merida_line


async def test_unlocks_reload_from_db(db):
    """chess_unlocks and elo_spent survive a state wipe + init_db_state."""
    uid = 6210
    _give_elo(uid, 10_000)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid))
    await cog.cmd_chess.callback(cog, ctx, "shop", "buy", "celtic")

    _state.chess_unlocks = {}
    _state.chess_user_stats = {}
    await _persistence.init_db_state()

    assert _state.chess_unlocks.get(uid) == {"pieces:celtic"}
    assert chess_elo_balance(uid) == 5_000


async def test_board_items_cover_all_paid_render_themes():
    """Every renderer theme is purchasable (or the free default) — adding a
    theme to BOARD_THEMES without a catalog entry should fail here."""
    from src.games.chess_render import BOARD_THEMES
    catalog_keys = {it["key"] for it in BOARD_ITEMS}
    assert catalog_keys == set(BOARD_THEMES)
