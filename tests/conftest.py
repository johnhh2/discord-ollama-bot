"""
Shared fixtures for src/ package tests.

Importing src as bot is safe because:
- All load_*() functions return defaults on FileNotFoundError
- bot.run() is only called from main.py / __main__
- commands.Bot(...) creates an object with no network calls
"""
import pytest
import src as bot
import src.state as _state
import src.persistence as _persistence


@pytest.fixture(autouse=True)
def reset_bot_state(monkeypatch):
    """Reset all mutable bot globals before each test and stub file I/O."""
    fresh_economy = {"users": {}, "last_daily_reset": None}
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

    # Prevent tests from writing to data/ on disk
    monkeypatch.setattr(bot, "_save_json", lambda *args: None)
    monkeypatch.setattr(_persistence, "_save_json", lambda *args: None)
