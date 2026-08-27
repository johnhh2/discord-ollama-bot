"""Real-estate system tests: revenue math, accrual cap + artifacts, the
daily-claim payout integration, unique-deed buy races, the cross-server
marketplace, and record wiring.
"""
import asyncio
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
from src.config import DAILY_REWARD
from src.economy import add_balance, get_balance
from src.properties import (
    PROPERTIES, PROPERTY_MAX_OWNED, PROPERTY_SALE_FEE_PCT,
    PROPERTY_ACCRUAL_CAP_BASE,
    find_property, daily_revenue, owned_property_count, portfolio_daily_revenue, accrual_cap, pending_property_revenue,
    bank_property_revenue,
)
from src.cogs.assets_cog import AssetsCog

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild


pytestmark = pytest.mark.asyncio

GID = 42
DAY = 86400.0


def _ctx(uid: int, gid: int = GID) -> FakeCtx:
    return FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=gid), command_name="assets")


def _set_level(uid: int, display_level: int, gid: int = GID):
    _state.leveling.setdefault(str(gid), {})[str(uid)] = {"level": display_level - 1}


async def _give_property(uid: int, pid: str, acquired_days_ago: float = 10.0):
    """Directly install ownership (bypassing the shop) for revenue tests."""
    await _economy._ensure_user(uid)
    now = time.time()
    row = {
        "owner_id": uid, "acquired_at": now - acquired_days_ago * DAY,
        "list_price": None, "listed_at": None,
        "upgraded": False, "custom_name": None,
    }
    _state.property_owners[pid] = row
    await _persistence.save_property_owner(pid, row)


def _give_artifact(uid: int, artifact_id: str):
    _state.user_artifacts.setdefault(uid, {})[artifact_id] = 1


async def _read_db_owner(pid: str):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT owner_id, list_price FROM property_owners WHERE property_id=?",
                (pid,),
            )
            return await cur.fetchone()


# ── catalog invariants ───────────────────────────────────────────────────────

async def test_catalog_ids_unique_and_revenue_formula():
    ids = [p["id"] for p in PROPERTIES]
    assert len(ids) == len(set(ids))
    names = [p["name"].lower() for p in PROPERTIES]
    assert len(names) == len(set(names))
    for p in PROPERTIES:
        # 1.1% of purchase price per day, always derived from cost.
        assert daily_revenue(p["cost"]) == p["cost"] * 11 // 1000
        assert p["tier"] in (1, 2, 3, 4, 5)


async def test_find_property_matches_name_and_id():
    assert find_property("Hot Dog Cart")["id"] == "hot_dog_cart"
    assert find_property("hot_dog_cart")["id"] == "hot_dog_cart"
    assert find_property("HOT DOG CART")["id"] == "hot_dog_cart"
    assert find_property("no such place") is None


# ── revenue accrual ──────────────────────────────────────────────────────────

async def test_pending_revenue_accrues_daily_and_caps(db):
    uid = 9001
    await _give_property(uid, "car_wash", acquired_days_ago=100.0)  # 20k → 220/day

    now = time.time()
    user = _state.economy["users"][str(uid)]
    # Paid yesterday → one day accrued.
    user["property_paid_at"] = now - DAY
    assert pending_property_revenue(uid, now) == daily_revenue(20_000)

    # Never paid since acquisition → accrual is capped at the base cap.
    user["property_paid_at"] = 0.0
    assert pending_property_revenue(uid, now) == PROPERTY_ACCRUAL_CAP_BASE


async def test_pending_revenue_never_backdates_before_acquisition(db):
    uid = 9002
    await _give_property(uid, "car_wash", acquired_days_ago=1.0)
    user = _state.economy["users"][str(uid)]
    # paid_at long before acquisition — accrual starts at acquisition.
    user["property_paid_at"] = time.time() - 50 * DAY
    now = time.time()
    assert pending_property_revenue(uid, now) == daily_revenue(20_000)


