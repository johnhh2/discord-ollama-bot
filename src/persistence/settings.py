import json

from src import state
from src.db import with_cursor


async def save_guild_settings():
    async with with_cursor() as cur:
        for gid_str, settings in state.guild_settings.items():
            await cur.execute(
                "INSERT INTO guild_settings (guild_id, settings_json) VALUES (%s,%s)"
                " ON DUPLICATE KEY UPDATE settings_json=VALUES(settings_json)",
                (int(gid_str), json.dumps(settings)),
            )


async def save_bot_roles():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM bot_roles")
        for role_id in state.bot_roles:
            await cur.execute("INSERT IGNORE INTO bot_roles (role_id) VALUES (%s)", (role_id,))


async def save_godmode_users():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM godmode_users")
        for uid in state.godmode_users:
            await cur.execute("INSERT IGNORE INTO godmode_users (user_id) VALUES (%s)", (uid,))


async def save_bot_settings():
    async with with_cursor() as cur:
        for k, v in state.bot_settings.items():
            await cur.execute(
                "INSERT INTO bot_settings (key_name, value_text) VALUES (%s,%s)"
                " ON DUPLICATE KEY UPDATE value_text=VALUES(value_text)",
                (k, str(v)),
            )
