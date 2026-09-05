"""Insurance tiers (migration 0065): crime is refunded, not blocked.

Three tiers — basic 50% / standard 75% / premium 100% of a crime loss, each
with a per-gameplay-day refund cap — replace the old "insured users can't be
robbed" gate. These tests cover the economy helpers (refund math, the daily
cap, tier switching and its surcharge, sub renewal at the sub's tier), the
`!shop insurance` tier picker flows, and the 0065 backfill that lands every
pre-tier policy on basic.
"""
import math
import sqlite3
import time as _time
from pathlib import Path

import pytest

import src.state as _state
import src.persistence as _persistence
import src.cogs.shop_cog as _shop_cog
import src.migrations as _migrations
from tests.fakes.db import FakeCursor
from src.config import SHOP_INSURANCE_TIERS, SHOP_INSURANCE_DURATION_SECS
from src.economy import (
    add_balance, get_balance, insurance_refund, insurance_switch_cost,
    extend_insurance, set_insurance_tier, get_insurance_tier, renew_insurance_subs,
)

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild

pytestmark = pytest.mark.asyncio

DAY = SHOP_INSURANCE_DURATION_SECS
BASIC = SHOP_INSURANCE_TIERS["basic"]["cost"]
STANDARD = SHOP_INSURANCE_TIERS["standard"]["cost"]
PREMIUM = SHOP_INSURANCE_TIERS["premium"]["cost"]


def _insure(uid: int, tier: str, seconds: float = 3600) -> int:
    expiry = int(_time.time() + seconds)
    _state.insurance[uid] = {"expires_at": expiry, "protected_from": ["mock"], "tier": tier}
    return expiry


async def _db_tier(effect_type: str, uid: int):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT insurance_tier FROM shop_effects WHERE effect_type=? AND user_id=?",
                (effect_type, uid),
            )
            row = await cur.fetchone()
    return row[0] if row else None


# ── insurance_refund ─────────────────────────────────────────────────────────

async def test_refund_uninsured_and_expired_pay_nothing(db):
    uid = 9001
    await add_balance(uid, 100)
    assert await insurance_refund(uid, 10_000) == 0
    _insure(uid, "premium", seconds=-5)  # already expired
    assert await insurance_refund(uid, 10_000) == 0
    assert await get_balance(uid) == 100


async def test_refund_pays_tier_share_into_wallet_and_persists(db):
    uid = 9002
    await add_balance(uid, 100)
    _insure(uid, "basic")
    assert await insurance_refund(uid, 1001) == 500  # 50%, floored
    assert await get_balance(uid) == 600
    _state.economy["users"].clear()
    await _persistence.init_db_state()
    assert await get_balance(uid) == 600


async def test_refund_cap_is_per_incident_not_per_day(db):
    """Each robbery is capped on its own: a second oversized loss the same
    day is refunded up to the full cap again — nothing accumulates."""
    uid = 9003
    await add_balance(uid, 0)
    _insure(uid, "standard")  # 75%, 200k cap
    assert await insurance_refund(uid, 200_000) == 150_000
    assert await insurance_refund(uid, 1_000_000) == 200_000  # capped
    assert await insurance_refund(uid, 1_000_000) == 200_000  # capped again, no tally
    assert await get_balance(uid) == 550_000


async def test_refund_unknown_tier_falls_back_to_default(db):
    uid = 9004
    await add_balance(uid, 0)
    _insure(uid, "platinum")  # a tier later removed from the catalog
    assert await insurance_refund(uid, 1000) == 500


# ── tier bookkeeping ─────────────────────────────────────────────────────────

async def test_switch_cost_only_charges_upgrades_on_remaining_coverage():
    uid = 9010
    assert insurance_switch_cost(uid, "premium") == 0          # uncovered
    _insure(uid, "basic", seconds=10 * DAY)
    assert insurance_switch_cost(uid, "basic") == 0            # same tier
    up = insurance_switch_cost(uid, "premium")
    assert up == math.ceil((10 * DAY - 1) / DAY * (PREMIUM - BASIC)) or up == 10 * (PREMIUM - BASIC)
    _insure(uid, "premium", seconds=10 * DAY)
    assert insurance_switch_cost(uid, "basic") == 0            # downgrade is free


