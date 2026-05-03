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
    _reset_dict(_state.economy, {"users": {}, "last_daily_reset": None, "guild_house": {}})
    _reset_dict(_state.guild_settings, {})
    _reset_dict(_state.insurance, {})
    _reset_dict(_state.user_last_request, {})
    _state.quote_log[:] = []

    # Reset per-test state that newer tests mutate. Existing tests don't touch
    # these (or set them to fresh values themselves), so this is additive-safe.
    monkeypatch.setattr(_state, "bot_admins", set())
    monkeypatch.setattr(_state, "godmode_users", set())
    monkeypatch.setattr(_state, "command_perms", {})
    monkeypatch.setattr(_state, "leveling", {})
    monkeypatch.setattr(_state, "active_taxes", {})
    monkeypatch.setattr(_state, "active_curses", {})
    monkeypatch.setattr(_state, "active_mocks", {})
    monkeypatch.setattr(_state, "active_ragebaits", {})
    monkeypatch.setattr(_state, "active_events", {})
    monkeypatch.setattr(_state, "rigged_slots", {})
    monkeypatch.setattr(_state, "rigged_steal", {})
    monkeypatch.setattr(_state, "active_chess_games", {})
    monkeypatch.setattr(_state, "ai_threads", {})
    monkeypatch.setattr(_state, "channel_prompts", {})
    monkeypatch.setattr(_state, "locked_channels", {})
    monkeypatch.setattr(_state, "locked_roles", {})

    # Stub all async DB save functions in persistence so tests don't need a real DB
    save_fn_names = [
        "save_economy", "save_guild_house", "save_insurance", "save_jackpot",
        "save_guild_settings", "save_bot_roles", "save_bot_settings", "save_godmode_users",
        "save_chess_games", "save_ragebait", "save_mock", "save_curse", "save_tax",
        "save_rigged_slots", "save_rigged_flips", "save_rigged_scratch", "save_rigged_steal",
        "save_gambler_streak", "save_ai_threads",
        "save_quote_log", "save_saved_quotes", "save_lottery", "save_records",
        "save_leveling", "save_command_perms", "save_channel_prompts",
        "save_balance_history", "save_bot_stats_history", "add_ephemeral_msg",
    ]
    for fn_name in save_fn_names:
        if hasattr(_persistence, fn_name):
            monkeypatch.setattr(_persistence, fn_name, _noop)

    monkeypatch.setattr(_persistence, "try_set_record", _noop_bool)

    # Also stub save_economy and save_insurance in src.economy (which imports them directly)
    monkeypatch.setattr(_economy, "save_economy", _noop)
    monkeypatch.setattr(_economy, "save_insurance", _noop)
    monkeypatch.setattr(_economy, "try_set_record", _noop_bool)
    monkeypatch.setattr(_economy, "save_balance_history", _noop)
    monkeypatch.setattr(_economy, "save_bot_stats_history", _noop)


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
