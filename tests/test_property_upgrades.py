"""The two property upgrade types (migration 0087): revenue, as before, and
security, priced in PROPERTY_SECURITY_UPGRADES and unsold until property
theft exists. Covers the catalog, the value math, the type word and choice
in `!assets upgrade`, and the `secured` flag's round trip."""
import pytest

import src.state as _state
import src.persistence as _persistence
import src.properties as _properties
import src.cogs.assets_cog as _assets_cog
from src.economy import add_balance, get_balance
from src.properties import (
    PROPERTIES, PROPERTY_UPGRADES, PROPERTY_SECURITY_UPGRADES, property_value, bank_buyback_offer,
)
from src.cogs.assets_cog import AssetsCog
from tests.test_properties import _ctx, _give_property, _set_level

PID = "movie_theater"  # tier 3: the lowest tier with a security upgrade
LOW = "tattoo_parlor"  # tier 2: none


@pytest.fixture
def security_live(monkeypatch):
    monkeypatch.setattr(_properties, "SECURITY_UPGRADES_LIVE", True)


async def _read_db_secured(pid: str):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT secured FROM property_owners WHERE property_id=?", (pid,))
            row = await cur.fetchone()
    return row[0] if row else None


# ── the catalog ──────────────────────────────────────────────────────────────

def test_only_tier_three_and_up_have_security():
    assert all((p["id"] in PROPERTY_SECURITY_UPGRADES) == (p["tier"] >= 3) for p in PROPERTIES)


def test_every_security_price_is_a_quarter_of_the_rolled_range():
    for p in PROPERTIES:
        if p["tier"] < 3:
            continue
        price = PROPERTY_SECURITY_UPGRADES[p["id"]]
        assert p["cost"] * 0.75 / 4 - 500 <= price <= p["cost"] * 1.25 / 4 + 500, p["id"]


def test_shipped_security_prices_are_never_rerolled():
    assert PROPERTY_SECURITY_UPGRADES["movie_theater"] == 33_000
    assert PROPERTY_SECURITY_UPGRADES["space_port"] == 536_000
    assert PROPERTY_SECURITY_UPGRADES["moon_base"] == 843_000


def test_security_folds_into_value_and_the_bank_buyback():
    row = {"owner_id": 1, "upgraded": True, "secured": True}
    cost = next(p["cost"] for p in PROPERTIES if p["id"] == PID)
    assert property_value(PID, row) == cost + PROPERTY_UPGRADES[PID][1] + PROPERTY_SECURITY_UPGRADES[PID]
    unsecured = {"owner_id": 1, "upgraded": True, "secured": False}
    assert bank_buyback_offer(PID, row) - bank_buyback_offer(PID, unsecured) in (
        PROPERTY_SECURITY_UPGRADES[PID] * 75 // 100, PROPERTY_SECURITY_UPGRADES[PID] * 75 // 100 + 1,
    )


# ── !assets upgrade while security is off ────────────────────────────────────

async def test_security_is_refused_while_property_theft_is_off(db):
    uid = 9201
    await _give_property(uid, PID)
    await add_balance(uid, 1_000_000)
    ctx = _ctx(uid)

    await AssetsCog.assets_upgrade.callback(AssetsCog(bot=None), ctx, name="security Movie Theater")

    assert await get_balance(uid) == 1_000_000
    assert not _state.property_owners[PID].get("secured")
    assert ctx.sent_embeds[-1].title == "🛡️ Security Upgrades"


async def test_the_revenue_word_buys_the_revenue_upgrade(db):
    uid = 9202
    _set_level(uid, 50)
    await _give_property(uid, PID)
    await add_balance(uid, PROPERTY_UPGRADES[PID][1])

    await AssetsCog.assets_upgrade.callback(AssetsCog(bot=None), _ctx(uid), name="Movie Theater revenue")

    assert _state.property_owners[PID]["upgraded"] is True
    assert await get_balance(uid) == 0


# ── !assets upgrade with security on ─────────────────────────────────────────