async def test_accrual_cap_uses_daily_revenue_when_larger(db):
    uid = 9003
    # Space Port: 2m → 22,000/day > 10k base cap.
    await _give_property(uid, "space_port")
    assert accrual_cap(uid) == daily_revenue(2_000_000)
    # A big portfolio's cap is one full day of revenue — a daily claimer
    # never loses coins to the cap.
    await _give_property(uid, "skyscraper")
    assert accrual_cap(uid) == daily_revenue(2_000_000) + daily_revenue(1_600_000)


async def test_cap_artifact_raises_accrual_cap(db):
    uid = 9004
    await _give_property(uid, "car_wash")
    assert accrual_cap(uid) == PROPERTY_ACCRUAL_CAP_BASE
    _give_artifact(uid, "property_cap_deed")
    assert accrual_cap(uid) == PROPERTY_ACCRUAL_CAP_BASE + 5_000


async def test_mogul_artifact_boosts_by_5pct_per_property(db):
    uid = 9005
    await _give_property(uid, "car_wash")       # 220/day
    await _give_property(uid, "lemonade_stand") # 110/day
    base = daily_revenue(20_000) + daily_revenue(10_000)
    assert portfolio_daily_revenue(uid) == base
    _give_artifact(uid, "property_mogul")
    # 2 properties → +10%.
    assert portfolio_daily_revenue(uid) == int(base * 1.1)


async def test_bank_property_revenue_pays_and_stamps(db):
    uid = 9006
    await _give_property(uid, "car_wash")
    user = _state.economy["users"][str(uid)]
    now = time.time()
    user["property_paid_at"] = now - DAY
    banked = await bank_property_revenue(uid, now)
    assert banked == daily_revenue(20_000)
    assert await get_balance(uid) == banked
    assert user["property_paid_at"] == now
    assert user["property_revenue_total"] == banked
    # Immediately banking again pays nothing.
    assert await bank_property_revenue(uid) == 0


# ── daily-claim integration ──────────────────────────────────────────────────

async def test_auto_daily_includes_property_revenue(db):
    from src.events import _auto_daily
    from tests.fakes.discord import FakeChannel
    uid = 9010
    await _give_property(uid, "car_wash")
    user = _state.economy["users"][str(uid)]
    now = time.time()
    user["property_paid_at"] = now - DAY
    rev = daily_revenue(20_000)

    member = FakeMember(uid=uid)
    channel = FakeChannel()
    total, prop_rev = await _auto_daily(member, channel)
    assert prop_rev == rev
    assert total == DAILY_REWARD + rev
    assert await get_balance(uid) == DAILY_REWARD + rev
    # The claim message names the property portion.
    sent = channel.send.await_args
    assert "property revenue" in sent.kwargs["embed"].description


async def test_cmd_daily_message_includes_property_revenue(db):
    from src.cogs.economy_cog import EconomyCog
    uid = 9011
    await _give_property(uid, "car_wash")
    user = _state.economy["users"][str(uid)]
    now = time.time()
    user["property_paid_at"] = now - DAY
    rev = daily_revenue(20_000)

    cog = EconomyCog.__new__(EconomyCog)
    ctx = _ctx(uid)
    await EconomyCog.cmd_daily.callback(cog, ctx)
    assert await get_balance(uid) == DAILY_REWARD + rev
    desc = ctx.sent_embeds[-1].description
    assert f"+{DAILY_REWARD:,} 🪙" in desc
    assert f"{rev:,} 🪙** property revenue" in desc


async def test_cmd_daily_without_property_message_unchanged(db):
    from src.cogs.economy_cog import EconomyCog
    uid = 9012
    cog = EconomyCog.__new__(EconomyCog)
    ctx = _ctx(uid)
    await EconomyCog.cmd_daily.callback(cog, ctx)
    assert "property revenue" not in ctx.sent_embeds[-1].description


