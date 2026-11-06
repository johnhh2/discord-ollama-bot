"""Property theft (`!steal <property>`, src/property_theft.py) and the repairs
it leaves (`!assets repair`, the Repair button): the odds and payout tables,
damage and the insurance snapshot, the investigators, the one-success-a-day
claim, bail, and the races."""
import asyncio
import random
import time

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
import src.cogs.assets_cog as _assets_cog
import src.cogs.economy_cog as _economy_cog
from src import property_theft as pt
from src.cogs.assets_cog import AssetsCog
from src.cogs.economy_cog import EconomyCog, _StealTarget, _bail_base, crime_jail_seconds
from src.economy import _ct_today, add_balance, get_balance
from src.features import set_feature
from src.properties import PROPERTIES_BY_ID, PROPERTY_UPGRADES, property_daily_revenue
from src.repair_view import RepairView

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage
from tests.test_money_flows import _StubBot, _grant_level
from tests.test_properties import _give_property

GID = 42
THIEF, OWNER = 7_001, 7_002
PID = "movie_theater"  # tier 3
LOW = "lemonade_stand"  # tier 1
_real_sleep = asyncio.sleep  # captured before the autouse fixture patches it


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)
    monkeypatch.setattr(random, "randint", lambda a, b: a)


@pytest.fixture(autouse=True)
def chase(monkeypatch):
    """The chase message, whose last edit is the theft's result embed."""
    msg = FakeMessage()

    async def _chase(ctx, success):
        return msg
    monkeypatch.setattr(_economy_cog, "_chase", _chase)
    return msg


def _result(msg):
    return msg.edit.await_args.kwargs["embed"]


def _rolls(monkeypatch, *values):
    """random.random() returns these in order: the success roll, then the
    jail/arrest roll."""
    it = iter(values)
    monkeypatch.setattr(random, "random", lambda: next(it))


def _guild():
    guild = FakeGuild(gid=GID)
    guild.members.append(FakeMember(uid=OWNER, display_name="owner"))
    return guild


def _ctx(uid=THIEF, content="!steal Movie Theater"):
    ctx = FakeCtx(author=FakeMember(uid=uid, display_name="thief"), guild=_guild())
    ctx.bot = _StubBot()
    ctx.message.content = content
    return ctx


async def _setup(pid=PID, **row):
    await _give_property(OWNER, pid)
    _state.property_owners[pid].update(row)
    _grant_level(OWNER, 12)
    await _economy._ensure_user(THIEF)


async def _steal(pid=PID, ctx=None):
    ctx = ctx or _ctx()
    cog = EconomyCog(bot=_StubBot())
    await cog.cmd_steal.callback(cog, ctx, target=PROPERTIES_BY_ID[pid])
    return ctx


def _insure(uid, tier="standard", secs=86_400):
    _state.property_insurance[uid] = {"expires_at": time.time() + secs, "protected_from": [], "tier": tier}
    _state.property_insurance_subs[uid] = tier


async def _db_damage(pid):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT damaged_at, repair_cost FROM property_owners WHERE property_id=?", (pid,))
            return await cur.fetchone()


# ── the tables ───────────────────────────────────────────────────────────────

def test_odds_and_jail_by_tier():
    assert pt.SUCCESS_BY_TIER == {1: 0.35, 2: 0.30, 3: 0.25, 4: 0.20, 5: 0.15}
    assert pt.JAIL_BY_TIER == {1: 0.20, 2: 0.25, 3: 0.30, 4: 0.35, 5: 0.40}


def test_take_is_two_days_of_rent_with_the_revenue_upgrade():
    row = {"owner_id": OWNER, "upgraded": False}
    assert pt.theft_take(LOW, row) == 220
    upgraded = {"owner_id": OWNER, "upgraded": True}
    assert pt.theft_take("moon_base", upgraded) == 2 * property_daily_revenue("moon_base", upgraded, current=True)
    assert PROPERTY_UPGRADES["moon_base"]  # the upgrade exists to fold in


