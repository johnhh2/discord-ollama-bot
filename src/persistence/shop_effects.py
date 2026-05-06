import json

from src import state
from src.db import with_cursor


async def save_ragebait():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='ragebait'")
        for uid, data in state.active_ragebaits.items():
            await cur.execute(
                "INSERT INTO shop_effects (user_id, effect_type, remaining, started_by, history_json, channel_id)"
                " VALUES (%s,'ragebait',%s,%s,%s,%s)",
                (int(uid), data.get("remaining"), data.get("started_by"),
                 json.dumps(data.get("history", [])), data.get("channel_id")),
            )


async def save_mock():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='mock'")
        for uid, data in state.active_mocks.items():
            await cur.execute(
                "INSERT INTO shop_effects (user_id, effect_type, remaining, started_by, channel_id)"
                " VALUES (%s,'mock',%s,%s,%s)",
                (int(uid), data.get("remaining"), data.get("started_by"), data.get("channel_id")),
            )


async def save_curse(curse_data: dict):
    state.active_curses = curse_data
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='curse'")
        for uid, data in curse_data.items():
            await cur.execute(
                "INSERT INTO shop_effects (user_id, effect_type, remaining, cursed_by, channel_id)"
                " VALUES (%s,'curse',%s,%s,%s)",
                (int(uid), data.get("remaining"), data.get("cursed_by"), data.get("channel_id")),
            )


async def save_tax(tax_data: dict):
    state.active_taxes = tax_data
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='tax'")
        for uid, data in tax_data.items():
            await cur.execute(
                "INSERT INTO shop_effects (user_id, effect_type, master_id, tax_type, tax_emoji, channel_id, activated_at)"
                " VALUES (%s,'tax',%s,%s,%s,%s,%s)",
                (int(uid), data.get("master"), data.get("type", "tax"),
                 data.get("emoji", "💰"), data.get("channel_id"), data.get("activated_at")),
            )