async def test_concurrent_daily_claims_pay_property_revenue_once(db, monkeypatch):
    """The revenue rides the daily claim's synchronous gate — two racing
    !daily invocations must bank the property revenue exactly once."""
    from src.cogs.economy_cog import EconomyCog
    uid = 9013
    await _give_property(uid, "car_wash")
    user = _state.economy["users"][str(uid)]
    now = time.time()
    user["property_paid_at"] = now - DAY
    rev = daily_revenue(20_000)

    real_add = _economy.add_balance

    async def _yielding_add(*a, **kw):
        await asyncio.sleep(0)   # force a real event-loop yield
        return await real_add(*a, **kw)

    monkeypatch.setattr("src.cogs.economy_cog.add_balance", _yielding_add)

    cog = EconomyCog.__new__(EconomyCog)
    await asyncio.gather(
        EconomyCog.cmd_daily.callback(cog, _ctx(uid)),
        EconomyCog.cmd_daily.callback(cog, _ctx(uid)),
    )
    assert await get_balance(uid) == DAILY_REWARD + rev


# ── buying ───────────────────────────────────────────────────────────────────

async def test_buy_from_bank_charges_and_persists(db):
    uid = 9020
    _set_level(uid, 50)
    await add_balance(uid, 50_000)
    cog = AssetsCog(bot=None)
    ctx = _ctx(uid)
    await AssetsCog.assets_buy.callback(cog, ctx, name="Car Wash")

    assert _state.property_owners["car_wash"]["owner_id"] == uid
    assert await get_balance(uid) == 30_000
    row = await _read_db_owner("car_wash")
    assert row is not None and int(row[0]) == uid


async def test_buy_rolls_back_claim_on_insufficient_funds(db):
    uid = 9021
    _set_level(uid, 50)
    await add_balance(uid, 100)   # can't afford 20k
    cog = AssetsCog(bot=None)
    ctx = _ctx(uid)
    await AssetsCog.assets_buy.callback(cog, ctx, name="Car Wash")

    assert "car_wash" not in _state.property_owners
    assert await _read_db_owner("car_wash") is None
    assert await get_balance(uid) == 100


async def test_buy_respects_level_gate(db):
    uid = 9022
    _set_level(uid, 3)            # Car Wash needs level 5
    await add_balance(uid, 50_000)
    cog = AssetsCog(bot=None)
    ctx = _ctx(uid)
    await AssetsCog.assets_buy.callback(cog, ctx, name="Car Wash")
    assert "car_wash" not in _state.property_owners
    assert "Level" in ctx.sent_embeds[-1].title or "Level" in ctx.sent_embeds[-1].description


async def test_buy_enforces_ownership_cap(db):
    uid = 9023
    _set_level(uid, 50)
    for pid in ("lemonade_stand", "hot_dog_cart", "newspaper_kiosk",
                "vending_route", "laundromat"):
        await _give_property(uid, pid)
    await add_balance(uid, 100_000)
    cog = AssetsCog(bot=None)
    ctx = _ctx(uid)
    await AssetsCog.assets_buy.callback(cog, ctx, name="Car Wash")
    assert "car_wash" not in _state.property_owners
    assert owned_property_count(uid) == PROPERTY_MAX_OWNED
    assert "Portfolio Full" in ctx.sent_embeds[-1].title


async def test_concurrent_buys_of_one_deed_sell_it_once(db, monkeypatch):
    """Two users race for the same unique deed — exactly one gets it and
    exactly one is charged."""
    a, b = 9024, 9025
    for uid in (a, b):
        _set_level(uid, 50)
        await add_balance(uid, 50_000)

    import src.cogs.assets_cog as _assets_cog
    real_charge = _assets_cog.shop_charge

    async def _yielding_charge(*args, **kw):
        await asyncio.sleep(0)   # force a real event-loop yield inside the window
        return await real_charge(*args, **kw)

    monkeypatch.setattr("src.cogs.assets_cog.shop_charge", _yielding_charge)

    cog = AssetsCog(bot=None)
    await asyncio.gather(
        AssetsCog.assets_buy.callback(cog, _ctx(a), name="Car Wash"),
        AssetsCog.assets_buy.callback(cog, _ctx(b), name="Car Wash"),
    )
    owner = _state.property_owners["car_wash"]["owner_id"]
    assert owner in (a, b)
    loser = b if owner == a else a
    assert await get_balance(owner) == 30_000   # charged
    assert await get_balance(loser) == 50_000   # untouched


# ── marketplace ──────────────────────────────────────────────────────────────

