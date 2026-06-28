import json

from src import state
from src.db import with_cursor


# All shop effects are scoped per server: state dicts are keyed by a
# (guild_id, user_id) int tuple, and each row carries guild_id. The save_*
# helpers below delete-and-reinsert their whole effect type, so a single PK
# (guild_id, user_id, effect_type) row exists per (guild, user).


async def save_ragebait():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='ragebait'")
        for (guild_id, uid), data in state.active_ragebaits.items():
            await cur.execute(
                "INSERT INTO shop_effects (guild_id, user_id, effect_type, remaining, started_by, history_json, channel_id)"
                " VALUES (%s,%s,'ragebait',%s,%s,%s,%s)",
                (int(guild_id), int(uid), data.get("remaining"), data.get("started_by"),
                 json.dumps(data.get("history", [])), data.get("channel_id")),
            )


async def save_mock():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='mock'")
        for (guild_id, uid), data in state.active_mocks.items():
            await cur.execute(
                "INSERT INTO shop_effects (guild_id, user_id, effect_type, remaining, started_by, channel_id)"
                " VALUES (%s,%s,'mock',%s,%s,%s)",
                (int(guild_id), int(uid), data.get("remaining"), data.get("started_by"), data.get("channel_id")),
            )


async def save_curse(curse_data: dict):
    state.active_curses = curse_data
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='curse'")
        for (guild_id, uid), data in curse_data.items():
            await cur.execute(
                "INSERT INTO shop_effects (guild_id, user_id, effect_type, remaining, cursed_by, channel_id)"
                " VALUES (%s,%s,'curse',%s,%s,%s)",
                (int(guild_id), int(uid), data.get("remaining"), data.get("cursed_by"), data.get("channel_id")),
            )


async def save_tax(tax_data: dict):
    state.active_taxes = tax_data
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='tax'")
        for (guild_id, uid), data in tax_data.items():
            await cur.execute(
                "INSERT INTO shop_effects (guild_id, user_id, effect_type, master_id, tax_type, tax_emoji, channel_id, activated_at, expires_at)"
                " VALUES (%s,%s,'tax',%s,%s,%s,%s,%s,%s)",
                (int(guild_id), int(uid), data.get("master"), data.get("type", "tax"),
                 data.get("emoji", "💰"), data.get("channel_id"), data.get("activated_at"),
                 data.get("expires_at")),
            )


async def save_spellcheck():
    """Persist active spellchecks. `remaining` holds the number of purchased
    days; expiry is stored in expires_at (also recomputable from activated_at)."""
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='spellcheck'")
        for (guild_id, uid), data in state.active_spellchecks.items():
            await cur.execute(
                "INSERT INTO shop_effects (guild_id, user_id, effect_type, master_id, remaining, channel_id, activated_at, expires_at)"
                " VALUES (%s,%s,'spellcheck',%s,%s,%s,%s,%s)",
                (int(guild_id), int(uid), data.get("started_by"), data.get("days"),
                 data.get("channel_id"), data.get("activated_at"), data.get("expires_at")),
            )
