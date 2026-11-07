"""Artifact shop tests: listing, purchase persistence, double-buy guard,
rollback on failed charge, the concurrent-buy race, selling back at half
price, the buy / sell dropdowns, and the slots-reel effect.
"""
import asyncio
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.cogs.shop_cog as _shop_cog
from src.artifacts import ARTIFACTS, RETIRED_ARTIFACTS, get_slot_reel
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


async def _read_db_acquired_at(uid: int, artifact_id: str) -> float | None:
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT acquired_at FROM user_artifacts WHERE user_id=? AND artifact_id=?",
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


def _picks(result):
    async def _stub(ctx, **kwargs):
        return result
    return _stub


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
    before = time.time()
    await _invoke(cog, ctx, "buy", "1")

    assert await get_balance(uid) == 500
    assert _state.user_artifacts[uid][BLANK_ART_ID] == 1
    assert await _read_db_artifact(uid, BLANK_ART_ID) == 1
    assert "Artifact Acquired" in ctx.sent_embeds[-1].title
    # The purchase instant is kept, in memory and in the row.
    acquired = _state.user_artifact_acquired_at[uid][BLANK_ART_ID]
    assert before <= acquired <= time.time()
    assert await _read_db_acquired_at(uid, BLANK_ART_ID) == pytest.approx(acquired)


async def test_acquired_at_survives_reload(db):
    """init_db_state reads the acquisition time back; a pre-0067 row (NULL)
    simply has none."""
    from src.persistence.artifacts import save_user_artifact
    from src.persistence.init import init_db_state
    await save_user_artifact(7010, BLANK_ART_ID, 1, 1_800_000_000.0)
    await save_user_artifact(7011, BLANK_ART_ID, 1)            # legacy row
    # A quantity re-save never moves the acquisition time.
    await save_user_artifact(7010, BLANK_ART_ID, 1, 1_900_000_000.0)

    await init_db_state()
    assert _state.user_artifacts[7010][BLANK_ART_ID] == 1
    assert _state.user_artifact_acquired_at[7010][BLANK_ART_ID] == 1_800_000_000.0
    assert _state.user_artifacts[7011][BLANK_ART_ID] == 1
    assert BLANK_ART_ID not in _state.user_artifact_acquired_at.get(7011, {})


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
    # The synchronous claim was rolled back — user owns nothing, and the
    # acquisition stamp went with it.
    assert _state.user_artifacts.get(uid, {}).get(BLANK_ART_ID, 0) == 0
    assert BLANK_ART_ID not in _state.user_artifact_acquired_at.get(uid, {})
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


async def test_buy_gates_on_global_level(db):
    """A level earned in another guild unlocks the artifact here."""
    cog = ShopCog(bot=None)
    uid = 7010
    _set_level(uid, 5, gid=99)
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST)

    await _invoke(cog, _ctx(uid), "buy", "1")

    assert _state.user_artifacts[uid][BLANK_ART_ID] == 1
    assert await get_balance(uid) == 0


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


# ── selling ───────────────────────────────────────────────────────────────────

async def test_sell_artifact_pays_half_and_drops_the_row(db):
    cog = ShopCog(bot=None)
    uid = 7020
    _set_level(uid, 5)
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST)
    await _invoke(cog, _ctx(uid), "buy", "1")
    assert await get_balance(uid) == 0

    ctx = _ctx(uid)
    await _invoke(cog, ctx, "sell", "1")

    assert "Artifact Sold" in ctx.sent_embeds[-1].title
    assert await get_balance(uid) == ARTIFACT_SLOTS_BLANK_COST // 2
    assert BLANK_ART_ID not in _state.user_artifacts.get(uid, {})
    assert BLANK_ART_ID not in _state.user_artifact_acquired_at.get(uid, {})
    assert await _read_db_artifact(uid, BLANK_ART_ID) is None
    ctx2 = _ctx(uid)
    await _invoke(cog, ctx2)
    assert "Owned" not in ctx2.sent_embeds[0].description