async def test_extend_insurance_tier_defaults_to_current_then_sub_then_basic():
    uid = 9011
    extend_insurance(uid, 1)
    assert _state.insurance[uid]["tier"] == "basic"
    assert "steal" not in _state.insurance[uid]["protected_from"]
    _state.insurance.pop(uid)
    _state.insurance_subs[uid] = "standard"
    extend_insurance(uid, 1)
    assert _state.insurance[uid]["tier"] == "standard"         # lapsed sub renews at its tier
    extend_insurance(uid, 1)
    assert _state.insurance[uid]["tier"] == "standard"         # active policy keeps its tier
    extend_insurance(uid, 1, tier="premium")
    assert _state.insurance[uid]["tier"] == "premium"
    assert get_insurance_tier(uid) == "premium"


async def test_set_insurance_tier_keeps_policy_and_sub_in_step():
    uid = 9012
    _insure(uid, "basic")
    _state.insurance_subs[uid] = "basic"
    set_insurance_tier(uid, "premium")
    assert _state.insurance[uid]["tier"] == "premium"
    assert _state.insurance_subs[uid] == "premium"


async def test_renew_charges_the_subscriptions_tier(db):
    uid = 9013
    await add_balance(uid, PREMIUM + 5)
    _state.insurance_subs[uid] = "premium"
    expiry = _insure(uid, "premium")
    charged, lapsed = await renew_insurance_subs(uid)
    assert (charged, lapsed) == (PREMIUM, 0)
    assert await get_balance(uid) == 5
    assert _state.insurance[uid]["expires_at"] == expiry + DAY
    assert _state.insurance[uid]["tier"] == "premium"


async def test_insurance_rows_round_trip_tier(db):
    _insure(9014, "standard")
    _state.insurance_subs[9015] = "premium"
    await _persistence.save_insurance()
    await _persistence.save_insurance_subs()
    assert await _db_tier("insurance", 9014) == "standard"
    assert await _db_tier("insurance_sub", 9015) == "premium"
    _state.insurance.clear()
    _state.insurance_subs.clear()
    await _persistence.init_db_state()
    assert _state.insurance[9014]["tier"] == "standard"
    assert _state.insurance_subs[9015] == "premium"


# ── !shop insurance picker flows ─────────────────────────────────────────────
# conftest auto-picks the highlighted (default) button: the tier named in
# the command, else the buyer's current tier, else basic.

def _ctx(uid: int) -> FakeCtx:
    return FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))


async def test_shop_insurance_picker_offers_all_three_tiers(db, monkeypatch):
    """Every purchase prompt lists all three tiers as buttons, with the
    named tier highlighted and each label carrying that tier's total."""
    from src.cogs.shop_cog import ShopCog
    seen = {}

    async def _capture(ctx, *, choices, **kwargs):
        seen["choices"] = choices
        return None  # cancel
    monkeypatch.setattr(_shop_cog, "confirm_choice", _capture)

    uid = 9020
    await add_balance(uid, 100_000)
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), _ctx(uid), "standard", "3")

    choices = seen["choices"]
    assert [c["value"] for c in choices] == ["basic", "standard", "premium"]
    assert [c.get("default", False) for c in choices] == [False, True, False]
    assert choices[2]["label"] == f"Premium — {3 * PREMIUM:,} 🪙"
    assert await get_balance(uid) == 100_000     # cancelled: nothing charged
    assert uid not in _state.insurance


