"""Insurance types (migration 0086): personal refunds crime, social blocks
the shop effects, property waits for property theft.

Covers the typed policy stores, the sweep charging every subscription a
user holds, the `!shop insurance` menu, the typed type word, the shop panel
items, the save/load round trip and the 0086 grandfathering of social cover.
"""
import sqlite3
import time as _time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

import src.state as _state
import src.persistence as _persistence
import src.migrations as _migrations
from src.cogs.shop_cog import ShopCog
from src.config import (
    SHOP_INSURANCE_TIERS, SHOP_SOCIAL_INSURANCE_TIERS, SHOP_INSURANCE_DURATION_SECS,
)
from src.economy import (
    add_balance, get_balance, is_insured, get_insurance_expiry, insurance_refund,
    renew_insurance_subs, sweep_insurance_subs,
)
from src.insurance_view import _TypeSelect
from src.shop_hub import items_for
from tests.fakes.db import FakeCursor
from tests.fakes.discord import FakeCtx, FakeMember, FakeGuild, FakeMessage

DAY = SHOP_INSURANCE_DURATION_SECS
BASIC = SHOP_INSURANCE_TIERS["basic"]["cost"]
SOCIAL = SHOP_SOCIAL_INSURANCE_TIERS["social"]["cost"]


def _ctx(uid: int) -> FakeCtx:
    return FakeCtx(author=FakeMember(uid=uid), guild=FakeGuild(gid=42))


# ── what each type does ──────────────────────────────────────────────────────

async def test_personal_insurance_no_longer_blocks_shop_effects():
    _state.insurance[1] = {"expires_at": _time.time() + 3600, "protected_from": [], "tier": "premium"}
    assert await is_insured(1, "mock") is False


async def test_social_insurance_blocks_shop_effects_but_refunds_no_crime(db):
    _state.social_insurance[2] = {
        "expires_at": _time.time() + 3600, "protected_from": ["mock"], "tier": "social",
    }
    assert await is_insured(2, "mock") is True
    assert await insurance_refund(2, 10_000) == 0


async def test_buying_social_insurance_leaves_personal_alone(db):
    cog = ShopCog(bot=None)
    uid = 9101
    await add_balance(uid, SOCIAL)

    await cog.shop_insurance.callback(cog, _ctx(uid), "social")

    assert await get_balance(uid) == 0
    assert await is_insured(uid, "tax") is True
    assert get_insurance_expiry(uid, "social") is not None
    assert uid not in _state.insurance


async def test_social_subscription_charges_the_first_day(db):
    cog = ShopCog(bot=None)
    uid = 9102
    await add_balance(uid, SOCIAL + 5)

    await cog.shop_insurance.callback(cog, _ctx(uid), "social", "sub")

    assert _state.social_insurance_subs[uid] == "social"
    assert await get_balance(uid) == 5
    assert uid not in _state.insurance_subs


async def test_property_insurance_is_not_sold_yet(db):
    cog = ShopCog(bot=None)
    uid = 9103
    await add_balance(uid, 50_000)
    ctx = _ctx(uid)

    await cog.shop_insurance.callback(cog, ctx, "property", "premium", "sub")

    assert await get_balance(uid) == 50_000
    assert uid not in _state.property_insurance_subs
    assert ctx.sent_embeds[-1].title == "🏠 Property Insurance"


# ── the 5am sweep ────────────────────────────────────────────────────────────

async def test_renewal_charges_every_subscription_a_user_holds(db):
    uid = 9110
    await add_balance(uid, BASIC + SOCIAL)
    _state.insurance_subs[uid] = "basic"
    _state.social_insurance_subs[uid] = "social"

    charged, lapsed = await renew_insurance_subs(uid)

    assert (charged, lapsed) == (BASIC + SOCIAL, 0)
    assert get_insurance_expiry(uid) is not None
    assert get_insurance_expiry(uid, "social") is not None
    assert _state.social_insurance[uid]["protected_from"]