async def test_listing_quotes_the_sell_price_of_owned(db):
    cog = ShopCog(bot=None)
    uid = 7026
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1}
    ctx = _ctx(uid)
    await _invoke(cog, ctx)
    assert f"sells for {ARTIFACT_SLOTS_BLANK_COST // 2:,}" in ctx.sent_embeds[0].description


async def test_sell_unowned_artifact_refused(db):
    cog = ShopCog(bot=None)
    uid = 7021
    ctx = _ctx(uid)
    await _invoke(cog, ctx, "sell", "1")
    assert "Not Owned" in ctx.sent_embeds[-1].title
    assert await get_balance(uid) == 0


async def test_sell_cancelled_keeps_the_artifact(db, monkeypatch):
    cog = ShopCog(bot=None)
    uid = 7022
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1}

    async def _decline(*args, **kwargs):
        return False
    monkeypatch.setattr(_shop_cog, "confirm_prompt", _decline)
    await _invoke(cog, _ctx(uid), "sell", "1")

    assert _state.user_artifacts[uid][BLANK_ART_ID] == 1
    assert await get_balance(uid) == 0


async def test_concurrent_sells_pay_once(db, monkeypatch):
    """Two interleaved sells of one copy pay out once: the claim is
    synchronous after the confirm, so the second sees the copy gone."""
    cog = ShopCog(bot=None)
    uid = 7023
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1}

    async def _yielding_confirm(*args, **kwargs):
        await asyncio.sleep(0)
        return True
    monkeypatch.setattr(_shop_cog, "confirm_prompt", _yielding_confirm)

    await asyncio.gather(
        _invoke(cog, _ctx(uid), "sell", "1"),
        _invoke(cog, _ctx(uid), "sell", "1"),
    )
    assert await get_balance(uid) == ARTIFACT_SLOTS_BLANK_COST // 2
    assert BLANK_ART_ID not in _state.user_artifacts.get(uid, {})


async def test_godmode_sell_pays_nothing(db):
    cog = ShopCog(bot=None)
    uid = 7024
    _state.godmode_users.add(uid)
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1}
    ctx = _ctx(uid)
    await _invoke(cog, ctx, "sell", "1")
    assert "Artifact Sold" in ctx.sent_embeds[-1].title
    assert await get_balance(uid) == 0


async def test_selling_the_savings_artifact_keeps_interest_already_earned(db, monkeypatch):
    """The boost is read from ownership, so the sale first banks each
    deposit's grown value — a month of boosted interest can't vanish."""
    from src.economy import get_savings_value, add_savings
    from src.config import ARTIFACT_SAVINGS_BOOST_COST

    cog = ShopCog(bot=None)
    uid = 7025
    monkeypatch.setattr(_shop_cog, "pick_from_list", _picks(["savings_rate_boost"]))
    month_ago = time.time() - 30 * 86400
    _state.user_artifacts[uid] = {"savings_rate_boost": 1}
    _state.user_artifact_acquired_at[uid] = {"savings_rate_boost": month_ago}
    await add_balance(uid, 100_000)
    assert await add_savings(uid, 100_000)
    _state.economy["users"][str(uid)]["savings"][0]["deposited_at"] = month_ago
    before = await get_savings_value(uid)
    assert before > 100_000

    await _invoke(cog, _ctx(uid), "sell")

    assert "savings_rate_boost" not in _state.user_artifacts.get(uid, {})
    assert await get_savings_value(uid) == pytest.approx(before, rel=1e-6)
    assert await get_balance(uid) == ARTIFACT_SAVINGS_BOOST_COST // 2


# ── the dropdowns ─────────────────────────────────────────────────────────────

def _menu_pick(monkeypatch, name: str, result):
    calls = []

    async def _stub(ctx, **kwargs):
        calls.append(kwargs)
        return result
    monkeypatch.setattr(_shop_cog, name, _stub)
    return calls


