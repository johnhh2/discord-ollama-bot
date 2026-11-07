"""The `!steal` panel (src/steal_view.py), auto-repair and the daily claim's
theft report (src/property_repair.py, `!assets autorepair`)."""
import asyncio
import random
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
import src.cogs.assets_cog as _assets_cog
import src.cogs.economy_cog as _economy_cog
import src.steal_view as _steal_view
from src import property_theft as pt
from src.cogs.assets_cog import AssetsCog
from src.cogs.economy_cog import EconomyCog
from src.economy import _ct_today, add_balance, get_balance
from src.properties import PROPERTIES_BY_ID
from src.property_repair import auto_repair, claim_report
from src.repair_view import RepairView
from src.steal_view import StealView

from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage
from tests.test_money_flows import _StubBot, _grant_level
from tests.test_property_theft import _insure
from tests.test_properties import _give_property

GID = 42
THIEF, OWNER = 7_101, 7_102
PID = "movie_theater"  # tier 3
LOW = "lemonade_stand"  # tier 1


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(asyncio, "sleep", _noop)
    monkeypatch.setattr(random, "randint", lambda a, b: a)

    async def _chase(ctx, success):
        return FakeMessage()
    monkeypatch.setattr(_economy_cog, "_chase", _chase)


@pytest.fixture
def panel(monkeypatch):
    """The real panel instead of conftest's auto-submit."""
    monkeypatch.setattr(_economy_cog, "open_steal_panel", _steal_view.open_steal_panel)


def _rolls(monkeypatch, *values):
    it = iter(values)
    monkeypatch.setattr(random, "random", lambda: next(it))


OWNER_MEMBER = FakeMember(uid=OWNER, display_name="owner")


def _ctx(uid=THIEF, content="!steal @owner"):
    guild = FakeGuild(gid=GID)
    guild.members.append(OWNER_MEMBER)
    ctx = FakeCtx(author=FakeMember(uid=uid, display_name="thief"), guild=guild)
    ctx.bot = _StubBot()
    ctx.message.content = content
    return ctx


async def _setup(*pids, wallet=10_000, **row):
    for pid in pids:
        await _give_property(OWNER, pid)
        _state.property_owners[pid].update(row)
    _grant_level(OWNER, 12)
    await _economy._ensure_user(THIEF)
    await add_balance(OWNER, wallet)


def _press(uid=THIEF):
    response = SimpleNamespace(edit_message=AsyncMock(), defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock())
    return SimpleNamespace(user=FakeMember(uid=uid), guild=SimpleNamespace(id=GID), response=response)


def _child(view, label):
    return next(c for c in view.children if str(getattr(c, "label", "")).startswith(label))


def _select(view):
    return next(c for c in view.children if hasattr(c, "options"))


async def _open(content="!steal @owner", target=OWNER_MEMBER):
    ctx = _ctx(content=content)
    cog = EconomyCog(bot=_StubBot())
    await cog.cmd_steal.callback(cog, ctx, target=target)
    return ctx, ctx.sent_views[-1] if ctx.sent_views else None


def _drawn(press):
    return press.response.edit_message.await_args.kwargs["embed"]


# ── the panel ────────────────────────────────────────────────────────────────

async def test_bare_steal_on_an_owner_asks_wallet_or_property(db, panel):
    await _setup(PID)
    ctx, view = await _open()

    assert {c.label for c in view.children} == {"Wallet", "Property", "Cancel"}
    assert "wallet" in ctx.sent_embeds[-1].description and "properties" in ctx.sent_embeds[-1].description


async def test_a_target_without_properties_goes_straight_to_the_wallet(db, panel):
    await _setup(wallet=10_000)
    ctx, view = await _open()

    assert [o.value for o in _select(view).options] == ["1", "2", "3"]
    assert _child(view, "Submit").disabled


