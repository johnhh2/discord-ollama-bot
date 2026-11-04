"""The bot-wide interest curve (src/rate_curves.py): the falling rates, what
they pay, and that a change never re-prices the past."""
import json
import time
from unittest.mock import AsyncMock

import pytest

import src.cogs.settings_cog as _settings_cog
import src.economy as _economy
import src.state as _state
from src.cogs.settings_cog import SettingsCog
from src.economy import SAVINGS_DAILY_MULT, savings_growth, set_rate_curve, user_savings_daily_pct
from src.properties import (
    PROPERTIES, claimable_property_revenue, daily_revenue, portfolio_daily_revenue,
    property_daily_revenue,
)
from src.rate_curves import (
    ARTIFACT_SAVINGS_FACTOR, DAY_SECS, FLAT_PCT, MODE_KEY, RATE_CURVES, STARTED_KEY, active_curve,
    curve_growth, curve_status, parse_position, property_pct_text, rate_at, savings_pct_text,
)
from src.settings_hub import items_for

from tests.fakes.discord import FakeCtx, FakeGuild, FakeMember

pytestmark = pytest.mark.asyncio

UID = 7
ADMIN = 1


def _start_curve(mode: str, days_ago: float = 0.0):
    _state.bot_settings[MODE_KEY] = mode
    _state.bot_settings[STARTED_KEY] = str(time.time() - days_ago * DAY_SECS)


async def _deposit(uid: int, amount: float, days_ago: float):
    await _economy._ensure_user(uid)
    _state.economy["users"][str(uid)]["savings"] = [
        {"amount": amount, "deposited_at": time.time() - days_ago * DAY_SECS},
    ]


def _value(uid: int = UID) -> float:
    now = time.time()
    return sum(
        e["amount"] * savings_growth(e["deposited_at"], now, uid)
        for e in _state.economy["users"][str(uid)]["savings"]
    )


async def _own(uid: int, days_ago: float = 30.0) -> dict:
    prop = PROPERTIES[0]
    await _economy._ensure_user(uid)
    _state.property_owners[prop["id"]] = {
        "owner_id": uid, "acquired_at": time.time() - days_ago * DAY_SECS,
        "list_price": None, "listed_at": None, "upgraded": False, "custom_name": None,
    }
    return prop


def _admin_ctx(command: str = "settings interest") -> FakeCtx:
    _state.bot_admins.add(ADMIN)
    return FakeCtx(author=FakeMember(ADMIN, administrator=True), guild=FakeGuild(gid=42), command_name=command)


# ── the catalog ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode, days, savings, prop", [
    ("small", 730, (1.0, 0.3), (2.5, 0.5)),
    ("medium", 365, (0.8, 0.2), (2.0, 0.4)),
    ("large", 182, (0.6, 0.1), (1.5, 0.3)),
])
async def test_each_mode_falls_from_start_to_end_and_holds(mode, days, savings, prop):
    curve = RATE_CURVES[mode]
    assert curve.days == days
    assert curve.pct("savings", 0) == savings[0] and curve.pct("property", 0) == prop[0]
    assert curve.pct("savings", days / 2) == pytest.approx(sum(savings) / 2)
    assert curve.pct("savings", days) == pytest.approx(savings[1])
    assert curve.pct("property", days * 10) == pytest.approx(prop[1])


async def test_menu_labels_carry_the_player_ranges():
    assert [c.label for c in RATE_CURVES.values()] == [
        "Small (2-9 players)", "Medium (10-99 players)", "Large (100+ players)",
    ]


# ── flat until picked ────────────────────────────────────────────────────────

async def test_no_curve_keeps_the_flat_math_exactly():
    assert active_curve() is None
    start = time.time() - 10 * DAY_SECS
    assert savings_growth(start, start + 10 * DAY_SECS, UID) == pytest.approx(SAVINGS_DAILY_MULT ** 10)
    assert user_savings_daily_pct(UID) == savings_pct_text() == "0.60%"
    assert property_pct_text() == "1.1%"
    assert curve_status() == "flat rates"


async def test_the_curve_counts_only_from_its_start():
    _start_curve("small")
    curve = active_curve()
    now = time.time()
    before = now - 5 * DAY_SECS
    assert rate_at(curve, "savings", before) == pytest.approx(FLAT_PCT["savings"])
    assert curve_growth(curve, before, now - 4 * DAY_SECS) == pytest.approx(SAVINGS_DAILY_MULT)


