"""Property supply: the release pool, the 5am release pass and the per-player
cap (src/property_supply.py)."""
import pytest

import src.economy as _economy
import src.state as _state
from src.cogs.assets_cog import AssetsCog
from src.economy import add_balance
from src.persistence import init_db_state
from src.properties import (
    PROPERTIES, PROPERTIES_BY_ID, PROPERTY_BASE_MAX_OWNED, PROPERTY_RELEASE_ORDER,
    PROPERTY_SECURITY_UPGRADES, PROPERTY_UPGRADES, TIER_ORIGINALS,
    copy_id, find_property, is_released, max_owned, register_copy, released_properties,
)
from src.property_supply import (
    RELEASE_THRESHOLDS, cap_for, copy_threshold, deeds_due, development_index,
    economy_net_worth, run_property_release, wealth_progress,
)
from tests.test_properties import _ctx, _give_property, _set_level

pytestmark = pytest.mark.asyncio

SHIPPED_IDS = [p["id"] for p in PROPERTIES if not p.get("pool")]


def _day(monkeypatch, day: str):
    monkeypatch.setattr(_economy, "_ct_today", lambda: day)


async def _todays_economy():
    """14 deeds and about 8.64M of net worth: D ≈ 0.377, the deploy-day figure."""
    for pid in SHIPPED_IDS[:14]:
        await _give_property(1, pid)
    await _economy._ensure_user(2)
    _state.economy["users"]["2"]["balance"] = 8_300_000


# ── the pool ─────────────────────────────────────────────────────────────────

async def test_pool_deeds_are_hidden_until_released():
    pool = [p for p in PROPERTIES if p.get("pool")]
    assert len(pool) == 42 and len(SHIPPED_IDS) == 36
    assert {p["id"] for p in released_properties()} == set(SHIPPED_IDS)
    assert find_property("Moon Base") is None
    _state.released_properties.add("moon_base")
    assert find_property("Moon Base")["id"] == "moon_base"


async def test_an_owned_pool_deed_never_vanishes():
    await _give_property(1, "zoo")
    assert is_released("zoo")


async def test_bare_buy_of_an_unreleased_deed_is_unknown(db):
    _set_level(5, 50)
    await add_balance(5, 5_000_000)
    ctx = _ctx(5)
    await AssetsCog.assets_buy.callback(AssetsCog(bot=None), ctx, name="Moon Base")
    assert "moon_base" not in _state.property_owners
    assert "Unknown Property" in ctx.sent_embeds[-1].title


async def test_thresholds_are_spread_across_tiers():
    for tier, ids in PROPERTY_RELEASE_ORDER.items():
        assert len(RELEASE_THRESHOLDS[tier]) == len(ids)
    points = [th for ths in RELEASE_THRESHOLDS.values() for th in ths]
    assert len(points) == len(set(points)), "no two deeds release at the same index"


# ── the index and the cap ────────────────────────────────────────────────────

async def test_deploy_day_index_admits_two_one_one_none_none():
    due = deeds_due(0.377)
    by_tier = {t: sum(1 for pid in due if pid in ids) for t, ids in PROPERTY_RELEASE_ORDER.items()}
    assert by_tier == {1: 2, 2: 1, 3: 1, 4: 0, 5: 0}
    assert due[:2] == ["ice_cream_truck", "print_shop"]


async def test_index_weights_ownership_three_to_one():
    assert development_index(40, 30_000_000) == pytest.approx(1.0)
    assert development_index(40, 3_000_000) == pytest.approx(0.75)
    assert development_index(0, 30_000_000) == pytest.approx(0.25)
    assert wealth_progress(1_000_000) == 0.0


async def test_cap_steps_every_two_and_a_half_times():
    assert cap_for(2_000_000) == PROPERTY_BASE_MAX_OWNED
    assert cap_for(8_600_000) == 6
    assert cap_for(18_000_000) == 6
    assert cap_for(19_000_000) == 7
    assert cap_for(48_000_000) == 8