async def test_listing_offers_buy_and_sell_menus(db, monkeypatch):
    """Bare `!artifacts` follows the listing with a buy menu of what the
    user's level unlocks and doesn't own, and a sell menu of what they own."""
    cog = ShopCog(bot=None)
    uid = 7030
    _set_level(uid, 10)
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1}
    calls = _menu_pick(monkeypatch, "pick_from_menus", None)

    await _invoke(cog, _ctx(uid))

    menus = {key: [v for _, v in options] for key, _, options, _ in calls[0]["menus"]}
    # Level 10 unlocks slots (5, owned) and chessthreats (10); the rest are locked.
    assert menus["buy"] == ["chessthreats_unlock"]
    assert menus["sell"] == [BLANK_ART_ID]


async def test_listing_menu_pick_buys(db, monkeypatch):
    cog = ShopCog(bot=None)
    uid = 7031
    _set_level(uid, 5)
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST)
    _menu_pick(monkeypatch, "pick_from_menus", ("buy", [BLANK_ART_ID]))

    ctx = _ctx(uid)
    await _invoke(cog, ctx)

    assert "Artifact Acquired" in ctx.sent_embeds[-1].title
    assert _state.user_artifacts[uid][BLANK_ART_ID] == 1
    assert await get_balance(uid) == 0


async def test_listing_menu_pick_sells(db, monkeypatch):
    cog = ShopCog(bot=None)
    uid = 7032
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1}
    _menu_pick(monkeypatch, "pick_from_menus", ("sell", [BLANK_ART_ID]))

    ctx = _ctx(uid)
    await _invoke(cog, ctx)

    assert "Artifact Sold" in ctx.sent_embeds[-1].title
    assert await get_balance(uid) == ARTIFACT_SLOTS_BLANK_COST // 2


async def test_bare_buy_opens_a_dropdown_of_what_is_buyable(db, monkeypatch):
    cog = ShopCog(bot=None)
    uid = 7033
    _set_level(uid, 5)
    await add_balance(uid, ARTIFACT_SLOTS_BLANK_COST)
    calls = _menu_pick(monkeypatch, "pick_from_list", [BLANK_ART_ID])

    ctx = _ctx(uid)
    await _invoke(cog, ctx, "buy")

    assert [v for _, v in calls[0]["options"]] == [BLANK_ART_ID]
    assert "Artifact Acquired" in ctx.sent_embeds[-1].title
    assert await get_balance(uid) == 0


async def test_bare_buy_with_nothing_buyable_says_so(db, monkeypatch):
    cog = ShopCog(bot=None)
    uid = 7034          # level 1: everything locked
    calls = _menu_pick(monkeypatch, "pick_from_list", None)
    ctx = _ctx(uid)
    await _invoke(cog, ctx, "buy")
    assert calls == []
    assert "Nothing to buy" in ctx.sent_embeds[-1].description


async def test_bare_sell_opens_a_dropdown_of_owned(db, monkeypatch):
    cog = ShopCog(bot=None)
    uid = 7035
    _state.user_artifacts[uid] = {BLANK_ART_ID: 1, "bail_discount": 1}
    calls = _menu_pick(monkeypatch, "pick_from_list", ["bail_discount"])

    ctx = _ctx(uid)
    await _invoke(cog, ctx, "sell")

    assert sorted(v for _, v in calls[0]["options"]) == sorted([BLANK_ART_ID, "bail_discount"])
    assert "Artifact Sold" in ctx.sent_embeds[-1].title
    assert _state.user_artifacts[uid] == {BLANK_ART_ID: 1}


async def test_bare_sell_with_nothing_owned_says_so(db, monkeypatch):
    cog = ShopCog(bot=None)
    calls = _menu_pick(monkeypatch, "pick_from_list", None)
    ctx = _ctx(7036)
    await _invoke(cog, ctx, "sell")
    assert calls == []
    assert "don't own any" in ctx.sent_embeds[-1].description


async def test_dismissed_dropdown_changes_nothing(db):
    """conftest dismisses the prompts: a bare buy / sell with no pick neither
    charges nor pays."""
    cog = ShopCog(bot=None)
    uid = 7037
    _set_level(uid, 5)
    await add_balance(uid, 100)
    _state.user_artifacts[uid] = {"bail_discount": 1}
    await _invoke(cog, _ctx(uid), "buy")
    await _invoke(cog, _ctx(uid), "sell")
    assert await get_balance(uid) == 100
    assert _state.user_artifacts[uid] == {"bail_discount": 1}


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