# ── savings ──────────────────────────────────────────────────────────────────

async def test_savings_compound_at_the_falling_rate():
    _start_curve("small", days_ago=2)
    await _deposit(UID, 100_000, days_ago=1)
    # Day two of 730 falls about 0.0014 percentage points from 1%.
    assert _value() == pytest.approx(100_000 * 1.00998, rel=1e-4)


async def test_the_end_rate_holds_past_the_curve():
    _start_curve("large", days_ago=400)
    await _deposit(UID, 100_000, days_ago=10)
    assert _value() == pytest.approx(100_000 * 1.001 ** 10, rel=1e-6)


async def test_growth_over_a_span_is_the_product_of_its_parts():
    _start_curve("medium", days_ago=100)
    curve = active_curve()
    now = time.time()
    a, b, c = now - 50 * DAY_SECS, now - 20.3 * DAY_SECS, now
    assert curve_growth(curve, a, c) == pytest.approx(curve_growth(curve, a, b) * curve_growth(curve, b, c))


async def test_the_artifact_keeps_its_proportion_from_its_purchase():
    _start_curve("large", days_ago=400)
    await _deposit(UID, 100_000, days_ago=10)
    _state.user_artifacts.setdefault(UID, {})["savings_rate_boost"] = 1
    _state.user_artifact_acquired_at.setdefault(UID, {})["savings_rate_boost"] = time.time() - 5 * DAY_SECS
    boosted = 0.1 * ARTIFACT_SAVINGS_FACTOR
    assert _value() == pytest.approx(100_000 * 1.001 ** 5 * (1 + boosted / 100) ** 5, rel=1e-6)
    assert user_savings_daily_pct(UID) == "0.13%"


# ── property ─────────────────────────────────────────────────────────────────

async def test_rent_is_paid_at_the_curve_rate():
    _start_curve("small")
    prop = await _own(UID)
    flat = daily_revenue(prop["cost"])
    _state.economy["users"][str(UID)]["property_paid_at"] = time.time() - DAY_SECS
    expected = int(flat * 2.5 / 1.1)
    assert claimable_property_revenue(UID) == pytest.approx(expected, abs=2)
    assert portfolio_daily_revenue(UID) == pytest.approx(expected, abs=2)
    assert property_daily_revenue(prop["id"], current=True) == pytest.approx(expected, abs=2)
    assert property_daily_revenue(prop["id"]) == flat


async def test_each_skipped_day_pays_that_days_rate():
    _start_curve("large", days_ago=100)
    prop = await _own(UID)
    _state.economy["users"][str(UID)]["property_paid_at"] = time.time() - 3 * DAY_SECS
    flat = daily_revenue(prop["cost"])
    pct = [1.5 - 1.2 * (100 - back) / 182 for back in (0, 1, 2)]
    assert claimable_property_revenue(UID) == pytest.approx(sum(flat * p / 1.1 for p in pct), abs=4)


# ── changes reach forward only ───────────────────────────────────────────────

async def test_the_clock_survives_off_and_on_and_a_change_of_mode():
    await set_rate_curve("small")
    started = _state.bot_settings[STARTED_KEY]
    await set_rate_curve(None)
    assert active_curve() is None
    await set_rate_curve("large")
    assert active_curve() == (float(started), "large")


async def test_setting_the_same_mode_is_a_noop():
    await set_rate_curve("small")
    assert await set_rate_curve("small") is False


async def test_the_first_pick_keeps_deposit_dates():
    await _deposit(UID, 100_000, days_ago=10)
    dated = _state.economy["users"][str(UID)]["savings"][0]["deposited_at"]
    await set_rate_curve("small")
    assert _state.economy["users"][str(UID)]["savings"][0]["deposited_at"] == dated


async def test_a_mode_change_does_not_reprice_earned_interest():
    _start_curve("small", days_ago=300)
    await _deposit(UID, 100_000, days_ago=200)
    before = _value()
    await set_rate_curve("large")
    assert _value() == pytest.approx(before, rel=1e-5)
    await set_rate_curve(None)
    assert _value() == pytest.approx(before, rel=1e-5)


async def test_off_and_on_does_not_pay_the_curve_for_the_off_time():
    _start_curve("small", days_ago=300)
    await _deposit(UID, 100_000, days_ago=200)
    await set_rate_curve(None)
    before = _value()
    await set_rate_curve("small")
    assert _value() == pytest.approx(before, rel=1e-5)