async def test_renewal_lapses_only_the_one_it_cannot_afford(db):
    """Types renew in catalog order, so with one premium's worth the personal
    renews and the social lapses."""
    uid = 9111
    await add_balance(uid, BASIC)
    _state.insurance_subs[uid] = "basic"
    _state.social_insurance_subs[uid] = "social"

    charged, lapsed = await renew_insurance_subs(uid)

    assert (charged, lapsed) == (BASIC, 1)
    assert get_insurance_expiry(uid, "social") is None
    assert uid in _state.social_insurance_subs


async def test_sweep_reaches_a_social_only_subscriber(db, monkeypatch):
    uid = 9112
    await add_balance(uid, SOCIAL)
    _state.social_insurance_subs[uid] = "social"
    _state.economy["last_insurance_sweep"] = "2000-01-01"

    await sweep_insurance_subs()

    assert get_insurance_expiry(uid, "social") is not None
    assert _state.economy["users"][str(uid)]["ins_paid_since_claim"] == SOCIAL


# ── the menu ─────────────────────────────────────────────────────────────────

def _interaction(uid: int):
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock())
    return SimpleNamespace(user=FakeMember(uid=uid), guild=SimpleNamespace(id=42), message=None,
                           response=response)


async def _open_menu(uid: int):
    ctx = _ctx(uid)
    cog = ShopCog(bot=None)
    await cog.shop_insurance.callback(cog, ctx)
    return ctx, ctx.sent_views[-1]


async def _pick_type(view, kind: str):
    select = next(i for i in view.children if isinstance(i, _TypeSelect))
    select._values = [kind]
    await select.callback(_interaction(view.owner.id))


def _buttons(view):
    return [b for b in view.children if isinstance(b, discord.ui.Button)]


async def test_bare_insurance_shows_coverage_not_a_tier_list(db):
    _state.insurance_subs[9120] = "standard"
    _state.insurance[9120] = {"expires_at": _time.time() + 3600, "protected_from": [], "tier": "standard"}
    ctx, view = await _open_menu(9120)

    desc = ctx.sent_embeds[-1].description
    assert "Personal insurance" in desc and "Social insurance" in desc
    assert "**Standard**, **75%** of crime losses refunded" in desc and "renews at the 5am reset" in desc
    assert "**Basic**" not in desc and "**Premium**" not in desc
    assert "Subscription:" not in desc
    select = next(i for i in view.children if isinstance(i, _TypeSelect))
    assert [o.value for o in select.options] == ["personal", "social"]


async def test_cancelled_coverage_says_when_it_ends(db):
    _state.insurance[9123] = {"expires_at": _time.time() + 3600, "protected_from": [], "tier": "basic"}
    ctx, _ = await _open_menu(9123)
    assert "cancelled, ends <t:" in ctx.sent_embeds[-1].description


async def test_a_type_pick_redraws_the_same_message_with_tier_buttons_and_back(db):
    ctx, view = await _open_menu(9124)
    await _pick_type(view, "personal")

    assert len(ctx.sent_embeds) == 1
    labels = [b.label for b in _buttons(view)]
    assert labels[:3] == [
        f"Basic — {BASIC:,} 🪙/day ({BASIC:,} now) · 50% up to 100k",
        "Standard — 3,000 🪙/day (3,000 now) · 75% up to 200k",
        "Premium — 6,000 🪙/day (6,000 now) · 100% up to 400k",
    ]
    assert labels[-1] == "Back" and "Cancel subscription" not in labels
    assert "Personal" in view.embed().title


async def test_a_tier_press_subscribes_and_shows_the_result_in_place(db):
    uid = 9121
    await add_balance(uid, SOCIAL)
    ctx, view = await _open_menu(uid)
    view.message = FakeMessage()
    await _pick_type(view, "social")

    await _buttons(view)[0].callback(_interaction(uid))

    assert _state.social_insurance_subs[uid] == "social"
    assert await get_balance(uid) == 0
    assert len(ctx.sent_embeds) == 1
    assert view.embed().fields[0].name == "🛡️ Insurance Subscribed"
    current = _buttons(view)[0]
    assert current.disabled
    assert "Cancel subscription" in [b.label for b in _buttons(view)]


