"""
Shared fixtures for bot.py tests.

Importing bot is safe because:
- All load_*() functions return defaults on FileNotFoundError
- bot.run() is guarded by __name__ == "__main__"
- commands.Bot(...) creates an object with no network calls
"""
import pytest
import bot


@pytest.fixture(autouse=True)
def reset_bot_state(monkeypatch):
    """Reset all mutable bot globals before each test and stub file I/O."""
    monkeypatch.setattr(bot, "economy", {"users": {}, "last_daily_reset": None})
    monkeypatch.setattr(bot, "guild_settings", {})
    monkeypatch.setattr(bot, "insurance", {})
    monkeypatch.setattr(bot, "user_last_request", {})
    # Prevent tests from writing to data/ on disk
    monkeypatch.setattr(bot, "_save_json", lambda *args: None)
