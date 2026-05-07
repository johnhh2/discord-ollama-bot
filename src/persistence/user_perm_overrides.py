from src.db import with_cursor


async def save_user_perm_override(guild_id: int, user_id: int, tier: str):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO user_perm_overrides (guild_id, user_id, tier) VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE tier=VALUES(tier)",
            (guild_id, user_id, tier),
        )


async def delete_user_perm_override(guild_id: int, user_id: int):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM user_perm_overrides WHERE guild_id=%s AND user_id=%s",
            (guild_id, user_id),
        )