async def test_the_setting_is_a_text_value():
    await set_rate_curve("small")
    assert all(isinstance(_state.bot_settings[k], str) for k in (MODE_KEY, STARTED_KEY))


# ── starting partway along ───────────────────────────────────────────────────

@pytest.mark.parametrize("text, days", [("274", 274.0), ("75%", 273.75), ("0", 0.0), ("100%", 365.0)])
async def test_parse_position_reads_days_and_percents(text, days):
    assert parse_position(text, RATE_CURVES["medium"]) == days


@pytest.mark.parametrize("text", ["366", "101%", "-1", "soon", "%"])
async def test_parse_position_refuses_what_is_off_the_curve(text):
    assert parse_position(text, RATE_CURVES["medium"]) is None


async def test_medium_three_quarters_in_pays_the_quoted_rates():
    await set_rate_curve("medium", parse_position("75%", RATE_CURVES["medium"]))
    assert property_pct_text() == "0.8%"
    assert savings_pct_text() == "0.35%"
    assert "day 274 of 365" in curve_status()


async def test_a_backdated_start_does_not_pay_the_curve_on_the_past():
    await _deposit(UID, 100_000, days_ago=100)
    before = _value()
    await set_rate_curve("small", 10)
    assert _value() == pytest.approx(before, rel=1e-5)


async def test_a_named_position_moves_the_clock():
    await set_rate_curve("medium", 100)
    assert await set_rate_curve("medium", 200) is True
    assert (time.time() - active_curve()[0]) / DAY_SECS == pytest.approx(200, abs=1e-3)


async def test_the_command_takes_a_position_and_refuses_a_bad_one():
    cog = SettingsCog(None)
    ctx = _admin_ctx()
    await cog.settings_interest.callback(cog, ctx, "medium", "50%")
    assert (time.time() - active_curve()[0]) / DAY_SECS == pytest.approx(182.5, abs=1e-3)
    await cog.settings_interest.callback(cog, ctx, "medium", "999")
    assert "Usage" in ctx.sent_embeds[-1].description
    assert (time.time() - active_curve()[0]) / DAY_SECS == pytest.approx(182.5, abs=1e-3)


# ── the command ──────────────────────────────────────────────────────────────

async def test_settings_interest_sets_validates_and_prompts(monkeypatch):
    cog = SettingsCog(None)
    ctx = _admin_ctx()

    await cog.settings_interest.callback(cog, ctx, "Small")
    assert active_curve()[1] == "small"
    await cog.settings_interest.callback(cog, ctx, "huge")
    assert active_curve()[1] == "small" and "Usage" in ctx.sent_embeds[-1].description

    seen = {}

    async def _pick(ctx, *, choices, **kwargs):
        seen["labels"] = [c["label"] for c in choices]
        return "off"
    monkeypatch.setattr(_settings_cog, "confirm_choice", _pick)
    await cog.settings_interest.callback(cog, ctx)
    assert seen["labels"] == ["Small (2-9 players)", "Medium (10-99 players)", "Large (100+ players)", "Flat rates"]
    assert active_curve() is None


async def test_settings_interest_is_global_admin_only():
    perms = json.load(open("src/command_perms.json", encoding="utf-8"))
    assert perms["settings interest"]["tier"] == "global_admin"


async def test_server_admins_cannot_set_it():
    cog = SettingsCog(None)
    ctx = FakeCtx(author=FakeMember(2, administrator=True), guild=FakeGuild(gid=42), command_name="settings interest")
    _state.command_perms["settings interest"] = {"tier": "global_admin", "hidden": True}
    await cog.settings_interest.callback(cog, ctx, "small")
    assert active_curve() is None


async def test_the_panel_lists_it_for_global_admins_only():
    ctx = _admin_ctx("settings")
    assert any(item.key == "interest" for item in items_for("games", ctx))
    other = FakeCtx(author=FakeMember(2, administrator=True), guild=FakeGuild(gid=42), command_name="settings")
    assert not any(item.key == "interest" for item in items_for("games", other))


async def test_the_pick_is_saved(monkeypatch):
    save = AsyncMock()
    monkeypatch.setattr("src.persistence.save_bot_settings", save)
    await set_rate_curve("small")
    save.assert_awaited_once()
