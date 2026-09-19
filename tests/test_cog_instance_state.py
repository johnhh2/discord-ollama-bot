"""Cog-instance state isolation.

The transient per-user dicts live on the cog instance — not on src.state,
and not as class vars: a fresh cog (a reload) starts empty and two
instances never share one. The globals they replaced must stay gone from
src.state. EconomyCog._crime_active is covered in test_money_flows.py.
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
    """A class var would let instance B see instance A's mutations."""
    cog_a = UtilityCog(bot=None)
    cog_a._last_puzzle_by_uid[7] = 1.0
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


# -- ChessCog._bot_chess_day_by_uid -------------------------------------------

async def test_chess_cog_starts_with_empty_bot_day_dict():
    from src.games.chess import ChessCog
    cog = ChessCog(bot=None)
    assert cog._bot_chess_day_by_uid == {}


async def test_chess_cog_bot_day_dict_is_per_instance_not_class_var():
    from src.games.chess import ChessCog
    cog_a = ChessCog(bot=None)
    cog_b = ChessCog(bot=None)
    cog_a._bot_chess_day_by_uid[42] = "2026-09-02"
    assert 42 not in cog_b._bot_chess_day_by_uid
    assert getattr(ChessCog, "_bot_chess_day_by_uid", None) is None
