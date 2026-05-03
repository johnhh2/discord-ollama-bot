"""Per-guild settings accessor.

`state.guild_settings` is the in-memory cache of `data/guild_settings.json`'s
JSON-blob equivalent (now backed by the `guild_settings` table in MariaDB).
This module exposes the one accessor that auto-creates an empty entry for a
guild seen for the first time. Pure dict logic — no DB I/O.
"""
from src import state


def get_guild_cfg(guild_id: int) -> dict:
    """Return the (mutable) settings dict for `guild_id`, creating it if absent."""
    key = str(guild_id)
    if key not in state.guild_settings:
        state.guild_settings[key] = {}
    return state.guild_settings[key]