async def test_buying_security_charges_and_persists(db, security_live):
    uid = 9211
    await _give_property(uid, PID)
    await add_balance(uid, PROPERTY_SECURITY_UPGRADES[PID] + 5)

    await AssetsCog.assets_upgrade.callback(AssetsCog(bot=None), _ctx(uid), name="security Movie Theater")

    assert _state.property_owners[PID]["secured"] is True
    assert _state.property_owners[PID]["upgraded"] is False
    assert await get_balance(uid) == 5
    assert await _read_db_secured(PID) == 1


async def test_a_deed_lacking_both_asks_which(db, security_live, monkeypatch):
    uid = 9212
    await _give_property(uid, PID)
    await add_balance(uid, PROPERTY_SECURITY_UPGRADES[PID])
    seen = {}

    async def _choose(ctx, *, choices, **kwargs):
        seen["values"] = [c["value"] for c in choices]
        seen["description"] = kwargs["description"]
        return "security"
    monkeypatch.setattr(_assets_cog, "confirm_choice", _choose)

    await AssetsCog.assets_upgrade.callback(AssetsCog(bot=None), _ctx(uid), name="Movie Theater")

    assert seen["values"] == ["revenue", "security"]
    assert "investigators" in seen["description"] and "no property insurance" in seen["description"]
    assert _state.property_owners[PID]["secured"] is True


async def test_a_deed_lacking_one_goes_straight_to_it(db, security_live, monkeypatch):
    uid = 9213
    await _give_property(uid, PID)
    _state.property_owners[PID]["upgraded"] = True
    await add_balance(uid, PROPERTY_SECURITY_UPGRADES[PID])

    async def _no_choice(*a, **k):
        raise AssertionError("no choice to make")
    monkeypatch.setattr(_assets_cog, "confirm_choice", _no_choice)

    await AssetsCog.assets_upgrade.callback(AssetsCog(bot=None), _ctx(uid), name="Movie Theater")

    assert _state.property_owners[PID]["secured"] is True


async def test_the_listing_shows_both_types(db, security_live):
    uid = 9214
    await _give_property(uid, PID)
    ctx = _ctx(uid)

    await AssetsCog.assets_upgrade.callback(AssetsCog(bot=None), ctx, name=None)

    desc = ctx.sent_embeds[-1].description
    assert PROPERTY_UPGRADES[PID][0] in desc and "Security" in desc
    assert "from tier 3 a security upgrade" in desc


# ── persistence ──────────────────────────────────────────────────────────────

async def test_the_secured_flag_survives_a_reload(db):
    uid = 9221
    await _give_property(uid, PID)
    _state.property_owners[PID]["secured"] = True
    await _persistence.save_property_owner(PID, _state.property_owners[PID])

    _state.property_owners.clear()
    await _persistence.init_db_state()

    assert _state.property_owners[PID]["secured"] is True


# ── tiers 1 and 2 ────────────────────────────────────────────────────────────

async def test_a_tier_two_deed_refuses_security(db, security_live):
    uid = 9231
    await _give_property(uid, LOW)
    await add_balance(uid, 1_000_000)
    ctx = _ctx(uid)

    await AssetsCog.assets_upgrade.callback(AssetsCog(bot=None), ctx, name="security Tattoo Parlor")

    assert await get_balance(uid) == 1_000_000
    assert ctx.sent_embeds[-1].title == "🛡️ No Security Upgrade"


async def test_a_tier_two_deed_goes_straight_to_revenue(db, security_live, monkeypatch):
    uid = 9232
    _set_level(uid, 50)
    await _give_property(uid, LOW)
    await add_balance(uid, PROPERTY_UPGRADES[LOW][1])

    async def _no_choice(*a, **k):
        raise AssertionError("a tier 2 deed has only one upgrade")
    monkeypatch.setattr(_assets_cog, "confirm_choice", _no_choice)

    await AssetsCog.assets_upgrade.callback(AssetsCog(bot=None), _ctx(uid), name="Tattoo Parlor")

    assert _state.property_owners[LOW]["upgraded"] is True
