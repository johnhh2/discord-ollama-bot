"""
Shared fixtures for src/ package tests.

Importing src as bot is safe because:
- All load_*() functions return defaults on FileNotFoundError
- bot.run() is only called from main.py / __main__
- commands.Bot(...) creates an object with no network calls
"""
import pytest
import pytest_asyncio
import src as bot
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


@pytest.fixture(autouse=True)
def reset_bot_state(monkeypatch):
    """Reset all mutable bot globals before each test and stub DB I/O."""
    fresh_economy = {"users": {}, "last_daily_reset": None, "guild_house": {}}
    monkeypatch.setattr(bot, "economy", fresh_economy)
    monkeypatch.setattr(_state, "economy", fresh_economy)

    fresh_guild_settings = {}
    monkeypatch.setattr(bot, "guild_settings", fresh_guild_settings)
    monkeypatch.setattr(_state, "guild_settings", fresh_guild_settings)

    fresh_insurance = {}
    monkeypatch.setattr(bot, "insurance", fresh_insurance)
    monkeypatch.setattr(_state, "insurance", fresh_insurance)

    fresh_user_last_request = {}
    monkeypatch.setattr(bot, "user_last_request", fresh_user_last_request)
    monkeypatch.setattr(_state, "user_last_request", fresh_user_last_request)

    # Reset per-test state that newer tests mutate. Existing tests don't touch
    # these (or set them to fresh values themselves), so this is additive-safe.
    monkeypatch.setattr(_state, "bot_admins", set())
    monkeypatch.setattr(_state, "godmode_users", set())
    monkeypatch.setattr(_state, "command_perms", {})

    # Stub all async DB save functions in persistence so tests don't need a real DB
    save_fn_names = [
        "save_economy", "save_guild_house", "save_insurance", "save_jackpot",
        "save_guild_settings", "save_bot_roles", "save_bot_settings", "save_godmode_users",
        "save_chess_games", "save_ragebait", "save_mock", "save_curse", "save_tax",
        "save_rigged_slots", "save_rigged_flips", "save_rigged_scratch", "save_rigged_steal",
        "save_gambler_streak", "save_roleplay_state", "save_fanfic_histories",
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

    # Stub save_quote_log at the src (bot) module level since it's re-exported there
    async def _stub_save_quote_log(log: list):
        import src.state as _s
        _s.quote_log = log[-10:]
    monkeypatch.setattr(bot, "save_quote_log", _stub_save_quote_log)


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

    # Replace the pool factory wherever it's referenced.
    monkeypatch.setattr(_db, "get_pool", _get_pool)
    monkeypatch.setattr(_persistence, "get_pool", _get_pool)

    # Restore real save_*/load_* refs that `reset_bot_state` stubbed.
    for target_module, attr_name, real_fn in _originals.ALL:
        monkeypatch.setattr(target_module, attr_name, real_fn)

    try:
        yield pool
    finally:
        await pool.close()