def test_security_cuts_success_and_investigators_need_insurance():
    row = {"owner_id": OWNER, "secured": True}
    assert pt.success_chance(THIEF, PID, row) == pytest.approx(0.1875)
    assert pt.has_investigators(row) is False
    assert pt.jail_chance(THIEF, PID, row, succeeded=False) == pytest.approx(0.30)
    assert pt.jail_chance(THIEF, PID, row, succeeded=True) == 0.0
    _insure(OWNER)
    assert pt.has_investigators(row) is True
    assert pt.jail_chance(THIEF, PID, row, succeeded=False) == pytest.approx(0.60)
    assert pt.jail_chance(THIEF, PID, row, succeeded=True) == pytest.approx(0.30)


def test_fine_and_bail():
    assert pt.fine_for(10_000) == 1_250
    assert pt.bail_for(220) == 1_000
    assert pt.bail_for(10_000) == 20_000


def test_damage_cuts_income_by_a_quarter():
    row = {"owner_id": OWNER, "upgraded": False}
    full = property_daily_revenue(PID, row)
    assert property_daily_revenue(PID, {**row, "damaged_at": 1.0}) == full * 75 // 100


# ── a successful theft ───────────────────────────────────────────────────────

async def test_success_pays_the_take_and_damages_the_deed(db, monkeypatch, chase):
    await _setup()
    take = pt.theft_take(PID, _state.property_owners[PID])
    _rolls(monkeypatch, 0.0, 0.99)

    ctx = await _steal()

    row = _state.property_owners[PID]
    assert await get_balance(THIEF) == take
    assert pt.is_damaged(row) and row["repair_cost"] == take
    assert _state.economy["users"][str(THIEF)]["property_theft_day"] == _ct_today()
    assert (await _db_damage(PID))[1] == take
    assert _result(chase).title == "🏚️ Property Robbed!"
    ping = ctx.send_mock.await_args_list[-1]
    assert f"<@{OWNER}>" in ping.args[0] and isinstance(ping.kwargs["view"], RepairView)


async def test_a_second_success_the_same_day_is_refused(db, monkeypatch):
    await _setup()
    await _setup(LOW)
    _rolls(monkeypatch, 0.0, 0.99, 0.0, 0.99)
    await _steal()
    before = await get_balance(THIEF)

    ctx = await _steal(LOW, _ctx(content="!steal Lemonade Stand"))

    assert ctx.sent_embeds[-1].title == "🕵️ Lie Low"
    assert await get_balance(THIEF) == before
    assert not pt.is_damaged(_state.property_owners[LOW])


async def test_a_damaged_deed_cant_be_hit_again(db, monkeypatch):
    await _setup(damaged_at=time.time(), repair_cost=500)
    _rolls(monkeypatch, 0.0, 0.99)

    ctx = await _steal()

    assert ctx.sent_embeds[-1].title == "🔧 Already Damaged"
    assert await get_balance(THIEF) == 0


async def test_own_and_bank_owned_deeds_are_refused(db):
    await _setup()
    ctx = await _steal(ctx=_ctx(uid=OWNER))
    assert "your own property" in ctx.sent_embeds[-1].description
    ctx = await _steal("moon_base")
    assert ctx.sent_embeds[-1].title == "🏦 Bank-Owned"


async def test_assets_switched_off_refuses_it(db):
    await _setup()
    set_feature(GID, "assets", False)
    try:
        ctx = await _steal()
    finally:
        set_feature(GID, "assets", True)
    assert ctx.sent_embeds[-1].title == "🚫 Turned Off"


# ── a failed theft ───────────────────────────────────────────────────────────

async def test_failure_fines_an_eighth_of_the_take_and_doesnt_use_up_the_day(db, monkeypatch):
    await _setup()
    take = pt.theft_take(PID, _state.property_owners[PID])
    await add_balance(THIEF, 10_000)
    _rolls(monkeypatch, 0.99, 0.99)

    await _steal()

    assert await get_balance(THIEF) == 10_000 - take // 8
    assert not pt.is_damaged(_state.property_owners[PID])
    assert _state.economy["users"][str(THIEF)].get("property_theft_day") != _ct_today()