async def test_a_typed_tier_previews_and_only_submit_robs(db, panel, monkeypatch):
    await _setup(wallet=10_000)
    ctx, view = await _open("!steal @owner 1")

    desc = ctx.sent_embeds[-1].description
    assert "1,000 🪙" in desc and "10% success" in desc and "bail" in desc
    assert await get_balance(OWNER) == 10_000

    _rolls(monkeypatch, 0.05)
    await _child(view, "Submit").callback(_press())

    assert await get_balance(OWNER) == 9_000
    assert await get_balance(THIEF) == 1_000


async def test_property_pick_previews_then_submit_robs_it(db, panel, monkeypatch):
    await _setup(PID, LOW)
    ctx, view = await _open()

    await _child(view, "Property").callback(_press())
    select = _select(view)
    assert [o.value for o in select.options] == [PID, LOW]  # biggest take first
    select._values = [PID]
    press = _press()
    await select.callback(press)

    take = pt.theft_take(PID, _state.property_owners[PID])
    assert f"{take:,} 🪙" in _drawn(press).description
    assert not pt.is_damaged(_state.property_owners[PID])

    _rolls(monkeypatch, 0.0, 0.99)
    await _child(view, "Submit").callback(_press())

    assert pt.is_damaged(_state.property_owners[PID])
    assert await get_balance(THIEF) == take


async def test_a_typed_property_opens_on_that_deed(db, panel):
    await _setup(PID, LOW)
    ctx, view = await _open("!steal Movie Theater", target=PROPERTIES_BY_ID[PID])

    assert view.mode == "property" and view.pid == PID
    assert not _child(view, "Submit").disabled


async def test_a_damaged_deed_cant_be_submitted(db, panel):
    await _setup(PID, damaged_at=time.time(), repair_cost=100)
    ctx = _ctx()
    view = StealView(EconomyCog(bot=_StubBot()), ctx, OWNER_MEMBER, pid=PID)
    embed = view.render()

    assert "repaired" in embed.description
    assert _child(view, "Submit").disabled
    assert "damaged" in _select(view).options[0].description


async def test_property_is_greyed_out_after_todays_success(db, panel):
    await _setup(PID)
    await _economy._ensure_user(THIEF)
    _state.economy["users"][str(THIEF)]["property_theft_day"] = _ct_today()
    ctx, view = await _open()

    assert _child(view, "Property").disabled


async def test_investigators_show_in_the_preview(db):
    await _setup(PID, secured=True)
    _insure(OWNER)
    view = StealView(EconomyCog(bot=_StubBot()), _ctx(), OWNER_MEMBER, pid=PID)

    assert "Investigators" in view.render().description
    assert "🕵️" in _select(view).options[0].description


async def test_submit_redraws_when_the_numbers_moved(db, panel, monkeypatch):
    await _setup(wallet=10_000)
    ctx, view = await _open("!steal @owner 1")
    await add_balance(OWNER, 10_000)  # the take is now 2,000, not the 1,000 shown

    press = _press()
    await _child(view, "Submit").callback(press)

    assert "Check again" in _drawn(press).fields[0].name
    assert await get_balance(OWNER) == 20_000

    _rolls(monkeypatch, 0.05)
    await _child(view, "Submit").callback(_press())
    assert await get_balance(THIEF) == 2_000


async def test_only_the_invoker_can_press(db):
    await _setup(PID)
    view = StealView(EconomyCog(bot=_StubBot()), _ctx(), OWNER_MEMBER)
    view.render()

    stranger = _press(OWNER)
    assert await view.interaction_check(stranger) is False
    stranger.response.send_message.assert_awaited()
    assert await view.interaction_check(_press()) is True


async def test_cancel_walks_away(db, panel):
    await _setup(wallet=10_000)
    ctx, view = await _open("!steal @owner 1")
    press = _press()
    await _child(view, "Cancel").callback(press)

    assert press.response.edit_message.await_args.kwargs["view"] is None
    assert await get_balance(OWNER) == 10_000


# ── auto-repair ──────────────────────────────────────────────────────────────