async def test_list_buy_cross_server_pays_seller_minus_fee(db):
    seller, buyer = 9030, 9031
    _set_level(seller, 50, gid=GID)
    _set_level(buyer, 50, gid=77)          # buyer is in a DIFFERENT guild
    await _give_property(seller, "car_wash")
    await add_balance(buyer, 100_000)

    cog = AssetsCog(bot=None)
    # Seller lists at 50k in guild 42.
    await AssetsCog.assets_sell.callback(cog, _ctx(seller, gid=GID), "Car", "Wash", "50k")
    assert _state.property_owners["car_wash"]["list_price"] == 50_000

    # Buyer buys from guild 77.
    buy_ctx = _ctx(buyer, gid=77)
    await AssetsCog.assets_buy.callback(cog, buy_ctx, name="Car Wash")

    row = _state.property_owners["car_wash"]
    assert row["owner_id"] == buyer
    assert row["list_price"] is None
    fee = 50_000 * PROPERTY_SALE_FEE_PCT // 100
    assert await get_balance(buyer) == 50_000
    assert await get_balance(seller) == 50_000 - fee
    # Fee lands in the buyer's guild's house pot.
    assert _economy.get_guild_house_balance(77) == fee
    db_row = await _read_db_owner("car_wash")
    assert int(db_row[0]) == buyer and db_row[1] is None


async def test_owned_unlisted_property_cannot_be_bought(db):
    owner, buyer = 9032, 9033
    _set_level(buyer, 50)
    await _give_property(owner, "car_wash")
    await add_balance(buyer, 100_000)
    cog = AssetsCog(bot=None)
    ctx = _ctx(buyer)
    await AssetsCog.assets_buy.callback(cog, ctx, name="Car Wash")
    assert _state.property_owners["car_wash"]["owner_id"] == owner
    assert await get_balance(buyer) == 100_000


async def test_unlist_clears_listing(db):
    uid = 9034
    await _give_property(uid, "car_wash")
    cog = AssetsCog(bot=None)
    await AssetsCog.assets_sell.callback(cog, _ctx(uid), "Car", "Wash", "50000")
    await AssetsCog.assets_unlist.callback(cog, _ctx(uid), name="Car Wash")
    row = _state.property_owners["car_wash"]
    assert row["list_price"] is None and row["listed_at"] is None


async def test_cannot_list_property_you_dont_own(db):
    uid = 9035
    cog = AssetsCog(bot=None)
    ctx = _ctx(uid)
    await AssetsCog.assets_sell.callback(cog, ctx, "Car", "Wash", "50000")
    assert "car_wash" not in _state.property_owners
    assert "Not Yours" in ctx.sent_embeds[-1].title


# ── records ──────────────────────────────────────────────────────────────────

async def test_buy_sets_property_records(db):
    from src.persistence.records import load_records
    uid = 9040
    _set_level(uid, 50)
    await add_balance(uid, 50_000)
    cog = AssetsCog(bot=None)
    await AssetsCog.assets_buy.callback(cog, _ctx(uid), name="Car Wash")

    recs = await load_records(GID)
    assert recs["total_assets"]["value"] == 1
    assert recs["total_assets"]["holder_id"] == uid
    assert recs["highest_property_value"]["value"] == 20_000


async def test_property_records_mirror_across_guilds(db):
    """total_assets is a global per-user stat — buying in guild A moves the
    record in every other guild the holder is active in."""
    from src.persistence.records import load_records
    uid = 9041
    _set_level(uid, 50, gid=GID)
    _set_level(uid, 50, gid=77)   # active in a second guild
    await add_balance(uid, 50_000)
    cog = AssetsCog(bot=None)
    await AssetsCog.assets_buy.callback(cog, _ctx(uid, gid=GID), name="Car Wash")

    recs_other = await load_records(77)
    assert recs_other["total_assets"]["value"] == 1
    assert recs_other["highest_property_value"]["value"] == 20_000


# ── flip/slots record exclusion ──────────────────────────────────────────────