async def test_cancel_and_back(db):
    uid = 9122
    _state.insurance_subs[uid] = "standard"
    ctx, view = await _open_menu(uid)
    view.message = FakeMessage()
    await _pick_type(view, "personal")

    await next(b for b in _buttons(view) if b.label == "Cancel subscription").callback(_interaction(uid))
    assert uid not in _state.insurance_subs
    assert view.embed().fields[0].name == "🛡️ Insurance Unsubscribed"

    await next(b for b in _buttons(view) if b.label == "Back").callback(_interaction(uid))
    assert view.kind is None and not view.embed().fields


async def test_only_the_invoker_can_press(db):
    _, view = await _open_menu(9125)
    other = _interaction(1)
    assert await view.interaction_check(other) is False
    other.response.send_message.assert_awaited()


# ── the shop panel ───────────────────────────────────────────────────────────

def test_shop_panel_lists_personal_and_social_but_not_property():
    ctx = _ctx(9130)
    _state.social_insurance_subs[9130] = "social"
    keys = {item.key: item for item in items_for("insurance", ctx)}

    assert set(keys) == {"insurance", "social-insurance", "social-insurance-unsub"}
    assert keys["social-insurance"].args == ("social",)
    assert keys["social-insurance-unsub"].args == ("social", "unsub")


# ── persistence ──────────────────────────────────────────────────────────────

async def test_each_type_round_trips_through_the_db(db):
    future = _time.time() + 3600
    _state.insurance[1] = {"expires_at": future, "protected_from": [], "tier": "premium"}
    _state.insurance_subs[1] = "premium"
    _state.social_insurance[1] = {"expires_at": future, "protected_from": ["mock"], "tier": "social"}
    _state.social_insurance_subs[2] = "social"
    for kind in ("personal", "social"):
        await _persistence.save_insurance(kind)
        await _persistence.save_insurance_subs(kind)

    for store in (_state.insurance, _state.insurance_subs, _state.social_insurance, _state.social_insurance_subs):
        store.clear()
    await _persistence.init_db_state()

    assert _state.insurance[1]["tier"] == "premium" and _state.insurance[1]["protected_from"] == []
    assert _state.insurance_subs == {1: "premium"}
    assert _state.social_insurance[1]["tier"] == "social"
    assert "tax" in _state.social_insurance[1]["protected_from"]  # unioned with the current list
    assert _state.social_insurance_subs == {2: "social"}


# ── migration 0086 ───────────────────────────────────────────────────────────

async def test_0086_keeps_personal_rows_and_grants_no_social_cover(tmp_path):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    cur = FakeCursor(conn.cursor())
    (tmp_path / "0001_shop_effects.sql").write_text("""
        CREATE TABLE shop_effects (
            guild_id INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL,
            effect_type TEXT NOT NULL,
            expires_at REAL NULL,
            history_json TEXT NULL,
            insurance_tier TEXT NULL,
            PRIMARY KEY (guild_id, user_id, effect_type)
        );
    """, encoding="utf-8")
    _migrations._done = False
    await _migrations.run_migrations(migrations_dir=tmp_path, cur=cur)
    seed = [
        (0, 10, "insurance", 5_000.0, "[]", "premium"),
        (0, 11, "insurance_sub", None, None, "basic"),
    ]
    for row in seed:
        await cur.execute(
            "INSERT INTO shop_effects (guild_id, user_id, effect_type, expires_at, history_json, insurance_tier)"
            " VALUES (%s,%s,%s,%s,%s,%s)", row,
        )

    real_sql = (Path(__file__).parent.parent / "migrations" / "0086_split_insurance_types.sql").read_text(encoding="utf-8")
    (tmp_path / "0002_split_insurance_types.sql").write_text(real_sql, encoding="utf-8")
    _migrations._done = False
    for _ in range(2):  # the second run re-executes it: a retry must be a no-op
        await cur.execute("DELETE FROM schema_migrations WHERE version=2")
        _migrations._done = False
        await _migrations.run_migrations(migrations_dir=tmp_path, cur=cur)

    await cur.execute(
        "SELECT guild_id, user_id, effect_type, expires_at, history_json, insurance_tier"
        " FROM shop_effects ORDER BY user_id"
    )
    assert await cur.fetchall() == seed
    _migrations._done = False
    conn.close()
