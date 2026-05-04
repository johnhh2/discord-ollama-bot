"""Snapshot the real save_*/load_* refs at import time.

The autouse `reset_bot_state` fixture in tests/conftest.py replaces every
`save_*` function on `src.persistence` (and a few mirrors on `src.economy`)
with no-op stubs, because most existing tests don't want to touch the DB.
The opt-in `db` fixture wants to put them back.

We capture the originals here, at module import time, before any test runs.
Importing this module is what actually performs the snapshot — keep it cheap
(no DB calls).

Each entry is `(target_module, attr_name, real_callable)`.
"""
import src.economy as _economy
import src.persistence as _persistence


# Names mirrored from tests/conftest.py:reset_bot_state.save_fn_names.
# Kept in lockstep — if a new save_* is stubbed there, add it here too.
_PERSISTENCE_SAVE_NAMES = [
    "save_economy", "save_guild_house", "save_insurance", "save_jackpot",
    "save_guild_settings", "save_bot_roles", "save_bot_settings", "save_godmode_users",
    "save_chess_games", "save_ragebait", "save_mock", "save_curse", "save_tax",
    "save_rigged_slots", "save_rigged_flips", "save_rigged_scratch", "save_rigged_steal",
    "save_gambler_streak", "save_ai_threads",
    "save_quote_log", "save_saved_quotes", "save_lottery", "save_records",
    "save_leveling", "save_command_perms", "save_channel_prompts",
    "save_balance_history", "save_bot_stats_history",
    "save_command_usage_history", "save_crime_history", "save_gambling_history",
    "add_ephemeral_msg",
]

ALL: list = []

for _name in _PERSISTENCE_SAVE_NAMES:
    if hasattr(_persistence, _name):
        ALL.append((_persistence, _name, getattr(_persistence, _name)))

# try_set_record is also stubbed (returns False) on persistence + economy.
ALL.append((_persistence, "try_set_record", _persistence.try_set_record))

# Mirrors on src.economy (it imports save_economy/save_insurance/try_set_record/
# save_balance_history/save_bot_stats_history directly).
for _name in ("save_economy", "save_insurance", "try_set_record",
              "save_balance_history", "save_bot_stats_history"):
    if hasattr(_economy, _name):
        ALL.append((_economy, _name, getattr(_economy, _name)))