async def _rob(monkeypatch, pid=PID):
    _rolls(monkeypatch, 0.0, 0.99)
    ctx = _ctx(content=f"!steal {PROPERTIES_BY_ID[pid]['name']}")
    cog = EconomyCog(bot=_StubBot())
    await cog.cmd_steal.callback(cog, ctx, target=PROPERTIES_BY_ID[pid])
    return ctx


def _ping(ctx):
    return next(m for m in ctx.sent_messages if f"<@{OWNER}>" in m)


async def test_auto_repair_fixes_a_robbed_deed_at_once(db, monkeypatch):
    await _setup(PID, wallet=100_000)
    _state.economy["users"][str(OWNER)]["auto_repair"] = True
    ctx = await _rob(monkeypatch)

    share = pt.theft_take(PID, _state.property_owners[PID])
    assert not pt.is_damaged(_state.property_owners[PID])
    assert await get_balance(OWNER) == 100_000 - share
    assert "Auto-repair fixed it" in _ping(ctx)
    assert not any(isinstance(v, RepairView) for v in ctx.sent_views)
    user = _state.economy["users"][str(OWNER)]
    assert user["robbed_since_claim"] == 1 and user["auto_repaired_since_claim"] == share


async def test_auto_repair_waits_for_the_daily_claim_and_never_uses_savings(db, monkeypatch):
    await _setup(PID, wallet=0)
    _state.economy["users"][str(OWNER)]["auto_repair"] = True
    await _economy.add_savings(OWNER, 1_000_000)
    ctx = await _rob(monkeypatch)

    assert pt.is_damaged(_state.property_owners[PID])
    assert "tries again at your next daily claim" in _ping(ctx)
    assert any(isinstance(v, RepairView) for v in ctx.sent_views)

    await add_balance(OWNER, 100_000)
    report = await claim_report(OWNER)

    assert not pt.is_damaged(_state.property_owners[PID])
    assert "robbed **1** time" in report and "Auto-repair paid" in report


async def test_auto_repair_keeps_what_the_dailies_stake_needs(db):
    await _setup(PID, wallet=0, damaged_at=time.time(), repair_cost=5_000)
    _state.economy["users"][str(OWNER)]["auto_repair"] = True
    await add_balance(OWNER, 6_000)

    assert await auto_repair(OWNER, keep=2_000) == ([], 0)
    assert await auto_repair(OWNER, keep=1_000) == ([PID], 5_000)
    assert await get_balance(OWNER) == 1_000


async def test_auto_repair_off_does_nothing(db):
    await _setup(PID, wallet=100_000, damaged_at=time.time(), repair_cost=5_000)

    assert await auto_repair(OWNER) == ([], 0)
    assert pt.is_damaged(_state.property_owners[PID])


async def test_autorepair_on_repairs_whats_damaged_now(db):
    await _setup(PID, wallet=100_000, damaged_at=time.time(), repair_cost=5_000)
    ctx = FakeCtx(author=OWNER_MEMBER, guild=FakeGuild(gid=GID))
    cog = AssetsCog(bot=None)

    await cog.assets_autorepair.callback(cog, ctx, "on")

    assert _state.economy["users"][str(OWNER)]["auto_repair"] is True
    assert not pt.is_damaged(_state.property_owners[PID])
    assert "Repaired now" in ctx.sent_embeds[-1].description

    await cog.assets_autorepair.callback(cog, ctx, "off")
    assert _state.economy["users"][str(OWNER)]["auto_repair"] is False


async def test_bare_autorepair_asks(db, monkeypatch):
    await _setup(PID)
    picked = {}

    async def _choice(ctx, *, choices, **kwargs):
        picked["default"] = next(c["value"] for c in choices if c.get("default"))
        return True
    monkeypatch.setattr(_assets_cog, "confirm_choice", _choice)
    cog = AssetsCog(bot=None)
    await cog.assets_autorepair.callback(cog, FakeCtx(author=OWNER_MEMBER, guild=FakeGuild(gid=GID)))

    assert picked["default"] is False
    assert _state.economy["users"][str(OWNER)]["auto_repair"] is True


