"""Persistence round-trip tests: write state -> save -> clear -> load -> assert.

These tests use the opt-in `db` fixture (see tests/conftest.py), which swaps
in an in-memory SQLite for `src.db.get_pool` and restores the real save_*/
load_* functions. They exercise the actual SQL strings in src/persistence.py;
a typo in a column name will fail a test here.
"""
import json

import pytest
import pytest_asyncio

import src as bot
import src.state as _state
import src.persistence as _persistence


pytestmark = pytest.mark.asyncio


# ── economy ───────────────────────────────────────────────────────────────────

async def test_economy_users_roundtrip(db):
    _state.economy["users"]["1001"] = {
        "balance": 5000,
        "last_daily": 1234.5,
        "daily_date": "2026-05-01",
        "scratch_used": 2,
        "scratch_date": "2026-05-02",
        "jailbreak_used": True,
        "jail_until": 0.0,
        "savings": [{"amount": 100, "deposited_at": 999.0}],
    }
    _state.economy["users"]["1002"] = {
        "balance": 0,
        "last_daily": 0.0,
        "daily_date": None,
        "scratch_used": 0,
        "scratch_date": None,
        "jailbreak_used": False,
        "jail_until": 0.0,
        "savings": [],
    }
    _state.economy["last_daily_reset"] = "2026-05-02"

    await _persistence.save_economy()

    # Wipe in-memory state and reload from DB
    _state.economy["users"].clear()
    _state.economy["last_daily_reset"] = None
    await _persistence.init_db_state()

    assert "1001" in _state.economy["users"]
    u = _state.economy["users"]["1001"]
    assert u["balance"] == 5000
    assert u["last_daily"] == 1234.5
    assert u["daily_date"] == "2026-05-01"
    assert u["scratch_used"] == 2
    assert u["scratch_date"] == "2026-05-02"
    assert u["jailbreak_used"] is True
    assert u["savings"] == [{"amount": 100, "deposited_at": 999.0}]
    assert _state.economy["users"]["1002"]["balance"] == 0
    assert _state.economy["last_daily_reset"] == "2026-05-02"


async def test_economy_targeted_save_writes_only_one_user(db):
    _state.economy["users"]["1"] = {"balance": 100, "last_daily": 0.0}
    _state.economy["users"]["2"] = {"balance": 200, "last_daily": 0.0}
    await _persistence.save_economy()  # bulk: both rows in DB

    # Mutate in memory but only save uid=1
    _state.economy["users"]["1"]["balance"] = 999
    _state.economy["users"]["2"]["balance"] = 999
    await _persistence.save_economy(uid=1)

    # Reload — uid 1 should reflect 999, uid 2 should still be 200
    _state.economy["users"].clear()
    await _persistence.init_db_state()
    assert _state.economy["users"]["1"]["balance"] == 999
    assert _state.economy["users"]["2"]["balance"] == 200


async def test_guild_house_roundtrip(db):
    from src.economy import add_guild_house
    await add_guild_house(42, 1500)
    await add_guild_house(42, 250)
    await add_guild_house(99, 7777)

    _state.economy["guild_house"].clear()
    await _persistence.init_db_state()
    assert _state.economy["guild_house"]["42"] == 1750
    assert _state.economy["guild_house"]["99"] == 7777


# ── insurance ─────────────────────────────────────────────────────────────────

async def test_insurance_delete_and_replace(db):
    import time
    future = time.time() + 3600

    _state.insurance["1"] = {
        "expires_at": future,
        "protected_from": ["nickname", "curse"],
    }
    _state.insurance["2"] = {
        "expires_at": future,
        "protected_from": ["tax"],
    }
    await _persistence.save_insurance()

    # Drop user 2, save again — DB should reflect the deletion (save_insurance
    # does DELETE FROM shop_insurance + re-insert).
    del _state.insurance["2"]
    await _persistence.save_insurance()

    _state.insurance.clear()
    await _persistence.init_db_state()
    assert "1" in _state.insurance
    assert "2" not in _state.insurance
    assert _state.insurance["1"]["protected_from"] == ["nickname", "curse"]


# ── lottery ───────────────────────────────────────────────────────────────────

async def test_lottery_roundtrip(db):
    payload = {
        "prize_pool": 5000,
        "last_posted_week": 18,
        "last_drawn_week": 17,
        "players": {"1001": 5, "1002": 3},
    }
    await _persistence.save_lottery(42, payload)

    loaded = await _persistence.load_lottery(42)
    assert loaded["prize_pool"] == 5000
    assert loaded["last_posted_week"] == 18
    assert loaded["last_drawn_week"] == 17
    assert loaded["players"] == {"1001": 5, "1002": 3}