async def test_a_jailed_failure_is_sentenced_and_bailed_on_the_take(db, monkeypatch, chase):
    await _setup()
    take = pt.theft_take(PID, _state.property_owners[PID])
    _rolls(monkeypatch, 0.99, 0.0)
    before = time.time()

    await _steal()

    thief = _state.economy["users"][str(THIEF)]
    assert _result(chase).title == "🚔 Caught & Jailed!"
    assert before + crime_jail_seconds(take) - 60 < thief["jail_until"] < before + crime_jail_seconds(take) + 60
    assert thief["bail_price"] == pt.bail_for(take)
    assert _bail_base(thief) == pt.bail_for(take)


def test_other_jails_keep_the_bail_formula():
    assert _bail_base({"bail_amount": 4_000, "bail_price": None}) == 12_000


# ── investigators ────────────────────────────────────────────────────────────

async def test_investigators_arrest_a_successful_thief_who_keeps_the_loot(db, monkeypatch, chase):
    await _setup(secured=True)
    _insure(OWNER)
    take = pt.theft_take(PID, _state.property_owners[PID])
    _rolls(monkeypatch, 0.0, 0.0)

    await _steal()

    thief = _state.economy["users"][str(THIEF)]
    assert await get_balance(THIEF) == take
    assert thief["jail_until"] > time.time()
    assert "investigators" in _result(chase).description
    assert "loot stays theirs" in _result(chase).description


async def test_no_investigators_means_no_arrest_on_success(db, monkeypatch):
    await _setup(secured=True)
    _rolls(monkeypatch, 0.0, 0.0)

    await _steal()

    assert _state.economy["users"][str(THIEF)].get("jail_until", 0) < time.time()


# ── the insurance snapshot ───────────────────────────────────────────────────

async def test_cover_is_locked_in_at_the_theft(db, monkeypatch):
    await _setup()
    _insure(OWNER, "premium", secs=60)
    take = pt.theft_take(PID, _state.property_owners[PID])
    _rolls(monkeypatch, 0.0, 0.99)
    await _steal()

    _state.property_insurance.pop(OWNER)  # lapsed since
    assert pt.repair_split(_state.property_owners[PID]) == (take, 0)


async def test_a_policy_bought_after_the_theft_pays_nothing(db, monkeypatch):
    await _setup()
    take = pt.theft_take(PID, _state.property_owners[PID])
    _rolls(monkeypatch, 0.0, 0.99)
    await _steal()

    _insure(OWNER, "premium")
    assert pt.repair_split(_state.property_owners[PID]) == (0, take)


def test_the_cap_limits_the_insurer_share():
    row = {"repair_cost": 100_000, "repair_cover_pct": 75, "repair_cover_cap": 60_000}
    assert pt.repair_split(row) == (60_000, 40_000)


# ── repairs ──────────────────────────────────────────────────────────────────

async def test_repair_charges_the_owner_share_and_restores_income(db):
    await _setup(damaged_at=time.time(), repair_cost=4_000, repair_cover_pct=50, repair_cover_cap=30_000)
    await add_balance(OWNER, 5_000)
    ctx = FakeCtx(author=FakeMember(uid=OWNER), guild=_guild())

    await AssetsCog.assets_repair.callback(AssetsCog(bot=None), ctx, name="Movie Theater")

    assert await get_balance(OWNER) == 3_000
    assert not pt.is_damaged(_state.property_owners[PID])
    assert (await _db_damage(PID))[0] is None
    assert ctx.sent_embeds[-1].title == "🔧 Repaired"


async def test_repair_all_spends_on_the_biggest_income_loss_first(db):
    await _setup(damaged_at=time.time(), repair_cost=3_000)
    await _setup("space_port", damaged_at=time.time(), repair_cost=3_000)
    await add_balance(OWNER, 4_000)
    ctx = FakeCtx(author=FakeMember(uid=OWNER), guild=_guild())

    await AssetsCog.assets_repair.callback(AssetsCog(bot=None), ctx, name="all")

    assert not pt.is_damaged(_state.property_owners["space_port"])
    assert pt.is_damaged(_state.property_owners[PID])
    assert await get_balance(OWNER) == 1_000
    assert "Not enough left for" in ctx.sent_embeds[-1].description


