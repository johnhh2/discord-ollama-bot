from src.db import with_cursor


async def save_blocklist(guild_id: int, user_id: int, reason: str | None, banned_by: int):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO blocklist (guild_id, user_id, reason, banned_by)"
            " VALUES (%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE reason=VALUES(reason), banned_by=VALUES(banned_by)",
            (guild_id, user_id, reason, banned_by),
        )


async def delete_blocklist(guild_id: int, user_id: int):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM blocklist WHERE guild_id=%s AND user_id=%s",
            (guild_id, user_id),
        )


async def save_global_blocklist(user_id: int, reason: str | None, banned_by: int):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO global_blocklist (user_id, reason, banned_by)"
            " VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE reason=VALUES(reason), banned_by=VALUES(banned_by)",
            (user_id, reason, banned_by),
        )


async def delete_global_blocklist(user_id: int):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM global_blocklist WHERE user_id=%s",
            (user_id,),
        )
