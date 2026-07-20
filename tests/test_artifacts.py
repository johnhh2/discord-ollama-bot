"""Artifact shop tests: listing, purchase persistence, double-buy guard,
rollback on failed charge, the concurrent-buy race, and the slots-reel effect.
"""
import asyncio

import pytest

import src.state as _state
import src.persistence as _persistence
import src.cogs.shop_cog as _shop_cog
from src.artifacts import ARTIFACTS, get_slot_reel
from src.config import ARTIFACT_SLOTS_BLANK_COST, SLOT_REEL
from src.cogs.shop_cog import ShopCog
from src.economy import add_balance, get_balance

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio

BLANK_ART_ID = "slots_blank_remover"


async def _read_db_artifact(uid: int, artifact_id: str) -> int | None:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT quantity FROM user_artifacts WHERE user_id=? AND artifact_id=?",
                (uid, artifact_id),
            )
            row = await cur.fetchone()
    return row[0] if row else None


def _ctx(uid: int) -> FakeCtx:
    return FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))


def _set_level(uid: int, display_level: int, gid: int = 42):
    """Give the user a display level in the test guild (internal is 0-based)."""
    _state.leveling.setdefault(str(gid), {})[str(uid)] = {"level": display_level - 1}


async def _invoke(cog: ShopCog, ctx: FakeCtx, *args):
    await cog.shop_artifacts.callback(cog, ctx, *args)


# ── listing ───────────────────────────────────────────────────────────────────

async def test_artifacts_list_shows_effect_and_cost(db):
    cog = ShopCog(bot=None)
    ctx = _ctx(7001)
    await _invoke(cog, ctx)

    assert len(ctx.sent_embeds) == 1
    desc = ctx.sent_embeds[0].description
    assert "⬛" in desc
    assert f"{ARTIFACT_SLOTS_BLANK_COST:,}" in desc
    assert "!artifacts buy" in desc


async def test_artifacts_list_marks_owned(db):
    cog = ShopCog(bot=None)
    uid = 7002
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1}
    ctx = _ctx(uid)
    await _invoke(cog, ctx)

    assert "Owned" in ctx.sent_embeds[0].description


# ── buying ────────────────────────────────────────────────────────────────────

async def test_buy_artifact_deducts_and_persists(db):
    cog = ShopCog(bot=None)
    uid = 7003
    _set_level(uid, 5)
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST + 500)

    ctx = _ctx(uid)
    await _invoke(cog, ctx, "buy", "1")

    assert await get_balance(uid) == 500
    assert _state.user_artifacts[uid][BLANK_ART_ID] == 1
    assert await _read_db_artifact(uid, BLANK_ART_ID) == 1
    assert "Artifact Acquired" in ctx.sent_embeds[-1].title


async def test_buy_artifact_twice_blocked(db):
    cog = ShopCog(bot=None)
    uid = 7004
    _set_level(uid, 5)
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST * 2)

    await _invoke(cog, _ctx(uid), "buy", "1")
    ctx2 = _ctx(uid)
    await _invoke(cog, ctx2, "buy", "1")

    assert "Already Owned" in ctx2.sent_embeds[-1].title
    # Charged exactly once.
    assert await get_balance(uid) == ARTIFACT_SLOTS_BLANK_COST
    assert _state.user_artifacts[uid][BLANK_ART_ID] == 1


async def test_buy_insufficient_funds_rolls_back_claim(db):
    cog = ShopCog(bot=None)
    uid = 7005
    _set_level(uid, 5)
    await add_balance(uid, 100)

    ctx = _ctx(uid)
    await _invoke(cog, ctx, "buy", "1")

    assert "Insufficient Funds" in ctx.sent_embeds[-1].title
    assert await get_balance(uid) == 100
    # The synchronous claim was rolled back — user owns nothing.
    assert _state.user_artifacts.get(uid, {}).get(BLANK_ART_ID, 0) == 0
    assert await _read_db_artifact(uid, BLANK_ART_ID) is None


async def test_buy_invalid_index(db):
    cog = ShopCog(bot=None)
    uid = 7006
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST)

    ctx = _ctx(uid)
    await _invoke(cog, ctx, "buy", str(len(ARTIFACTS) + 1))

    assert "Invalid Artifact" in ctx.sent_embeds[-1].title
    assert await get_balance(uid) == ARTIFACT_SLOTS_BLANK_COST


async def test_concurrent_buys_charge_once(db, monkeypatch):
    """Two interleaved !artifacts buy invocations grant one artifact and one
    charge. The fake DB is synchronous, so force a real event-loop yield
    inside the charge window to expose the race (see CLAUDE.md)."""
    cog = ShopCog(bot=None)
    uid = 7007
    _set_level(uid, 5)
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST * 2)

    real_charge = _shop_cog.shop_charge

    async def _yielding_charge(ctx, uid_, cost, **kwargs):
        await asyncio.sleep(0)
        return await real_charge(ctx, uid_, cost, **kwargs)

    monkeypatch.setattr(_shop_cog, "shop_charge", _yielding_charge)

    await asyncio.gather(
        _invoke(cog, _ctx(uid), "buy", "1"),
        _invoke(cog, _ctx(uid), "buy", "1"),
    )

    assert _state.user_artifacts[uid][BLANK_ART_ID] == 1
    assert await get_balance(uid) == ARTIFACT_SLOTS_BLANK_COST
    assert await _read_db_artifact(uid, BLANK_ART_ID) == 1


