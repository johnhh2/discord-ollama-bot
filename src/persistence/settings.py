import json

from src import state
from src.db import with_cursor, with_transaction


async def save_guild_settings():
    async with with_cursor() as cur:
        for gid_str, settings in state.guild_settings.items():
            await cur.execute(
                "INSERT INTO guild_settings (guild_id, settings_json) VALUES (%s,%s)"
                " ON DUPLICATE KEY UPDATE settings_json=VALUES(settings_json)",
                (int(gid_str), json.dumps(settings)),
            )


async def save_bot_roles():
    """Replace the bot_roles table with the current in-memory contents.

    Source of truth for *rank* is state.bot_role_ranks ({(guild_id, role_id):
    rank}). state.bot_roles is the set of role IDs and is kept in sync as a
    derived view. Any role in bot_roles that has no entry in bot_role_ranks
    defaults to guild_id=0 and rank_pos=0 — that branch shouldn't normally
    fire (createrole / deleterole maintain both), but it preserves legacy
    behavior for any caller that mutates bot_roles directly.
    """
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM bot_roles")
        seen: set = set()
        for (guild_id, role_id), rank_pos in state.bot_role_ranks.items():
            await cur.execute(
                "INSERT IGNORE INTO bot_roles (role_id, guild_id, rank_pos) VALUES (%s,%s,%s)",
                (role_id, guild_id, rank_pos),
            )
            seen.add(role_id)
        for role_id in state.bot_roles - seen:
            await cur.execute(
                "INSERT IGNORE INTO bot_roles (role_id, guild_id, rank_pos) VALUES (%s,%s,%s)",
                (role_id, 0, 0),
            )


async def save_godmode_users():
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM godmode_users")
        for uid in state.godmode_users:
            await cur.execute("INSERT IGNORE INTO godmode_users (user_id) VALUES (%s)", (uid,))


async def save_bot_settings():
    async with with_transaction() as cur:
        for k, v in state.bot_settings.items():
            await cur.execute(
                "INSERT INTO bot_settings (key_name, value_text) VALUES (%s,%s)"
                " ON DUPLICATE KEY UPDATE value_text=VALUES(value_text)",
                (k, str(v)),
            )
        # Also delete rows for keys popped from state (e.g. `!settings-channel
        # X clear`) — upsert-only left them in the DB, so cleared channels
        # silently resurrected on the next reboot.
        if state.bot_settings:
            placeholders = ",".join(["%s"] * len(state.bot_settings))
            await cur.execute(
                f"DELETE FROM bot_settings WHERE key_name NOT IN ({placeholders})",  # nosec B608 - placeholders is only "%s,%s,...", values are bound
                tuple(state.bot_settings.keys()),
            )
        else:
            await cur.execute("DELETE FROM bot_settings")