async def test_bare_repair_with_several_offers_repair_all(db, monkeypatch):
    await _setup(damaged_at=time.time(), repair_cost=1_000)
    await _setup(LOW, damaged_at=time.time(), repair_cost=100)
    await add_balance(OWNER, 5_000)
    seen = {}

    async def _pick(ctx, *, options, **kwargs):
        seen["values"] = [v for _, v in options]
        return ["all"]
    monkeypatch.setattr(_assets_cog, "pick_from_list", _pick)

    await AssetsCog.assets_repair.callback(AssetsCog(bot=None), FakeCtx(author=FakeMember(uid=OWNER), guild=_guild()))

    assert seen["values"][0] == "all"
    assert not pt.damaged_deeds(OWNER)
    assert await get_balance(OWNER) == 3_900


async def test_concurrent_repairs_charge_once(db, monkeypatch):
    await _setup(damaged_at=time.time(), repair_cost=2_000)
    await add_balance(OWNER, 10_000)
    real = _assets_cog.shop_charge

    async def _yielding(ctx, uid, cost, **kw):
        await _real_sleep(0)
        return await real(ctx, uid, cost, **kw)
    monkeypatch.setattr(_assets_cog, "shop_charge", _yielding)
    cog = AssetsCog(bot=None)

    await asyncio.gather(*(
        cog.assets_repair.callback(cog, FakeCtx(author=FakeMember(uid=OWNER), guild=_guild()), name="Movie Theater")
        for _ in range(2)
    ))

    assert await get_balance(OWNER) == 8_000


async def test_failed_charge_puts_the_damage_back(db):
    await _setup(damaged_at=time.time(), repair_cost=2_000)
    ctx = FakeCtx(author=FakeMember(uid=OWNER), guild=_guild())

    await AssetsCog.assets_repair.callback(AssetsCog(bot=None), ctx, name="Movie Theater")

    assert pt.is_damaged(_state.property_owners[PID])
    assert _state.property_owners[PID]["repair_cost"] == 2_000


# ── races and parsing ────────────────────────────────────────────────────────

async def test_two_racing_thefts_succeed_once(db, monkeypatch):
    await _setup()
    await _setup(LOW)
    _rolls(monkeypatch, *([0.0, 0.99] * 4))
    real = _economy_cog._maybe_latch_crime_eligible

    async def _yielding(uid):
        await _real_sleep(0)
        return await real(uid)
    monkeypatch.setattr(_economy_cog, "_maybe_latch_crime_eligible", _yielding)

    await asyncio.gather(_steal(), _steal(LOW, _ctx(content="!steal Lemonade Stand")))

    assert len(pt.damaged_deeds(OWNER)) == 1


async def test_a_deed_name_wins_over_a_member_name(db):
    await _setup()
    ctx = _ctx(content="!steal Movie Theater")
    ctx.prefix, ctx.invoked_with = "!", "steal"

    assert await _StealTarget().convert(ctx, "Movie") is PROPERTIES_BY_ID[PID]


# ── the Repair button ────────────────────────────────────────────────────────

def _press(uid):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(user=FakeMember(uid=uid), guild=SimpleNamespace(id=GID),
                           message=FakeMessage(), response=response)


async def test_only_the_owner_can_press_repair(db, monkeypatch):
    import src.repair_view as _rv
    calls = []

    async def _forward(ctx, command, *args, cog=None, **kwargs):
        calls.append(kwargs)
    monkeypatch.setattr(_rv, "forward", _forward)
    monkeypatch.setattr(_rv, "button_context", lambda *a, **k: object())
    monkeypatch.setattr(_rv, "refusal_for", lambda *a, **k: None)
    command = type("Cmd", (), {"cog": None})()
    bot = type("Bot", (), {"get_command": staticmethod(lambda name: command)})()
    await _setup(damaged_at=time.time(), repair_cost=100)
    view = RepairView(bot, OWNER, PID)
    button = view.children[0]

    stranger = _press(THIEF)
    await button.callback(stranger)
    stranger.response.send_message.assert_awaited()
    assert calls == []

    await button.callback(_press(OWNER))
    assert calls == [{"name": PID}]
