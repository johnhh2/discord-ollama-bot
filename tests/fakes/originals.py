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
import src.leveling as _leveling
import src.persistence as _persistence


# Names mirrored from tests/conftest.py:reset_bot_state.save_fn_names.
# Kept in lockstep — if a new save_* is stubbed there, add it here too.
_PERSISTENCE_SAVE_NAMES = [
    "save_economy", "save_guild_house", "save_insurance", "save_jackpot",
    "save_guild_settings", "save_bot_roles", "save_bot_settings", "save_godmode_users",
    "save_chess_games", "save_chess_game", "delete_chess_game", "save_chess_report",
    "save_ragebait", "save_mock", "save_curse", "save_tax",
    "save_rigged_slots", "save_rigged_flips", "save_rigged_scratch", "save_rigged_steal",
    "save_user_artifact",
    "save_gambler_streak", "save_command_streak", "save_ai_threads",
    "save_quote_log", "save_saved_quotes", "save_lottery", "save_records",
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
    # Loads stubbed to empty rows for the monitor's rollup (not save_*, but
    # the same stub-and-restore lifecycle applies).
    "load_mc_ping_samples", "load_mc_daily_ping_stats",
    "bump_daily_counter", "prune_daily_counters",
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
              "save_balance_history", "save_bot_stats_history",
              "upsert_crime_delta", "upsert_gambling_delta",
              "prune_balance_history", "prune_bot_stats_history",
              "prune_command_usage_history",
              "prune_crime_history", "prune_gambling_history",
              "prune_levelup_history",
              "prune_notable_events", "prune_daily_counters"):
    if hasattr(_economy, _name):
        ALL.append((_economy, _name, getattr(_economy, _name)))

# Mirror on src.leveling for upsert_levelup_delta.
if hasattr(_leveling, "upsert_levelup_delta"):
    ALL.append((_leveling, "upsert_levelup_delta", _leveling.upsert_levelup_delta))