async def test_streak_scratchoffs_unlock_at_25_day_streak():
    from src.artifacts import scratchoff_daily_cap
    from src.economy import _ct_today

    uid = 8010
    today = _ct_today()
    # A monster streak without the artifact grants nothing.
    _state.command_streak[str(uid)] = {"date": today, "count": 120}
    assert scratchoff_daily_cap(uid) == 3

    _state.user_artifacts[uid] = {"streak_scratchoffs": 1}
    assert scratchoff_daily_cap(uid) == 4  # flat +1, doesn't scale past 25

    # Below 25 days the artifact grants nothing yet.
    _state.command_streak[str(uid)] = {"date": today, "count": 24}
    assert scratchoff_daily_cap(uid) == 3
    _state.command_streak[str(uid)] = {"date": today, "count": 25}
    assert scratchoff_daily_cap(uid) == 4

    # A dead streak (last bump before yesterday) counts as 0.
    _state.command_streak[str(uid)] = {"date": "2020-01-01", "count": 200}
    assert scratchoff_daily_cap(uid) == 3

    # Stacks with the flat 4th-ticket artifact.
    _state.command_streak[str(uid)] = {"date": today, "count": 30}
    _state.user_artifacts[uid]["extra_scratchoff"] = 1
    assert scratchoff_daily_cap(uid) == 5


async def test_catalog_levels_costs_and_order():
    """The shipped ladder: levels never decrease down the list (the !artifacts
    numbering reads as a progression) and the tuned prices hold."""
    from src.config import (
        ARTIFACT_CRIME_CATCH_COST, ARTIFACT_STREAK_SCRATCH_COST,
        ARTIFACT_PROPERTY_CAP_COST, ARTIFACT_SAVINGS_BOOST_COST,
        ARTIFACT_UPGRADE_DISCOUNT_COST, ARTIFACT_HEIST_PARTNER_COST,
        ARTIFACT_PROPERTY_BOOST_COST,
    )
    by_id = {a["id"]: a for a in ARTIFACTS}
    levels = [a["level"] for a in ARTIFACTS]
    assert levels == sorted(levels)
    assert len(by_id) == len(ARTIFACTS)

    assert (by_id["crime_catch_reducer"]["level"], ARTIFACT_CRIME_CATCH_COST) == (30, 200_000)
    assert (by_id["streak_scratchoffs"]["level"], ARTIFACT_STREAK_SCRATCH_COST) == (35, 300_000)
    assert (by_id["silas_heist_partner"]["level"], ARTIFACT_HEIST_PARTNER_COST) == (40, 400_000)
    assert by_id["silas_heist_partner"]["heist_npc_partner"] == 1
    assert "Silas" in by_id["silas_heist_partner"]["effect"]
    assert (by_id["property_upgrade_discount"]["level"], ARTIFACT_UPGRADE_DISCOUNT_COST) == (45, 500_000)
    assert by_id["property_upgrade_discount"]["property_upgrade_discount_pct"] == 20
    assert [a["id"] for a in ARTIFACTS][6:] == [
        "streak_scratchoffs", "silas_heist_partner", "property_upgrade_discount",
    ]

    # Retired: off sale, but owners paid these prices and sell back at half.
    retired = {a["id"]: a for a in RETIRED_ARTIFACTS}
    assert set(retired) == {"property_cap_deed", "savings_rate_boost", "property_mogul"}
    assert not set(retired) & set(by_id)
    assert retired["property_cap_deed"]["cost"] == ARTIFACT_PROPERTY_CAP_COST == 150_000
    assert retired["property_cap_deed"]["property_accrual_cap_bonus"] == 10_000
    assert retired["savings_rate_boost"]["cost"] == ARTIFACT_SAVINGS_BOOST_COST == 200_000
    assert retired["property_mogul"]["cost"] == ARTIFACT_PROPERTY_BOOST_COST == 500_000
    assert retired["property_mogul"]["property_revenue_pct_cap"] == 25
    for art in ARTIFACTS + RETIRED_ARTIFACTS:
        assert art["cost"] > 0 and art["max"] >= 1