async def test_lottery_no_players_round_trip_and_redraw_guard(db):
    """Edge case: a week ends with prize pool but zero ticket-buyers.

    Two things must hold for the scheduler in src/cogs/lottery_cog.py to behave:
    1. An empty `players` dict must round-trip cleanly (not become None or
       crash load_lottery — the scheduler keys off `if players and pool > 0`).
    2. After a no-player week, saving lottery with the new `last_drawn_week`
       must persist; otherwise the scheduler would re-trigger the draw branch
       every minute for the rest of the day.
    """
    # Week 17 ended with 5000 in the pool and no buyers.
    abandoned = {
        "prize_pool": 5000,
        "players": {},
        "last_posted_week": 16,
        "last_drawn_week": 16,  # NOT yet drawn for week 17
    }
    await _persistence.save_lottery(7, abandoned)

    loaded = await _persistence.load_lottery(7)
    assert loaded["players"] == {}
    assert loaded["prize_pool"] == 5000
    # Scheduler's guard: skips payout when this is False.
    assert not (loaded["players"] and loaded["prize_pool"] > 0)

    # Scheduler now resets the week. The seed (2000) replaces the pool because
    # no one won, last_drawn_week advances to 17, players cleared.
    reset = {"prize_pool": 2000, "players": {}, "last_drawn_week": 17, "last_posted_week": 0}
    await _persistence.save_lottery(7, reset)

    reloaded = await _persistence.load_lottery(7)
    assert reloaded["players"] == {}
    assert reloaded["prize_pool"] == 2000
    assert reloaded["last_drawn_week"] == 17  # redraw guard is now armed

    # Sanity: hand the loaded lottery back to drain_bot_balance_into_lottery
    # the way the scheduler does — empty house, pool unchanged.
    from src.economy import drain_bot_balance_into_lottery
    transferred = await drain_bot_balance_into_lottery(reloaded, 7)
    assert transferred == 0
    assert reloaded["prize_pool"] == 2000


async def test_lottery_save_replaces_players(db):
    await _persistence.save_lottery(1, {
        "prize_pool": 100, "last_posted_week": 0, "last_drawn_week": 0,
        "players": {"100": 1, "200": 2, "300": 3},
    })
    # Now save with only one player — old players for this guild should be gone
    await _persistence.save_lottery(1, {
        "prize_pool": 100, "last_posted_week": 0, "last_drawn_week": 0,
        "players": {"100": 5},
    })
    loaded = await _persistence.load_lottery(1)
    assert loaded["players"] == {"100": 5}


# ── records ───────────────────────────────────────────────────────────────────

async def test_records_roundtrip_and_extra_meta(db):
    records = {
        "highest_balance": {"value": 9999, "holder_id": 1, "holder_name": "alice"},
        "best_streak": {
            "value": 7, "holder_id": 2, "holder_name": "bob",
            "set_at": "2026-04-01",
        },
    }
    await _persistence.save_records(99, records)
    loaded = await _persistence.load_records(99)
    assert loaded["highest_balance"]["value"] == 9999
    assert loaded["highest_balance"]["holder_name"] == "alice"
    assert loaded["best_streak"]["set_at"] == "2026-04-01"


async def test_try_set_record_only_updates_when_higher(db):
    ok1 = await _persistence.try_set_record(7, "score", 100, 1, "alice")
    assert ok1 is True

    # Lower value: should NOT update
    ok2 = await _persistence.try_set_record(7, "score", 50, 2, "bob")
    assert ok2 is False
    loaded = await _persistence.load_records(7)
    assert loaded["score"]["value"] == 100
    assert loaded["score"]["holder_name"] == "alice"

    # Higher value: should update
    ok3 = await _persistence.try_set_record(7, "score", 200, 3, "carol")
    assert ok3 is True
    loaded = await _persistence.load_records(7)
    assert loaded["score"]["value"] == 200
    assert loaded["score"]["holder_name"] == "carol"


# ── balance history ───────────────────────────────────────────────────────────

async def test_balance_history_roundtrip(db):
    history = {
        "2026-04-30": {"1": {"wallet": 100, "savings": 50}},
        "2026-05-01": {"1": {"wallet": 110, "savings": 55}, "2": {"wallet": 0, "savings": 0}},
    }
    await _persistence.save_balance_history(history)
    loaded = await _persistence.load_balance_history()
    assert loaded == history


# ── command perms ─────────────────────────────────────────────────────────────

async def test_command_perms_roundtrip(db):
    _state.command_perms.clear()
    _state.command_perms["godmode"] = {"tier": "bot_admin", "hidden": True}
    _state.command_perms["balance"] = {"tier": "everyone", "hidden": False}
    await _persistence.save_command_perms()

    # Read back via SQL directly — init_db_state's command_perms branch also
    # tries to load JSON file from disk, which would muddy the assertion.
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT command_name, tier, hidden FROM command_perms")
            rows = await cur.fetchall()
    by_name = {r[0]: (r[1], bool(r[2])) for r in rows}
    assert by_name["godmode"] == ("bot_admin", True)
    assert by_name["balance"] == ("everyone", False)


# ── gambler streak ────────────────────────────────────────────────────────────

async def test_gambler_streak_roundtrip(db):
    _state.gambler_streak.clear()
    _state.gambler_streak["1"] = {"date": "2026-05-01", "count": 3}
    _state.gambler_streak["2"] = {"date": "2026-04-29", "count": 1}
    await _persistence.save_gambler_streak()

    _state.gambler_streak.clear()
    await _persistence.init_db_state()
    assert _state.gambler_streak["1"] == {"date": "2026-05-01", "count": 3}
    assert _state.gambler_streak["2"] == {"date": "2026-04-29", "count": 1}


# ── jackpot ───────────────────────────────────────────────────────────────────

async def test_jackpot_roundtrip(db):
    await _persistence.save_jackpot(42424)
    # Wipe in-memory then reload
    _state.slot_jackpot = 0
    await _persistence.init_db_state()
    assert _state.slot_jackpot == 42424