async def test_flip_record_excludes_property_portion(db, monkeypatch):
    """A dailies-claim flip stakes daily + property revenue, but only the
    non-property portion may enter the 'biggest flip win' record."""
    from src.gambling.flip import play_flip
    from src.persistence.records import load_records
    from tests.fakes.discord import FakeChannel
    uid = 9050
    await add_balance(uid, 10_000)
    monkeypatch.setattr("src.gambling.flip.random.random", lambda: 0.1)  # heads → win
    member = FakeMember(uid=uid)
    guild = FakeGuild(gid=GID)
    channel = FakeChannel(guild=guild)
    await play_flip(member, channel, guild, 5_000, record_exclude=4_800)

    recs = await load_records(GID)
    # Record offer is (5000 - 4800) × 2, not 5000 × 2.
    assert recs["flip"]["value"] == 400
    # Payout is untouched: 10k - 5k + 10k = 15k.
    assert await get_balance(uid) == 15_000


async def test_slots_record_excludes_property_portion(db, monkeypatch):
    from src.gambling.slots import play_slots
    from src.persistence.records import load_records
    from tests.fakes.discord import FakeChannel
    uid = 9051
    await add_balance(uid, 10_000)
    _state.rigged_slots[uid] = "🍒"   # force 3-cherry (3x)
    member = FakeMember(uid=uid)
    guild = FakeGuild(gid=GID)
    channel = FakeChannel(guild=guild)
    await play_slots(member, channel, guild, 1_000, record_exclude=900)

    recs = await load_records(GID)
    assert recs["slots_non_jackpot"]["value"] == 300   # (1000-900) × 3
    assert await get_balance(uid) == 12_000            # payout untouched: 10k - 1k + 3k


async def test_flip_record_skipped_when_stake_all_property(db, monkeypatch):
    from src.gambling.flip import play_flip
    from src.persistence.records import load_records
    from tests.fakes.discord import FakeChannel
    uid = 9052
    await add_balance(uid, 10_000)
    monkeypatch.setattr("src.gambling.flip.random.random", lambda: 0.1)
    guild = FakeGuild(gid=GID)
    await play_flip(FakeMember(uid=uid), FakeChannel(guild=guild), guild, 1_000, record_exclude=1_000)
    recs = await load_records(GID)
    assert "flip" not in recs


# ── portfolio view ───────────────────────────────────────────────────────────

async def test_portfolio_shows_daily_and_banked_revenue(db):
    uid = 9060
    await _give_property(uid, "car_wash")
    user = _state.economy["users"][str(uid)]
    now = time.time()
    user["property_paid_at"] = now - DAY
    await bank_property_revenue(uid, now)          # bank one day → lifetime total
    rev = daily_revenue(20_000)

    cog = AssetsCog(bot=None)
    ctx = _ctx(uid)
    await AssetsCog.cmd_assets.callback(cog, ctx, None)
    desc = ctx.sent_embeds[-1].description
    assert f"**Daily revenue:** {rev:,} 🪙/day" in desc
    assert f"**Revenue banked (lifetime):** {rev:,} 🪙" in desc
    assert "**Unredeemed revenue:**" in desc


# ── upgrades ─────────────────────────────────────────────────────────────────

async def test_upgrade_catalog_complete_and_in_range():
    from src.properties import PROPERTY_UPGRADES, PROPERTIES_BY_ID
    assert set(PROPERTY_UPGRADES) == set(PROPERTIES_BY_ID)
    for pid, (name, cost, boost) in PROPERTY_UPGRADES.items():
        base = PROPERTIES_BY_ID[pid]["cost"]
        assert name
        # Hardcoded costs were rolled at 75-125% of property cost (then
        # rounded), boosts at 35-75%.
        assert base * 70 // 100 <= cost <= base * 130 // 100, pid
        assert 35 <= boost <= 75, pid