async def test_buy_below_level_blocked(db):
    """Level 1 user can't buy the level-5 blank remover; no claim, no charge."""
    cog = ShopCog(bot=None)
    uid = 7008
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST)

    ctx = _ctx(uid)
    await _invoke(cog, ctx, "buy", "1")

    assert "Level Locked" in ctx.sent_embeds[-1].title
    assert "Level 5" in ctx.sent_embeds[-1].description
    assert await get_balance(uid) == ARTIFACT_SLOTS_BLANK_COST
    assert _state.user_artifacts.get(uid, {}).get(BLANK_ART_ID, 0) == 0


async def test_list_marks_level_locked(db):
    """Below-level artifacts are struck through with a 🔒 Lvl marker."""
    cog = ShopCog(bot=None)
    uid = 7009
    _set_level(uid, 10)
    ctx = _ctx(uid)
    await _invoke(cog, ctx)

    desc = ctx.sent_embeds[0].description
    # Level 10: slots (5) and chessthreats (10) open, bail_discount (15) locked.
    assert "🔒 **Lvl 15**" in desc
    assert "🔒 **Lvl 30**" in desc
    assert "🔒 **Lvl 5**" not in desc
    assert "🔒 **Lvl 10**" not in desc


# ── effect helpers for the other artifacts ────────────────────────────────────

async def test_scratchoff_cap_with_artifact():
    from src.artifacts import scratchoff_daily_cap
    from src.gambling.scratchoff import scratchoff_attempts_remaining

    uid = 8003
    assert scratchoff_daily_cap(uid) == 3
    _state.user_artifacts[uid] = {"extra_scratchoff": 1}
    assert scratchoff_daily_cap(uid) == 4

    # A user who exhausted the base 3 still has one ticket left.
    user = {"scratch_date": "2026-07-20", "scratch_used": 3}
    assert scratchoff_attempts_remaining(user, "2026-07-20", scratchoff_daily_cap(uid)) == 1


async def test_bail_discount():
    from src.artifacts import bail_cost

    uid = 8004
    assert bail_cost(uid, 20_000) == 20_000
    _state.user_artifacts[uid] = {"bail_discount": 1}
    assert bail_cost(uid, 20_000) == 10_000
    # Odd base costs round in the payer's favor (floor of the discount).
    assert bail_cost(uid, 10_001) == 5_001


async def test_steal_success_boost():
    from src.artifacts import steal_success_chance

    uid = 8005
    assert steal_success_chance(uid, 0.10) == pytest.approx(0.10)
    _state.user_artifacts[uid] = {"steal_boost": 1}
    assert steal_success_chance(uid, 0.10) == pytest.approx(0.125)
    # Never exceeds certainty.
    assert steal_success_chance(uid, 0.9) == pytest.approx(1.0)


async def test_crime_catch_reduction():
    from src.artifacts import crime_catch_chance

    uid = 8006
    assert crime_catch_chance(uid, 0.5) == pytest.approx(0.5)
    _state.user_artifacts[uid] = {"crime_catch_reducer": 1}
    assert crime_catch_chance(uid, 0.5) == pytest.approx(0.4)   # mug
    assert crime_catch_chance(uid, 0.25) == pytest.approx(0.2)  # steal tier 1


# ── chessthreats unlock ───────────────────────────────────────────────────────

async def test_chessthreats_locked_without_artifact():
    from src.games.chess import ChessCog

    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=8007), command_name="chessthreats")
    await cog.cmd_chessthreats.callback(cog, ctx)

    assert "Locked" in ctx.sent_embeds[-1].title
    assert "!artifacts" in ctx.sent_embeds[-1].description


async def test_chessthreats_unlocked_with_artifact():
    from src.games.chess import ChessCog

    uid = 8008
    _state.user_artifacts[uid] = {"chessthreats_unlock": 1}
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid), command_name="chessthreats")
    await cog.cmd_chessthreats.callback(cog, ctx)

    # Past the artifact gate — fails on "no active game", not on the lock.
    assert "No Game" in ctx.sent_embeds[-1].title


async def test_chessthreats_admin_bypasses_artifact():
    from src.games.chess import ChessCog

    uid = 8009
    _state.bot_admins.add(uid)
    cog = ChessCog(bot=None)
    ctx = FakeCtx(author=FakeMember(uid=uid), command_name="chessthreats")
    await cog.cmd_chessthreats.callback(cog, ctx)

    assert "No Game" in ctx.sent_embeds[-1].title


# ── slots reel effect ─────────────────────────────────────────────────────────

async def test_slot_reel_without_artifact_is_unchanged():
    reel = get_slot_reel(8001)
    assert reel == list(SLOT_REEL)
    assert reel.count("⬛") == 4


async def test_slot_reel_with_artifact_drops_one_blank():
    uid = 8002
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1}
    reel = get_slot_reel(uid)
    assert reel.count("⬛") == SLOT_REEL.count("⬛") - 1
    assert len(reel) == len(SLOT_REEL) - 1
    # Only blanks were removed — every other symbol count is untouched.
    for sym in ("🍒", "🍋", "🔔", "🎰", "7️⃣"):
        assert reel.count(sym) == SLOT_REEL.count(sym)