async def test_retired_artifacts_are_off_sale_but_owners_keep_and_sell(db, monkeypatch):
    """A retired artifact is neither listed nor offered for purchase, even at
    level 50; an owner still has its effect, sees it, and can sell it back."""
    from src.artifacts import property_accrual_cap_bonus

    cog = ShopCog(bot=None)
    buyer, owner = 7040, 7041
    _set_level(buyer, 50)
    calls = _menu_pick(monkeypatch, "pick_from_menus", None)
    ctx = _ctx(buyer)
    await _invoke(cog, ctx)
    menus = {key: [v for _, v in options] for key, _, options, _ in calls[0]["menus"]}
    retired_ids = {a["id"] for a in RETIRED_ARTIFACTS}
    assert not retired_ids & set(menus["buy"])
    assert "10,000" not in ctx.sent_embeds[-1].description
    assert "Retired" not in ctx.sent_embeds[-1].description

    _state.user_artifacts[owner] = {"property_cap_deed": 1}
    assert property_accrual_cap_bonus(owner) == 10_000
    calls.clear()
    ctx = _ctx(owner)
    await _invoke(cog, ctx)
    menus = {key: [v for _, v in options] for key, _, options, _ in calls[0]["menus"]}
    assert menus["sell"] == ["property_cap_deed"]
    assert "Retired" in ctx.sent_embeds[-1].description

    monkeypatch.setattr(_shop_cog, "pick_from_list", _picks(["property_cap_deed"]))
    ctx = _ctx(owner)
    await _invoke(cog, ctx, "sell")
    assert "Artifact Sold" in ctx.sent_embeds[-1].title
    assert await get_balance(owner) == 75_000
    assert property_accrual_cap_bonus(owner) == 0


async def test_savings_artifact_copy_matches_rates():
    """The savings artifact's blurb quotes both rates from the constants the
    math uses, so a rate change can't leave stale copy behind."""
    from src.economy import SAVINGS_DAILY_PCT, ARTIFACT_SAVINGS_DAILY_PCT
    art = next(a for a in RETIRED_ARTIFACTS if a["id"] == "savings_rate_boost")
    assert SAVINGS_DAILY_PCT == "0.60%"
    assert ARTIFACT_SAVINGS_DAILY_PCT == "0.80%"
    assert ARTIFACT_SAVINGS_DAILY_PCT in art["effect"]
    assert SAVINGS_DAILY_PCT in art["effect"]


async def test_savings_boost_since_needs_owned_row_with_timestamp():
    from src.artifacts import savings_boost_since, artifact_acquired_at

    uid = 8011
    assert savings_boost_since(uid) is None
    # Owned but no acquisition time (a hand-inserted / pre-0067 row): no
    # boost — never a retroactive one.
    _state.user_artifacts[uid] = {"savings_rate_boost": 1}
    assert artifact_acquired_at(uid, "savings_rate_boost") is None
    assert savings_boost_since(uid) is None
    _state.user_artifact_acquired_at[uid] = {"savings_rate_boost": 1_800_000_000.0}
    assert savings_boost_since(uid) == 1_800_000_000.0
    # A timestamp without ownership means nothing.
    _state.user_artifacts[uid] = {}
    assert artifact_acquired_at(uid, "savings_rate_boost") is None
    assert savings_boost_since(uid) is None


async def test_property_upgrade_cost_discount():
    from src.artifacts import property_upgrade_cost

    uid = 8012
    assert property_upgrade_cost(uid, 50_000) == 50_000
    _state.user_artifacts[uid] = {"property_upgrade_discount": 1}
    assert property_upgrade_cost(uid, 50_000) == 40_000
    # Odd costs round in the buyer's favor (floor of the discount).
    assert property_upgrade_cost(uid, 10_001) == 8_001


async def test_has_heist_partner():
    from src.artifacts import has_heist_partner

    uid = 8013
    assert has_heist_partner(uid) is False
    _state.user_artifacts[uid] = {"silas_heist_partner": 1}
    assert has_heist_partner(uid) is True


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
