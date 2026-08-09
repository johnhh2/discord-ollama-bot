from src.db import with_cursor, with_transaction


async def load_lottery(guild_id: int) -> dict:
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT prize_pool, last_posted_week, last_drawn_week FROM lottery WHERE guild_id=%s",
            (guild_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return {"prize_pool": 0, "players": {}, "last_posted_week": 0, "last_drawn_week": 0}
        prize_pool, last_posted_week, last_drawn_week = row
        await cur.execute(
            "SELECT user_id, tickets FROM lottery_players WHERE guild_id=%s", (guild_id,)
        )
        players = {str(r[0]): r[1] for r in await cur.fetchall()}
    return {
        "prize_pool": prize_pool,
        "players": players,
        "last_posted_week": last_posted_week,
        "last_drawn_week": last_drawn_week,
    }


async def load_lottery_automatch(guild_id: int) -> dict:
    """Return {user_id_str: max_tickets} for every automatch opt-in in the guild."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT user_id, max_tickets FROM lottery_automatch WHERE guild_id=%s",
            (guild_id,),
        )
        return {str(r[0]): int(r[1]) for r in await cur.fetchall()}


async def save_lottery_automatch(guild_id: int, user_id: int, max_tickets: int):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO lottery_automatch (guild_id, user_id, max_tickets)"
            " VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE max_tickets=VALUES(max_tickets)",
            (guild_id, user_id, max_tickets),
        )


async def delete_lottery_automatch(guild_id: int, user_id: int):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM lottery_automatch WHERE guild_id=%s AND user_id=%s",
            (guild_id, user_id),
        )


async def clear_lottery_automatch(guild_id: int):
    """Drop every automatch opt-in in the guild — called when the draw resets the lottery."""
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM lottery_automatch WHERE guild_id=%s",
            (guild_id,),
        )


async def save_lottery(guild_id: int, lottery_data: dict):
    async with with_transaction() as cur:
        await cur.execute(
            "INSERT INTO lottery (guild_id, prize_pool, last_posted_week, last_drawn_week)"
            " VALUES (%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE prize_pool=VALUES(prize_pool),"
            " last_posted_week=VALUES(last_posted_week),"
            " last_drawn_week=VALUES(last_drawn_week)",
            (
                guild_id,
                lottery_data.get("prize_pool", 0),
                lottery_data.get("last_posted_week", 0),
                lottery_data.get("last_drawn_week", 0),
            ),
        )
        await cur.execute("DELETE FROM lottery_players WHERE guild_id=%s", (guild_id,))
        for uid_str, tickets in lottery_data.get("players", {}).items():
            await cur.execute(
                "INSERT INTO lottery_players (guild_id, user_id, tickets) VALUES (%s,%s,%s)",
                (guild_id, int(uid_str), tickets),
            )