async def test_upgrade_boosts_revenue_and_value(db):
    from src.properties import PROPERTY_UPGRADES, property_value, property_daily_revenue
    uid = 9070
    await _give_property(uid, "tattoo_parlor")   # 68k
    row = _state.property_owners["tattoo_parlor"]
    base_rev = daily_revenue(68_000)
    assert property_daily_revenue("tattoo_parlor", row) == base_rev
    assert property_value("tattoo_parlor", row) == 68_000

    row["upgraded"] = True
    up_name, up_cost, up_boost = PROPERTY_UPGRADES["tattoo_parlor"]
    assert up_name == "Piercing Booth"
    assert property_daily_revenue("tattoo_parlor", row) == base_rev * (100 + up_boost) // 100
    assert property_value("tattoo_parlor", row) == 68_000 + up_cost
    # Pending accrual uses the boosted rate.
    user = _state.economy["users"][str(uid)]
    now = time.time()
    user["property_paid_at"] = now - DAY
    assert pending_property_revenue(uid, now) == base_rev * (100 + up_boost) // 100


async def test_upgrade_command_charges_and_persists(db):
    from src.properties import PROPERTY_UPGRADES
    uid = 9071
    _set_level(uid, 50)
    await _give_property(uid, "tattoo_parlor")
    _, up_cost, _ = PROPERTY_UPGRADES["tattoo_parlor"]
    await add_balance(uid, up_cost + 1_000)
    cog = AssetsCog(bot=None)
    ctx = _ctx(uid)
    await AssetsCog.assets_upgrade.callback(cog, ctx, name="Tattoo Parlor")
    assert _state.property_owners["tattoo_parlor"]["upgraded"] is True
    assert await get_balance(uid) == 1_000
    # Second attempt is a no-op with a friendly message.
    await AssetsCog.assets_upgrade.callback(cog, ctx, name="Tattoo Parlor")
    assert await get_balance(uid) == 1_000
    assert "Already Upgraded" in ctx.sent_embeds[-1].title


async def test_upgrade_rolls_back_on_insufficient_funds(db):
    uid = 9072
    await _give_property(uid, "tattoo_parlor")
    await add_balance(uid, 10)
    cog = AssetsCog(bot=None)
    await AssetsCog.assets_upgrade.callback(cog, _ctx(uid), name="Tattoo Parlor")
    assert _state.property_owners["tattoo_parlor"]["upgraded"] is False
    assert await get_balance(uid) == 10


async def test_upgrade_value_feeds_property_record(db):
    from src.properties import PROPERTY_UPGRADES
    from src.persistence.records import load_records
    uid = 9073
    _set_level(uid, 50)
    _, up_cost, _ = PROPERTY_UPGRADES["car_wash"]
    await add_balance(uid, 20_000 + up_cost)
    cog = AssetsCog(bot=None)
    await AssetsCog.assets_buy.callback(cog, _ctx(uid), name="Car Wash")
    await AssetsCog.assets_upgrade.callback(cog, _ctx(uid), name="Car Wash")
    recs = await load_records(GID)
    assert recs["highest_property_value"]["value"] == 20_000 + up_cost


# ── bank buyback offer ───────────────────────────────────────────────────────

async def test_lowball_listing_accepted_sells_to_bank(db, monkeypatch):
    uid = 9080
    await _give_property(uid, "car_wash")      # value 20k -> offer 15k

    async def _accept(*a, **kw):
        return True
    monkeypatch.setattr("src.cogs.assets_cog.confirm_prompt", _accept)

    cog = AssetsCog(bot=None)
    ctx = _ctx(uid)
    await AssetsCog.assets_sell.callback(cog, ctx, "Car", "Wash", "15000")
    assert "car_wash" not in _state.property_owners
    assert await _read_db_owner("car_wash") is None
    assert await get_balance(uid) == 15_000    # 75% of 20k, no market fee
    assert "Bank" in ctx.sent_embeds[-1].title


async def test_lowball_listing_declined_lists_normally(db, monkeypatch):
    uid = 9081
    await _give_property(uid, "car_wash")

    async def _decline(*a, **kw):
        return False
    monkeypatch.setattr("src.cogs.assets_cog.confirm_prompt", _decline)

    cog = AssetsCog(bot=None)
    await AssetsCog.assets_sell.callback(cog, _ctx(uid), "Car", "Wash", "15000")
    row = _state.property_owners["car_wash"]
    assert row["owner_id"] == uid and row["list_price"] == 15_000


