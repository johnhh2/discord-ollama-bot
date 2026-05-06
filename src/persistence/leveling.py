import json

from src import state
from src.db import with_cursor


async def save_leveling(guild_id: int = None, uid: int = None):
    """Write leveling rows. state.leveling = {guild_id_str: {uid_str: {...}}}.

    If both guild_id and uid are passed, only that one row is written — safe
    even if state.leveling was never fully loaded. If both are None, writes
    every row in state (bulk; should rarely be needed now).
    """
    async with with_cursor() as cur:
        if guild_id is not None and uid is not None:
            rec = state.leveling.get(str(guild_id), {}).get(str(uid))
            if rec is not None:
                await cur.execute(
                    "INSERT INTO leveling (guild_id, user_id, data) VALUES (%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE data=VALUES(data)",
                    (int(guild_id), int(uid), json.dumps(rec)),
                )
            return
        for gid_str, users in state.leveling.items():
            for uid_str, rec in users.items():
                await cur.execute(
                    "INSERT INTO leveling (guild_id, user_id, data) VALUES (%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE data=VALUES(data)",
                    (int(gid_str), int(uid_str), json.dumps(rec)),
                )
