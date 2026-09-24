"""mc_players / mc_trades tables — the Minecraft block shop (migration 0081).
Mirrors state.mc_players; the trade log is write-only from the bot."""
from src import state
from src.db import with_cursor


async def save_mc_player(user_id: int) -> None:
    """Mirror state.mc_players[user_id] whole — gamertag, purse and link
    time in one shot, so no call site can drop a column."""
    row = state.mc_players[int(user_id)]
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO mc_players (user_id, gamertag, blocks, linked_at) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE gamertag=VALUES(gamertag), blocks=VALUES(blocks), "
            "linked_at=VALUES(linked_at)",
            (int(user_id), row.get("gamertag"), int(row.get("blocks", 0)), row.get("linked_at")),
        )


async def log_mc_trade(*, ts: int, user_id: int, guild_id: "int | None", gamertag: str,
                       kind: str, item_id: str, count: int, blocks: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO mc_trades (ts, user_id, guild_id, gamertag, kind, item_id, count, blocks) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (int(ts), int(user_id), guild_id, gamertag, kind, item_id, int(count), int(blocks)),
        )
