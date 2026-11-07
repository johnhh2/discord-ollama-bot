"""Personal and property premiums scale with exposure above each tier's
minimum, and a claims surcharge follows recent refunds (economy.insurance_premium)."""
import datetime
import math
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
from src.config import (
    INSURANCE_CLAIMS_DAYS, INSURANCE_SURCHARGE_PCT,
    SHOP_INSURANCE_TIERS, SHOP_PROPERTY_INSURANCE_TIERS, SHOP_SOCIAL_INSURANCE_TIERS,
)
from src.economy import (
    _ct_today, add_balance, get_balance, insurance_premium, insurance_pricing_text,
    insurance_refund, insurance_surcharge, insurance_switch_cost, renew_insurance_subs,
)
from src.properties import PROPERTIES_BY_ID, property_value
from tests.test_properties import _give_property

pytestmark = pytest.mark.asyncio

UID = 9_301


def _insure(uid: int, tier: str, kind: str = "personal", secs: float = 3600) -> None:
    policies, subs = _economy.insurance_policies(kind)
    policies[uid] = {"expires_at": int(time.time() + secs), "protected_from": [], "tier": tier}
    subs[uid] = tier


async def test_small_wallets_pay_the_tier_minimum(db):
    await add_balance(UID, 50_000)
    for tier, info in SHOP_INSURANCE_TIERS.items():
        assert insurance_premium(UID, tier) == info["cost"]


async def test_premium_scales_with_wallet_plus_savings_up_to_the_cap(db):
    await add_balance(UID, 1_000_000)
    _state.economy["users"][str(UID)]["savings"] = [{"amount": 1_000_000, "deposited_at": time.time()}]
    assert _economy.insurance_exposure(UID) == 2_000_000
    assert insurance_premium(UID, "basic") == 4_000       # capped at 1M
    assert insurance_premium(UID, "standard") == 8_000    # capped at ~1.33M
    assert insurance_premium(UID, "premium") == 20_000


async def test_property_premium_scales_with_deed_value_without_a_cap(db):
    pids = sorted(PROPERTIES_BY_ID, key=lambda p: -PROPERTIES_BY_ID[p]["cost"])[:3]
    for pid in pids:
        await _give_property(UID, pid)
    value = sum(property_value(pid) for pid in pids)
    assert _economy.insurance_exposure(UID, "property") == value
    for tier, info in SHOP_PROPERTY_INSURANCE_TIERS.items():
        assert insurance_premium(UID, tier, "property") == max(info["cost"], math.ceil(value * info["rate_bp"] / 10_000))


async def test_social_stays_flat(db):
    await add_balance(UID, 5_000_000)
    assert insurance_premium(UID, "social", "social") == SHOP_SOCIAL_INSURANCE_TIERS["social"]["cost"]
    assert insurance_pricing_text(UID, "social") == ""


async def test_a_refund_adds_a_week_long_surcharge(db):
    await add_balance(UID, 100)
    _insure(UID, "premium")
    refund = await insurance_refund(UID, 70_000)
    expected = math.ceil(refund * INSURANCE_SURCHARGE_PCT / 100 / INSURANCE_CLAIMS_DAYS)
    assert insurance_surcharge(UID) == expected
    assert insurance_premium(UID, "premium") == SHOP_INSURANCE_TIERS["premium"]["cost"] + expected
    assert insurance_surcharge(UID, "property") == 0


async def test_claims_older_than_the_window_drop_off(db):
    await add_balance(UID, 100)
    today = datetime.date.fromisoformat(_ct_today())
    old = (today - datetime.timedelta(days=INSURANCE_CLAIMS_DAYS)).isoformat()
    edge = (today - datetime.timedelta(days=INSURANCE_CLAIMS_DAYS - 1)).isoformat()
    _state.economy["users"][str(UID)]["insurance_claims"] = {"personal": {old: 70_000, edge: 14_000}}
    assert insurance_surcharge(UID) == math.ceil(14_000 * INSURANCE_SURCHARGE_PCT / 100 / INSURANCE_CLAIMS_DAYS)
    assert old not in _state.economy["users"][str(UID)]["insurance_claims"]["personal"]


async def test_claims_survive_a_reboot(db):
    await add_balance(UID, 100)
    _insure(UID, "basic")
    await insurance_refund(UID, 10_000)
    surcharge = insurance_surcharge(UID)
    _state.economy["users"].clear()
    await _persistence.init_db_state()
    assert insurance_surcharge(UID) == surcharge > 0


async def test_the_sweep_charges_the_scaled_premium(db):
    await add_balance(UID, 2_000_000)
    _insure(UID, "premium")
    charged, lapsed = await renew_insurance_subs(UID)
    assert (charged, lapsed) == (20_000, 0)
    assert await get_balance(UID) == 2_000_000 - 20_000


async def test_switch_cost_uses_the_scaled_difference(db):
    await add_balance(UID, 2_000_000)
    _insure(UID, "basic", secs=86_400)
    # 20,000 - 4,000 a day on just under a day of coverage.
    assert 15_990 < insurance_switch_cost(UID, "premium") <= 16_000


async def test_the_prompt_explains_the_pricing(db):
    await add_balance(UID, 2_000_000)
    text = insurance_pricing_text(UID)
    assert "wallet + savings" in text and "2,000,000" in text and "Premium 1%" in text
    assert "deeds' value" in insurance_pricing_text(UID, "property")