async def test_shop_insurance_explicit_tier_buys_that_tier(db):
    from src.cogs.shop_cog import ShopCog
    uid = 9021
    await add_balance(uid, 2 * PREMIUM + 7)
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), _ctx(uid), "premium", "2")
    assert await get_balance(uid) == 7
    assert _state.insurance[uid]["tier"] == "premium"
    assert await _db_tier("insurance", uid) == "premium"


async def test_shop_insurance_number_only_keeps_current_tier(db):
    from src.cogs.shop_cog import ShopCog
    uid = 9022
    await add_balance(uid, 3 * STANDARD)
    expiry = _insure(uid, "standard")
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), _ctx(uid), "3")
    assert await get_balance(uid) == 0
    assert _state.insurance[uid]["tier"] == "standard"
    assert _state.insurance[uid]["expires_at"] == expiry + 3 * DAY


async def test_shop_insurance_upgrade_charges_difference_on_remaining_coverage(db):
    """Basic coverage with ~10 days left, buying 1 day of premium: pays the
    premium day plus the daily difference across the remaining 10 days, and
    the whole policy is premium afterwards."""
    from src.cogs.shop_cog import ShopCog
    uid = 9023
    expiry = _insure(uid, "basic", seconds=10 * DAY)
    surcharge = insurance_switch_cost(uid, "premium")
    assert 10 * (PREMIUM - BASIC) - (PREMIUM - BASIC) < surcharge <= 10 * (PREMIUM - BASIC)
    await add_balance(uid, PREMIUM + surcharge + 3)
    ctx = _ctx(uid)
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), ctx, "premium")
    assert await get_balance(uid) == 3
    assert _state.insurance[uid]["tier"] == "premium"
    assert _state.insurance[uid]["expires_at"] == expiry + DAY
    assert f"Includes **{surcharge:,} 🪙** to move your remaining coverage up" in ctx.sent_embeds[-1].description


async def test_shop_insurance_downgrade_is_free_and_not_refunded(db):
    from src.cogs.shop_cog import ShopCog
    uid = 9024
    expiry = _insure(uid, "premium", seconds=5 * DAY)
    await add_balance(uid, BASIC)
    ctx = _ctx(uid)
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), ctx, "basic")
    assert await get_balance(uid) == 0
    assert _state.insurance[uid]["tier"] == "basic"
    assert _state.insurance[uid]["expires_at"] == expiry + DAY
    assert "no refund on the difference" in ctx.sent_embeds[-1].description


async def test_shop_insurance_upgrade_unaffordable_rolls_back_tier_and_days(db):
    from src.cogs.shop_cog import ShopCog
    uid = 9025
    expiry = _insure(uid, "basic", seconds=10 * DAY)
    _state.insurance_subs[uid] = "basic"
    await add_balance(uid, PREMIUM)  # can't cover the surcharge
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), _ctx(uid), "premium")
    assert await get_balance(uid) == PREMIUM
    assert _state.insurance[uid]["expires_at"] == expiry
    assert _state.insurance[uid]["tier"] == "basic"
    assert _state.insurance_subs[uid] == "basic"


async def test_shop_insurance_sub_at_tier_charges_that_tiers_first_day(db):
    from src.cogs.shop_cog import ShopCog
    uid = 9026
    await add_balance(uid, STANDARD + 1)
    ctx = _ctx(uid)
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), ctx, "sub", "standard")
    assert await get_balance(uid) == 1
    assert _state.insurance_subs[uid] == "standard"
    assert _state.insurance[uid]["tier"] == "standard"
    assert await _db_tier("insurance_sub", uid) == "standard"
    assert "Insurance Subscribed" in ctx.sent_embeds[-1].title


