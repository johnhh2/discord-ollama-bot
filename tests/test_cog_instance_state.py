"""Cog-instance state isolation regression tests.

Round-2 cleanup commit 8113e4d moved 4 transient dicts off global state.py
onto cog-instance attributes:
  - state.crime_active_users   -> EconomyCog._crime_active   (already covered
    in test_money_flows.py)
  - state.user_last_puzzle     -> UtilityCog._last_puzzle_by_uid
  - state.user_last_hangman    -> HangmanCog._last_hangman_by_uid
  - state._soundboard_timestamps -> module-private _SOUNDBOARD_TIMESTAMPS

This file covers the puzzle and hangman dicts directly:
- New cog instance starts with an empty dict (no leakage from a previous
  instance).
- The dict is per-uid (independent timestamps).
- The dict is per-cog-instance (no cross-instance pollution).
"""
import pytest

from src.cogs.utility_cog import UtilityCog
from src.games.hangman import HangmanCog


pytestmark = pytest.mark.asyncio


# ── UtilityCog._last_puzzle_by_uid ────────────────────────────────────────────

async def test_utility_cog_starts_with_empty_puzzle_cooldown_dict():
    cog = UtilityCog(bot=None)
    assert cog._last_puzzle_by_uid == {}


async def test_utility_cog_puzzle_cooldown_per_uid():
    """Setting a cooldown for one uid doesn't affect others."""
    cog = UtilityCog(bot=None)
    cog._last_puzzle_by_uid[1001] = 1_000_000.0
    cog._last_puzzle_by_uid[1002] = 2_000_000.0
    assert cog._last_puzzle_by_uid[1001] == 1_000_000.0
    assert cog._last_puzzle_by_uid[1002] == 2_000_000.0
    assert 1003 not in cog._last_puzzle_by_uid


async def test_separate_utility_cog_instances_have_independent_state():
    """Two cogs (e.g. cog reload scenario) must not share the dict.
    Lives on the *instance*, not the class."""
    cog_a = UtilityCog(bot=None)
    cog_b = UtilityCog(bot=None)
    cog_a._last_puzzle_by_uid[42] = 1_000_000.0
    assert 42 not in cog_b._last_puzzle_by_uid


async def test_utility_cog_puzzle_cooldown_dict_is_not_class_var():
    """If the dict were a class var (not instance), instance B would
    see instance A's mutations. Defend against that regression."""
    cog_a = UtilityCog(bot=None)
    cog_a._last_puzzle_by_uid[7] = 1.0
    # The class itself should not have the populated dict.
    assert getattr(UtilityCog, "_last_puzzle_by_uid", None) is None


# ── HangmanCog._last_hangman_by_uid ───────────────────────────────────────────

async def test_hangman_cog_starts_with_empty_cooldown_dict():
    cog = HangmanCog(bot=None)
    assert cog._last_hangman_by_uid == {}


async def test_hangman_cog_cooldown_per_uid():
    cog = HangmanCog(bot=None)
    cog._last_hangman_by_uid[1001] = 1_000_000.0
    cog._last_hangman_by_uid[1002] = 2_000_000.0
    assert cog._last_hangman_by_uid[1001] == 1_000_000.0
    assert cog._last_hangman_by_uid[1002] == 2_000_000.0


async def test_separate_hangman_cog_instances_have_independent_state():
    cog_a = HangmanCog(bot=None)
    cog_b = HangmanCog(bot=None)
    cog_a._last_hangman_by_uid[42] = 1_000_000.0
    assert 42 not in cog_b._last_hangman_by_uid


async def test_hangman_cog_cooldown_dict_is_not_class_var():
    cog_a = HangmanCog(bot=None)
    cog_a._last_hangman_by_uid[7] = 1.0
    assert getattr(HangmanCog, "_last_hangman_by_uid", None) is None


# ── State no longer lives on src.state ────────────────────────────────────────

async def test_state_module_no_longer_exposes_user_last_puzzle():
    """Regression: round-2 cleanup removed these from src.state. If they
    come back, something is wrong."""
    import src.state as _state
    assert not hasattr(_state, "user_last_puzzle")


async def test_state_module_no_longer_exposes_user_last_hangman():
    import src.state as _state
    assert not hasattr(_state, "user_last_hangman")


async def test_state_module_no_longer_exposes_crime_active_users():
    import src.state as _state
    assert not hasattr(_state, "crime_active_users")


async def test_state_module_no_longer_exposes_soundboard_timestamps():
    import src.state as _state
    assert not hasattr(_state, "_soundboard_timestamps")
