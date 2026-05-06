from src import state
from src.db import with_cursor


async def save_gambler_streak():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM gambler_streak")
        for uid_str, entry in state.gambler_streak.items():
            if isinstance(entry, dict):
                date_str = entry.get("date", "")
                count = int(entry.get("count", 1))
            else:
                date_str = entry
                count = 1
            if not date_str:
                continue
            await cur.execute(
                "INSERT INTO gambler_streak (user_id, last_full_date, streak_count) VALUES (%s,%s,%s)",
                (int(uid_str), date_str, count),
            )