async def test_net_worth_counts_wallets_savings_deeds_and_artifacts():
    await _economy._ensure_user(3)
    _state.economy["users"]["3"]["balance"] = 1_000
    _state.economy["users"]["3"]["savings"] = [{"amount": 2_000, "deposited_at": 2_000_000_000.0}]
    await _give_property(3, "lemonade_stand")
    _state.user_artifacts.setdefault(3, {})["slots_blank_remover"] = 1
    assert economy_net_worth(now=2_000_000_000.0) == 1_000 + 2_000 + 10_000 + 15_000


# ── the release pass ─────────────────────────────────────────────────────────

async def test_first_pass_only_stamps_the_day(db, monkeypatch):
    await _todays_economy()
    _day(monkeypatch, "2026-11-05")
    assert await run_property_release() == ([], PROPERTY_BASE_MAX_OWNED)
    assert _state.released_properties == set()
    assert _state.economy["last_property_release"] == "2026-11-05"


async def test_reset_releases_due_deeds_and_raises_the_cap_once(db, monkeypatch):
    await _todays_economy()
    _state.economy["last_property_release"] = "2026-11-05"
    _day(monkeypatch, "2026-11-06")
    new, cap = await run_property_release()
    assert set(new) == {"ice_cream_truck", "print_shop", "karaoke_bar", "zoo"}
    assert cap == 6 and max_owned() == 6
    assert await run_property_release() == ([], 6)

    await init_db_state()
    assert _state.released_properties == set(new)
    assert _state.economy["property_cap"] == 6
    assert _state.economy["last_property_release"] == "2026-11-06"


async def test_a_shrinking_economy_takes_nothing_back(db, monkeypatch):
    await _todays_economy()
    _state.economy["last_property_release"] = "2026-11-05"
    _day(monkeypatch, "2026-11-06")
    new, _ = await run_property_release()
    _state.economy["users"]["2"]["balance"] = 0
    _day(monkeypatch, "2026-11-07")
    assert await run_property_release() == ([], 6)
    assert set(new) <= _state.released_properties


async def test_a_raised_cap_lets_a_sixth_deed_in(db):
    uid = 9100
    _set_level(uid, 50)
    for pid in SHIPPED_IDS[:5]:
        await _give_property(uid, pid)
    await add_balance(uid, 100_000)
    _state.economy["property_cap"] = 6
    await AssetsCog.assets_buy.callback(AssetsCog(bot=None), _ctx(uid), name="Car Wash")
    assert _state.property_owners["car_wash"]["owner_id"] == uid


# ── !assets browse ───────────────────────────────────────────────────────────

async def test_browse_lists_only_what_is_for_sale(db):
    from src.persistence import save_property_owner
    _set_level(7, 50)
    await _give_property(7, "lemonade_stand")
    await _give_property(8, "car_wash")
    row = _state.property_owners["car_wash"]
    row.update(list_price=90_000, upgraded=1, custom_name="Suds City")
    await save_property_owner("car_wash", row)
    ctx = _ctx(7)
    await AssetsCog.assets_browse.callback(AssetsCog(bot=None), ctx, tier=1)
    text = ctx.sent_embeds[-1].description
    assert "Lemonade Stand" not in text and "Yours" not in text
    assert "Car Wash" in text and "90,000" in text
    assert "Suds City" not in text and "⭐" not in text
    assert "available" not in text


async def test_browse_marks_a_tier_with_nothing_for_sale_sold_out(db):
    _set_level(7, 50)
    for p in released_properties():
        if p["tier"] == 5:
            await _give_property(8, p["id"])
    ctx = _ctx(7)
    await AssetsCog.assets_browse.callback(AssetsCog(bot=None), ctx, tier=5)
    assert "*Sold out.*" in ctx.sent_embeds[-1].description


async def test_buying_an_owned_deed_says_it_is_already_owned(db):
    _set_level(7, 50)
    await _give_property(7, "lemonade_stand")
    await _give_property(8, "car_wash")
    await add_balance(7, 100_000)
    ctx = _ctx(7)
    await AssetsCog.assets_buy.callback(AssetsCog(bot=None), ctx, name="Lemonade Stand")
    assert ctx.sent_embeds[-1].title == "🔒 Already Owned"
    assert "You already own" in ctx.sent_embeds[-1].description
    await AssetsCog.assets_buy.callback(AssetsCog(bot=None), ctx, name="Car Wash")
    assert "already owned by someone else" in ctx.sent_embeds[-1].description
    assert _state.economy["users"]["7"]["balance"] == 100_000


