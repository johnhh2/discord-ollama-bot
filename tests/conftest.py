"""
Shared fixtures for src/ package tests.

Tests import directly from src.<module> (state/persistence/economy/etc.).
There is no longer a `src` re-export wall — patching `state.X` is the only
source of truth.
"""
import pytest
import pytest_asyncio
import src.state as _state
import src.persistence as _persistence
import src.economy as _economy
import src.db as _db

# Import this BEFORE the autouse fixture below runs — it snapshots the real
# save_*/load_* function refs so the opt-in `db` fixture can restore them.
from tests.fakes import originals as _originals  # noqa: E402,F401


async def _noop(*args, **kwargs):
    pass


async def _noop_bool(*args, **kwargs):
    return False


def _reset_dict(d, fresh: dict):
    """Mutate dict d in-place so existing references (e.g. `from src.state
    import economy`) stay valid across the reset."""
    d.clear()
    d.update(fresh)


def _reset_set(s, fresh: set):
    s.clear()
    s.update(fresh)


@pytest.fixture(autouse=True)
def reset_bot_state(monkeypatch):
    """Reset all mutable bot globals before each test and stub DB I/O.

    Uses in-place mutation rather than rebinding so test files that did
    `from src.state import economy` keep their reference live.
    """
    # The on_message gate (src/persistence/__init__.py:init_done) starts unset
    # in production and is .set() by init_db_state. Tests that drive on_message
    # directly don't go through init_db_state, so default to set per-test so
    # those tests don't hang on the await.
    #
    # asyncio.Event binds to the running loop on first use, so the module-level
    # Event from import time is stale across pytest-asyncio's per-test loops.
    # Replace it with a fresh Event each test (and re-set it).
    import asyncio as _asyncio_for_event
    _fresh_event = _asyncio_for_event.Event()
    _fresh_event.set()
    monkeypatch.setattr(_persistence, "init_done", _fresh_event)

    # Cogs register presence providers into this module-level registry on
    # construction; clear it so providers don't leak across tests.
    import src.status_manager as _status_manager
    _status_manager._providers.clear()
    # GamblingSessionCog registers its rename hook on construction; same deal.
    _economy.GAMBLING_RESULT_HOOKS.clear()

    _reset_dict(_state.economy, {"users": {}, "last_daily_reset": None, "last_insurance_sweep": None, "guild_house": {}})
    _reset_dict(_state.lottery_tickets_today, {"date": None, "count": 0})
    _reset_dict(_state.guild_settings, {})
    _reset_dict(_state.insurance, {})
    _state.quote_log[:] = []

    # Reset per-test state that newer tests mutate. Existing tests don't touch
    # these (or set them to fresh values themselves), so this is additive-safe.
    monkeypatch.setattr(_state, "bot_admins", set())
    monkeypatch.setattr(_state, "godmode_users", set())
    monkeypatch.setattr(_state, "insurance_subs", {})
    monkeypatch.setattr(_state, "command_perms", {})
    monkeypatch.setattr(_state, "user_perm_overrides", {})
    monkeypatch.setattr(_state, "blocklist", {})
    monkeypatch.setattr(_state, "global_blocklist", {})
    monkeypatch.setattr(_state, "leveling", {})
    monkeypatch.setattr(_state, "voice_pings", {})
    monkeypatch.setattr(_state, "voice_ping_ignores", {})
    monkeypatch.setattr(_state, "active_taxes", {})
    monkeypatch.setattr(_state, "active_curses", {})
    monkeypatch.setattr(_state, "active_mocks", {})
    monkeypatch.setattr(_state, "active_bounties", {})
    monkeypatch.setattr(_state, "active_ragebaits", {})
    monkeypatch.setattr(_state, "active_events", {})
    monkeypatch.setattr(_state, "user_artifacts", {})
    monkeypatch.setattr(_state, "property_owners", {})
    monkeypatch.setattr(_state, "lottery_ticket_grants", {})
    monkeypatch.setattr(_state, "rigged_slots", {})
    monkeypatch.setattr(_state, "rigged_flips", {})
    monkeypatch.setattr(_state, "rigged_scratch", {})
    monkeypatch.setattr(_state, "rigged_steal", {})
    monkeypatch.setattr(_state, "active_chess_games", {})
    monkeypatch.setattr(_state, "chess_user_stats", {})
    monkeypatch.setattr(_state, "chess_unlocks", {})
    monkeypatch.setattr(_state, "chess_equipped", {})
    monkeypatch.setattr(_state, "active_blackjack_games", {})
    monkeypatch.setattr(_state, "active_hangman_games", {})
    monkeypatch.setattr(_state, "active_ttt_games", {})
    monkeypatch.setattr(_state, "active_c4_games", {})
    monkeypatch.setattr(_state, "active_race_games", {})
    monkeypatch.setattr(_state, "active_puzzles", {})
    monkeypatch.setattr(_state, "ai_threads", {})
    monkeypatch.setattr(_state, "gambling_threads", {})
    monkeypatch.setattr(_state, "channel_prompts", {})
    monkeypatch.setattr(_state, "command_streak", {})
    monkeypatch.setattr(_state, "crime_today_by_user", {})
    monkeypatch.setattr(_state, "gambling_today_by_user", {})
    monkeypatch.setattr(_state, "levelups_today", {})
    monkeypatch.setattr(_state, "locked_channels", {})
    monkeypatch.setattr(_state, "locked_roles", {})
    monkeypatch.setattr(_state, "bot_roles", set())
    monkeypatch.setattr(_state, "bot_role_ranks", {})
    monkeypatch.setattr(_state, "mc_last_online", None)
    monkeypatch.setattr(_state, "mc_last_ping_ms", None)

    # init_db_state is one-shot in production (guarded against on_ready
    # reconnects), but tests call it repeatedly to re-seed state from the
    # fake DB; wrap to clear the guard before every invocation.
    _real_init_db_state = _persistence.init_db_state

    async def _init_db_state_for_tests(*args, **kwargs):
        _persistence._init_db_state_done = False
        return await _real_init_db_state(*args, **kwargs)

    monkeypatch.setattr(_persistence, "init_db_state", _init_db_state_for_tests)

    # Stub all async DB save functions in persistence so tests don't need a real DB
    save_fn_names = [
        "save_economy", "save_guild_house", "save_insurance", "save_insurance_subs",
        "save_insurance_sweep_day", "save_jackpot",
        "save_guild_settings", "save_bot_roles", "save_bot_settings", "save_godmode_users",
        "save_chess_games", "save_chess_game", "delete_chess_game", "save_chess_report",
        "save_chess_user_stats", "save_chess_unlock", "save_chess_equipped",
        "save_chess_analysis",
        "save_ragebait", "save_mock", "save_curse", "save_tax",
        "save_rigged_slots", "save_rigged_flips", "save_rigged_scratch", "save_rigged_steal",
        "save_user_artifact",
        "save_property_owner", "delete_property_owner",
        "save_gambler_streak", "save_command_streak", "save_ai_threads",
        "save_gambling_thread", "delete_gambling_thread",
        "save_quote_log", "save_saved_quotes", "save_lottery", "save_records",
        "save_lottery_ticket_grant",
        "save_leveling", "save_command_perms", "save_channel_prompts",
        "save_balance_history", "save_bot_stats_history",
        "save_command_usage_history",
        "save_blocklist", "delete_blocklist",
        "save_global_blocklist", "delete_global_blocklist",
        "upsert_crime_delta", "upsert_gambling_delta", "upsert_levelup_delta",
        "prune_balance_history", "prune_bot_stats_history",
        "prune_command_usage_history",
        "prune_crime_history", "prune_gambling_history",
        "prune_levelup_history",
        "log_notable_event", "prune_notable_events",
        "add_ephemeral_msg",
        "save_mc_ping_sample", "prune_mc_ping_samples",
        "record_mc_player_event", "prune_mc_player_events",
        "upsert_mc_daily_player_stats", "prune_mc_daily_player_stats",
        "save_mc_daily_ping_stats", "prune_mc_daily_ping_stats",
        "bump_daily_counter", "prune_daily_counters",
    ]
    for fn_name in save_fn_names:
        if hasattr(_persistence, fn_name):
            monkeypatch.setattr(_persistence, fn_name, _noop)

    monkeypatch.setattr(_persistence, "try_set_record", _noop_bool)

    # Loads the Minecraft monitor's hourly rollup reads inside the poll loop —
    # stubbed to empty rows so ticking the monitor in non-db tests never
    # touches a real pool. Restored by the `db` fixture via originals.
    async def _empty_rows(*args, **kwargs):
        return []
    monkeypatch.setattr(_persistence, "load_mc_ping_samples", _empty_rows)
    monkeypatch.setattr(_persistence, "load_mc_daily_ping_stats", _empty_rows)

    # Also stub save_economy and save_insurance in src.economy (which imports them directly)
    monkeypatch.setattr(_economy, "save_economy", _noop)
    monkeypatch.setattr(_economy, "save_insurance", _noop)
    monkeypatch.setattr(_economy, "try_set_record", _noop_bool)
    monkeypatch.setattr(_economy, "save_balance_history", _noop)
    monkeypatch.setattr(_economy, "save_bot_stats_history", _noop)
    # Activity-stat upserters: src.economy and src.leveling import them at module
    # scope, so patching src.persistence isn't enough — the bound names in those
    # modules need stubs too.
    monkeypatch.setattr(_economy, "upsert_crime_delta", _noop, raising=False)
    monkeypatch.setattr(_economy, "upsert_gambling_delta", _noop, raising=False)
    monkeypatch.setattr(_economy, "prune_balance_history", _noop, raising=False)
    monkeypatch.setattr(_economy, "prune_bot_stats_history", _noop, raising=False)
    monkeypatch.setattr(_economy, "prune_command_usage_history", _noop, raising=False)
    monkeypatch.setattr(_economy, "prune_crime_history", _noop, raising=False)
    monkeypatch.setattr(_economy, "prune_gambling_history", _noop, raising=False)
    monkeypatch.setattr(_economy, "prune_levelup_history", _noop, raising=False)
    monkeypatch.setattr(_economy, "prune_notable_events", _noop, raising=False)
    monkeypatch.setattr(_economy, "prune_daily_counters", _noop, raising=False)
    import src.leveling as _leveling
    monkeypatch.setattr(_leveling, "upsert_levelup_delta", _noop, raising=False)

    # Post-game chess engine analysis: never spawn Stockfish in tests. The
    # engine_available gate keeps _finalize_game from even creating the
    # background task; the _run_engine_analysis stub is a second guard for
    # anything calling analyze_and_post directly. Tests that exercise the
    # analysis pipeline monkeypatch _run_engine_analysis with canned data.
    import src.games.chess_analysis as _chess_analysis
    monkeypatch.setattr(_chess_analysis, "engine_available", lambda: False)

    async def _no_analysis(*args, **kwargs):
        return None
    monkeypatch.setattr(_chess_analysis, "_run_engine_analysis", _no_analysis)

    # Every purchase now opens with a Confirm/Cancel button prompt
    # (confirm_purchase / confirm_prompt). Auto-accept by default so the
    # existing purchase tests run unchanged; tests exercising decline,
    # timeout, or mid-confirm drift monkeypatch these per-test (which
    # overrides this stub).
    async def _auto_confirm(*args, **kwargs):
        return True
    import src.cogs.shop_cog as _shop_cog_mod
    import src.cogs.bounty_cog as _bounty_cog_mod
    import src.cogs.assets_cog as _assets_cog_mod
    import src.cogs.lottery_cog as _lottery_cog_mod
    import src.games.chess as _chess_game_mod
    monkeypatch.setattr(_shop_cog_mod, "confirm_purchase", _auto_confirm)

    # The insurance tier picker (confirm_choice) auto-picks the highlighted
    # default choice — the tier the command was given, else the buyer's
    # current tier, else the cheapest — so `!shop insurance premium 3` buys
    # premium in tests without a button click. Tests exercising a different
    # click, or cancel, monkeypatch this per-test.
    async def _auto_choice(*args, choices=None, **kwargs):
        for choice in choices or []:
            if choice.get("default"):
                return choice["value"]
        return choices[0]["value"] if choices else None
    monkeypatch.setattr(_shop_cog_mod, "confirm_choice", _auto_choice)
    monkeypatch.setattr(_chess_game_mod, "confirm_prompt", _auto_confirm)
    monkeypatch.setattr(_bounty_cog_mod, "confirm_purchase", _auto_confirm)
    monkeypatch.setattr(_assets_cog_mod, "confirm_purchase", _auto_confirm)
    monkeypatch.setattr(_assets_cog_mod, "confirm_prompt", _auto_confirm)
    monkeypatch.setattr(_lottery_cog_mod, "confirm_purchase", _auto_confirm)


@pytest_asyncio.fixture
async def db(monkeypatch):
    """Opt-in: real save/load paths against an in-memory SQLite that mimics MariaDB.

    Tests that take this fixture exercise the actual SQL in src/persistence.py
    instead of the no-op stubs installed by `reset_bot_state`. Use this for
    persistence round-trips, shop money flow, and anything where "did it
    actually persist?" matters.
    """
    from tests.fakes.db import make_fake_pool
    pool = await make_fake_pool()

    async def _get_pool():
        return pool

    # Replace the pool factory at the source. with_cursor() in src/db.py
    # calls get_pool() through its module scope, so patching _db.get_pool
    # cascades wherever with_cursor is used. Tests that reach into
    # _persistence.get_pool directly (e.g. test_slots_flow.py:_read_jackpot)
    # need the second patch.
    monkeypatch.setattr(_db, "get_pool", _get_pool)
    monkeypatch.setattr(_persistence, "get_pool", _get_pool)

    # Restore real save_*/load_* refs that `reset_bot_state` stubbed.
    for target_module, attr_name, real_fn in _originals.ALL:
        monkeypatch.setattr(target_module, attr_name, real_fn)

    try:
        yield pool
    finally:
        await pool.close()