# ── the daily claim ──────────────────────────────────────────────────────────

async def test_claim_reports_investigators_and_damage_then_zeroes(db):
    await _setup(PID, wallet=0, damaged_at=time.time(), repair_cost=5_000)
    user = _state.economy["users"][str(OWNER)]
    user.update(robbed_since_claim=2, investigations_since_claim=3, arrests_since_claim=1)

    report = await claim_report(OWNER)

    assert "robbed **2** times" in report
    assert "answered **3** alarms and caught **1** thief" in report
    assert "**1** damaged property is losing" in report
    again = await claim_report(OWNER)
    assert "robbed" not in again and "Investigators" not in again
    assert "damaged property" in again


async def test_daily_carries_the_report(db):
    await _setup(PID, wallet=0)
    _state.economy["users"][str(OWNER)]["robbed_since_claim"] = 1
    ctx = FakeCtx(author=OWNER_MEMBER, guild=FakeGuild(gid=GID))
    cog = EconomyCog(bot=_StubBot())

    await cog.cmd_daily.callback(cog, ctx)

    assert "robbed **1** time since your last claim" in ctx.sent_embeds[-1].description


async def test_theft_tallies_investigators_on_the_owner(db, monkeypatch):
    await _setup(PID, secured=True)
    _insure(OWNER)
    _rolls(monkeypatch, 0.99, 0.0)  # failed and jailed
    cog = EconomyCog(bot=_StubBot())
    await cog.cmd_steal.callback(cog, _ctx(content="!steal Movie Theater"), target=PROPERTIES_BY_ID[PID])

    user = _state.economy["users"][str(OWNER)]
    assert user["investigations_since_claim"] == 1 and user["arrests_since_claim"] == 1
    assert not user.get("robbed_since_claim")


async def test_the_new_columns_persist(db):
    await _economy._ensure_user(OWNER)
    _state.economy["users"][str(OWNER)].update(auto_repair=True, robbed_since_claim=4, auto_repaired_since_claim=900)
    await _persistence.save_economy(uid=OWNER)
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT auto_repair, robbed_since_claim, auto_repaired_since_claim FROM economy_users WHERE user_id=?",
                (OWNER,))
            assert tuple(await cur.fetchone()) == (1, 4, 900)


# ── bare !steal: Choose target ───────────────────────────────────────────────

async def _pick_target(view, ctx, uid):
    opened = _press()
    await _child(view, "Choose target" if view.target is None else "Change target").callback(opened)
    modal = opened.response.send_modal.await_args.args[0]
    modal._inputs["user"]._values = [SimpleNamespace(id=uid)]
    submit = _press()
    submit.guild = ctx.guild
    await modal.on_submit(submit)
    return submit


async def test_bare_steal_asks_for_a_target_before_the_menu(db, panel):
    await _setup(PID)
    ctx = _ctx(content="!steal")
    cog = EconomyCog(bot=_StubBot())
    await cog.cmd_steal.callback(cog, ctx, target=None)
    view = ctx.sent_views[-1]
    assert [c.label for c in view.children] == ["Choose target", "Cancel"]

    submit = await _pick_target(view, ctx, OWNER)
    assert {c.label for c in view.children} == {"Wallet", "Property", "Change target", "Cancel"}
    assert "owner" in _drawn(submit).title


async def test_a_refused_target_keeps_the_choose_button_and_says_why(db, panel):
    await _setup(PID)
    ctx = _ctx(content="!steal")
    cog = EconomyCog(bot=_StubBot())
    await cog.cmd_steal.callback(cog, ctx, target=None)
    view = ctx.sent_views[-1]
    ctx.guild.members.append(ctx.author)

    submit = await _pick_target(view, ctx, THIEF)  # yourself
    assert view.target is None and [c.label for c in view.children] == ["Choose target", "Cancel"]
    assert "yourself" in _drawn(submit).fields[0].value