async def test_above_threshold_listing_skips_bank_offer(db, monkeypatch):
    uid = 9082
    await _give_property(uid, "car_wash")

    async def _boom(*a, **kw):
        raise AssertionError("bank offer should not fire above 75% of value")
    monkeypatch.setattr("src.cogs.assets_cog.confirm_prompt", _boom)

    cog = AssetsCog(bot=None)
    await AssetsCog.assets_sell.callback(cog, _ctx(uid), "Car", "Wash", "15001")
    assert _state.property_owners["car_wash"]["list_price"] == 15_001


async def test_bank_offer_includes_upgrade_in_value(db, monkeypatch):
    from src.properties import PROPERTY_UPGRADES
    uid = 9083
    await _give_property(uid, "car_wash")
    _state.property_owners["car_wash"]["upgraded"] = True
    _, up_cost, _ = PROPERTY_UPGRADES["car_wash"]
    value = 20_000 + up_cost

    async def _accept(*a, **kw):
        return True
    monkeypatch.setattr("src.cogs.assets_cog.confirm_prompt", _accept)

    cog = AssetsCog(bot=None)
    # 75% of the upgraded value still triggers the offer and pays on it.
    await AssetsCog.assets_sell.callback(cog, _ctx(uid), "Car", "Wash", str(value * 75 // 100))
    assert await get_balance(uid) == value * 75 // 100
    assert "car_wash" not in _state.property_owners


# ── rename ───────────────────────────────────────────────────────────────────

async def test_rename_sets_custom_name_and_resolves(db):
    uid = 9090
    await _give_property(uid, "tattoo_parlor")
    cog = AssetsCog(bot=None)
    await AssetsCog.assets_rename.callback(cog, _ctx(uid), "Tattoo", "Parlor", "Inkwell", "Studio")
    assert _state.property_owners["tattoo_parlor"]["custom_name"] == "Inkwell Studio"
    # The custom name now resolves in find_property (sell/upgrade/buy input).
    assert find_property("inkwell studio")["id"] == "tattoo_parlor"


async def test_rename_rejects_collisions_and_non_owner(db):
    uid, other = 9091, 9092
    await _give_property(uid, "tattoo_parlor")
    await _give_property(other, "car_wash")
    _state.property_owners["car_wash"]["custom_name"] = "Sudsy Suds"
    cog = AssetsCog(bot=None)
    # Catalog-name collision.
    ctx = _ctx(uid)
    await AssetsCog.assets_rename.callback(cog, ctx, "Tattoo", "Parlor", "Car", "Wash")
    assert _state.property_owners["tattoo_parlor"]["custom_name"] is None
    assert "Name Taken" in ctx.sent_embeds[-1].title
    # Another business's custom-name collision.
    await AssetsCog.assets_rename.callback(cog, _ctx(uid), "Tattoo", "Parlor", "Sudsy", "Suds")
    assert _state.property_owners["tattoo_parlor"]["custom_name"] is None
    # Renaming someone else's property fails.
    ctx2 = _ctx(uid)
    await AssetsCog.assets_rename.callback(cog, ctx2, "Car", "Wash", "Mine", "Now")
    assert _state.property_owners["car_wash"]["custom_name"] == "Sudsy Suds"


async def test_custom_name_and_upgrade_travel_on_market_sale(db):
    seller, buyer = 9093, 9094
    _set_level(buyer, 50, gid=77)
    await _give_property(seller, "car_wash")
    row = _state.property_owners["car_wash"]
    row["custom_name"] = "Sudsy Suds"
    row["upgraded"] = True
    await add_balance(buyer, 100_000)
    cog = AssetsCog(bot=None)
    await AssetsCog.assets_sell.callback(cog, _ctx(seller), "Sudsy", "Suds", "50000")
    await AssetsCog.assets_buy.callback(cog, _ctx(buyer, gid=77), name="Sudsy Suds")
    row = _state.property_owners["car_wash"]
    assert row["owner_id"] == buyer
    assert row["custom_name"] == "Sudsy Suds" and row["upgraded"] is True
