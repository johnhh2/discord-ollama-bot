import json

from src import state
from src.db import with_cursor


async def save_chess_games():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM chess_games")
        for ch_id, game in state.active_chess_games.items():
            await cur.execute(
                "INSERT INTO chess_games (channel_id, game_json) VALUES (%s,%s)",
                (int(ch_id), json.dumps(game)),
            )