async def test_shop_insurance_sub_switch_reprices_coverage_and_moves_sub(db, monkeypatch):
    """A basic subscriber whose coverage outlasts the next sweep picks
    premium: no starter day, the remaining coverage's difference is charged
    now, and both the policy and the subscription are premium."""
    from src.cogs.shop_cog import ShopCog
    now = int(_time.time())
    monkeypatch.setattr(_shop_cog, "next_daily_reset_ts", lambda *a, **k: now + 7200)
    uid = 9027
    _state.insurance_subs[uid] = "basic"
    expiry = _insure(uid, "basic", seconds=10800)
    surcharge = insurance_switch_cost(uid, "premium")
    assert 0 < surcharge <= math.ceil(10800 / DAY * (PREMIUM - BASIC))
    await add_balance(uid, surcharge)
    ctx = _ctx(uid)
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), ctx, "premium", "sub")
    assert await get_balance(uid) == 0
    assert _state.insurance_subs[uid] == "premium"
    assert _state.insurance[uid]["tier"] == "premium"
    assert _state.insurance[uid]["expires_at"] == expiry  # no starter day needed
    assert "Subscription Switched" in ctx.sent_embeds[-1].title


async def test_shop_insurance_sub_same_tier_is_already_subscribed(db):
    from src.cogs.shop_cog import ShopCog
    uid = 9028
    _state.insurance_subs[uid] = "basic"
    _insure(uid, "basic", seconds=3 * DAY)
    await add_balance(uid, 500)
    ctx = _ctx(uid)
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), ctx, "sub")
    assert "Already Subscribed" in ctx.sent_embeds[-1].title
    assert await get_balance(uid) == 500


async def test_shop_insurance_unknown_word_shows_tier_info(db):
    from src.cogs.shop_cog import ShopCog
    uid = 9029
    await add_balance(uid, 500)
    ctx = _ctx(uid)
    await ShopCog(bot=None).shop_insurance.callback(ShopCog(bot=None), ctx, "status")
    desc = ctx.sent_embeds[-1].description
    for name, info in SHOP_INSURANCE_TIERS.items():
        assert f"**{name.title()}** — **{info['cost']:,} 🪙/day**" in desc
        assert f"refunds **{info['refund_pct']}%**" in desc
    assert await get_balance(uid) == 500


# ── migration 0065 backfill ──────────────────────────────────────────────────

@pytest.fixture
def fake_cur():
    """In-memory SQLite cursor behind the MariaDB translator (as in
    tests/test_migrations.py). Auto-commit."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    yield FakeCursor(conn.cursor())
    conn.close()


def _write(tmp_path: Path, name: str, sql: str) -> None:
    (tmp_path / name).write_text(sql, encoding="utf-8")


async def test_0065_backfills_existing_insurance_to_basic(tmp_path, fake_cur):
    """Every pre-tier policy and subscription was bought at the basic price,
    so the real 0065 lands them all on 'basic' and leaves other effects'
    tier NULL."""
    _write(tmp_path, "0001_tables.sql", """
        CREATE TABLE shop_effects (
            guild_id INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL,
            effect_type TEXT NOT NULL,
            expires_at DOUBLE NULL,
            history_json TEXT NULL,
            PRIMARY KEY (guild_id, user_id, effect_type)
        );
    """)
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [1]
    for row in [
        (0, 10, "insurance", 1_000.0, '["steal"]'),
        (0, 10, "insurance_sub", None, None),
        (0, 11, "insurance", 500.0, '["mock"]'),
        (7, 13, "tax", 999.0, None),
    ]:
        await fake_cur.execute(
            "INSERT INTO shop_effects (guild_id, user_id, effect_type, expires_at, history_json)"
            " VALUES (%s,%s,%s,%s,%s)", row,
        )
    real_sql = (Path(__file__).parent.parent / "migrations" / "0065_insurance_tiers.sql").read_text(encoding="utf-8")
    _write(tmp_path, "0002_insurance_tiers.sql", real_sql)
    _migrations._done = False
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [2]

    await fake_cur.execute(
        "SELECT user_id, effect_type, insurance_tier FROM shop_effects ORDER BY user_id, effect_type"
    )
    assert await fake_cur.fetchall() == [
        (10, "insurance", "basic"),
        (10, "insurance_sub", "basic"),
        (11, "insurance", "basic"),
        (13, "tax", None),
    ]
