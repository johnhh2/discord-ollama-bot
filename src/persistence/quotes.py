import json

from src import state
from src.db import with_cursor, with_transaction


async def save_quote_log(log: list):
    trimmed = log[-10:]
    state.quote_log = trimmed
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM quote_log")
        for entry in trimmed:
            await cur.execute(
                "INSERT INTO quote_log (content) VALUES (%s)", (entry,)
            )


async def save_saved_quotes(quotes: dict):
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM saved_quotes")
        for guild_id_str, guild_quotes in quotes.items():
            for q in guild_quotes:
                await cur.execute(
                    "INSERT INTO saved_quotes (guild_id, quote_json) VALUES (%s,%s)",
                    (str(guild_id_str), json.dumps(q)),
                )


async def load_saved_quotes() -> dict:
    async with with_cursor() as cur:
        await cur.execute("SELECT guild_id, quote_json FROM saved_quotes")
        rows = await cur.fetchall()
    result = {}
    for guild_id_str, quote_json in rows:
        q = json.loads(quote_json)
        result.setdefault(guild_id_str, []).append(q)
    return result
