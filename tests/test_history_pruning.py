"""Tests for the history-table retention policy.

The graph cog only renders the last 14 days, but the *_history tables
keep `GRAPH_HISTORY_RETENTION_DAYS` (10y) of data — headroom for future
"show me a year" features. levelup_history is never pruned (it grows
slowly enough that an unbounded table is fine).

These tests pin:
  - do_daily_reset DELETEs rows past the retention window from each of
    the 5 pruned tables (balance, bot_stats, bot_command_usage, crime,
    gambling).
  - do_daily_reset does NOT touch levelup_history.
  - The 5 prune helpers individually use a strict-less-than cutoff (rows
    ON the cutoff date are kept).

Snapshot helpers (snapshot_balances etc.) no longer prune in-line —
that's been moved to the daily DB-level DELETE for correctness (the
old in-memory load/filter/save pattern silently failed to actually
remove old DB rows).
"""
import datetime

import pytest

import src.economy as _economy
import src.persistence as _persistence


pytestmark = pytest.mark.asyncio


def _ancient_date_iso(years_back: int) -> str:
    return (_economy._ct_now().date() - datetime.timedelta(days=365 * years_back)).isoformat()


# ── do_daily_reset prunes all 5 pruned tables ────────────────────────────────


async def test_do_daily_reset_prunes_all_pruned_tables(db, monkeypatch):
    """One pass of do_daily_reset deletes rows past retention from
    balance, bot_stats, bot_command_usage, crime, and gambling."""
    very_old = _ancient_date_iso(11)
    recent = _economy._ct_now().date().isoformat()

    # Seed one ancient row in each pruned table.
    await _persistence.save_balance_history({
        very_old: {0: {"1": {"wallet": 999, "savings": 0}}},
    })
    await _persistence.save_bot_stats_history({
        very_old: {0: {"messages": 1, "commands": 1, "ai_responses": 0,
                       "ai_up": False, "memory_mb": 0}},
    })
    await _persistence.save_command_usage_history({
        very_old: {0: {"GraphCog": 5}},
    })
    await _persistence.upsert_crime_delta(very_old, 0, 1, 42, gained=999)
    await _persistence.upsert_gambling_delta(very_old, 0, 1, 42, gained=999)
    # Also a recent row to confirm it's untouched.
    await _persistence.upsert_crime_delta(recent, 0, 1, 42, gained=10)

    async def _ai_up(): return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ai_up)
    await _economy.do_daily_reset()

    assert very_old not in await _persistence.load_balance_history()
    assert very_old not in await _persistence.load_bot_stats_history()
    assert very_old not in await _persistence.load_command_usage_history()
    crime = await _persistence.load_crime_history()
    assert very_old not in crime
    assert recent in crime  # recent row untouched
    assert very_old not in await _persistence.load_gambling_history()


async def test_do_daily_reset_keeps_rows_within_retention(db, monkeypatch):
    """A row from 5 years back is well within the 10y window — must survive."""
    five_years_back = _ancient_date_iso(5)
    await _persistence.upsert_crime_delta(five_years_back, 0, 1, 42, gained=100)

    async def _ai_up(): return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ai_up)
    await _economy.do_daily_reset()

    loaded = await _persistence.load_crime_history()
    assert five_years_back in loaded


async def test_do_daily_reset_leaves_levelup_history_alone(db, monkeypatch):
    """levelup_history is intentionally NOT pruned. Even very old rows
    must survive the daily reset."""
    very_old = _ancient_date_iso(15)  # well past any reasonable retention
    await _persistence.upsert_levelup_delta(very_old, 0, 100, 42, count=5)

    async def _ai_up(): return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ai_up)
    await _economy.do_daily_reset()

    loaded = await _persistence.load_levelup_history()
    assert very_old in loaded
    assert loaded[very_old][0][(100, "42")] == 5


# ── Direct prune helpers: strict-less-than cutoff ────────────────────────────


async def test_prune_crime_history_strictly_before_cutoff(db):
    """The cutoff is exclusive: rows ON the cutoff date must NOT be deleted."""
    cutoff = "2026-01-01"
    older = "2025-12-31"

    await _persistence.upsert_crime_delta(older, 0, 1, 1, gained=1)
    await _persistence.upsert_crime_delta(cutoff, 0, 1, 2, gained=1)

    await _persistence.prune_crime_history(before_date=cutoff)

    loaded = await _persistence.load_crime_history()
    assert older not in loaded
    assert cutoff in loaded


async def test_prune_gambling_history_strictly_before_cutoff(db):
    cutoff = "2026-01-01"
    older = "2025-12-31"

    await _persistence.upsert_gambling_delta(older, 0, 1, 1, gained=1)
    await _persistence.upsert_gambling_delta(cutoff, 0, 1, 2, gained=1)

    await _persistence.prune_gambling_history(before_date=cutoff)

    loaded = await _persistence.load_gambling_history()
    assert older not in loaded
    assert cutoff in loaded


async def test_prune_balance_history_strictly_before_cutoff(db):
    cutoff = "2026-01-01"
    older = "2025-12-31"

    await _persistence.save_balance_history({
        older: {0: {"1": {"wallet": 1, "savings": 0}}},
        cutoff: {0: {"1": {"wallet": 2, "savings": 0}}},
    })

    await _persistence.prune_balance_history(before_date=cutoff)

    loaded = await _persistence.load_balance_history()
    assert older not in loaded
    assert cutoff in loaded


async def test_prune_bot_stats_history_strictly_before_cutoff(db):
    cutoff = "2026-01-01"
    older = "2025-12-31"
    payload = {"messages": 1, "commands": 0, "ai_responses": 0, "ai_up": False, "memory_mb": 0}

    await _persistence.save_bot_stats_history({
        older: {0: payload}, cutoff: {0: payload},
    })

    await _persistence.prune_bot_stats_history(before_date=cutoff)

    loaded = await _persistence.load_bot_stats_history()
    assert older not in loaded
    assert cutoff in loaded


async def test_prune_command_usage_history_strictly_before_cutoff(db):
    cutoff = "2026-01-01"
    older = "2025-12-31"

    await _persistence.save_command_usage_history({
        older: {0: {"GraphCog": 1}},
        cutoff: {0: {"GraphCog": 2}},
    })

    await _persistence.prune_command_usage_history(before_date=cutoff)

    loaded = await _persistence.load_command_usage_history()
    assert older not in loaded
    assert cutoff in loaded