# ── numbered copies ──────────────────────────────────────────────────────────

async def test_copies_follow_the_last_pool_deed_in_catalog_order():
    for tier, ids in PROPERTY_RELEASE_ORDER.items():
        last = RELEASE_THRESHOLDS[tier][-1]
        assert copy_threshold(tier, 0) > last
        assert [copy_threshold(tier, k) for k in range(30)] == sorted(copy_threshold(tier, k) for k in range(30))
        tier_copies = {copy_id(tier, k) for k in range(30)}
        assert not tier_copies & set(deeds_due(last))
    originals = TIER_ORIGINALS[5]
    assert copy_id(5, 0) == f"{originals[0]}__2"
    assert copy_id(5, len(originals)) == f"{originals[0]}__3"
    assert [p for p in deeds_due(copy_threshold(5, 1)) if "__" in p and PROPERTIES_BY_ID.get(p.split("__")[0], {}).get("tier") == 5] \
        == [copy_id(5, 0), copy_id(5, 1)]


async def test_a_copy_is_its_own_deed_with_the_originals_numbers():
    entry = register_copy("lemonade_stand__2")
    original = PROPERTIES_BY_ID["lemonade_stand"]
    assert entry["name"] == "Lemonade Stand #2"
    assert {k: entry[k] for k in ("emoji", "tier", "level", "cost")} == {k: original[k] for k in ("emoji", "tier", "level", "cost")}
    assert PROPERTY_UPGRADES["lemonade_stand__2"] == PROPERTY_UPGRADES["lemonade_stand"]
    register_copy("movie_theater__3")
    assert PROPERTY_SECURITY_UPGRADES["movie_theater__3"] == PROPERTY_SECURITY_UPGRADES["movie_theater"]
    assert register_copy("lemonade_stand__1") is None and register_copy("nope__2") is None
    assert not is_released("lemonade_stand__2")
    _state.released_properties.add("lemonade_stand__2")
    assert find_property("Lemonade Stand #2")["id"] == "lemonade_stand__2"
    assert find_property("Lemonade Stand")["id"] == "lemonade_stand"


async def test_the_reset_releases_copies_once_a_tier_is_exhausted(db, monkeypatch):
    _state.economy["last_property_release"] = "2026-11-05"
    _day(monkeypatch, "2026-11-06")
    target = copy_threshold(1, 1)
    monkeypatch.setattr("src.property_supply.development_index", lambda owned, worth: target)
    new, _ = await run_property_release()
    assert set(PROPERTY_RELEASE_ORDER[1]) <= set(new)
    assert copy_id(1, 0) in new and copy_id(1, 1) in new and copy_id(1, 2) not in new
    assert find_property(PROPERTIES_BY_ID[copy_id(1, 0)]["name"])["id"] == copy_id(1, 0)

    # A boot sees them in the table only; it registers them again.
    for pid in (copy_id(1, 0), copy_id(1, 1)):
        del PROPERTIES_BY_ID[pid]
    import src.persistence as _persistence  # the conftest wrapper re-arms the one-shot guard
    await _persistence.init_db_state()
    assert copy_id(1, 0) in PROPERTIES_BY_ID and is_released(copy_id(1, 1))


async def test_a_copy_can_be_bought_and_named_names_are_reserved(db):
    from src.persistence import save_property_owner
    register_copy("lemonade_stand__2")
    _state.released_properties.add("lemonade_stand__2")
    _set_level(7, 50)
    await add_balance(7, 100_000)
    await _give_property(7, "lemonade_stand")
    ctx = _ctx(7)
    await AssetsCog.assets_buy.callback(AssetsCog(bot=None), ctx, name="Lemonade Stand #2")
    assert _state.property_owners["lemonade_stand__2"]["owner_id"] == 7

    row = _state.property_owners["lemonade_stand"]
    await save_property_owner("lemonade_stand", row)
    await AssetsCog.assets_rename.callback(AssetsCog(bot=None), ctx, "Lemonade", "Stand", "Hot", "Dog", "Cart", "#4")
    assert "Name Taken" in ctx.sent_embeds[-1].title
