from src.db import with_cursor


async def save_recap_usage(guild_id: int, user_id: int, day: str):
    """Record that `user_id` ran !recap in `guild_id` on the 5am-CT day `day`.

    Upserts the single (guild_id, user_id) row — the daily cap only needs
    the most recent run date, so older dates are simply overwritten.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO recap_usage (guild_id, user_id, last_date) VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE last_date=VALUES(last_date)",
            (guild_id, user_id, day),
        )
