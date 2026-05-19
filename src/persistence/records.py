import json

from src.db import with_cursor


async def load_records(guild_id: int) -> dict:
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT category, value, holder_id, holder_name, extra_json FROM records WHERE guild_id=%s",
            (guild_id,),
        )
        rows = await cur.fetchall()
    result = {}
    for cat, val, holder_id, holder_name, extra_json in rows:
        entry = {"value": val, "holder_id": holder_id, "holder_name": holder_name}
        if extra_json:
            entry.update(json.loads(extra_json))
        result[cat] = entry
    return result


async def load_global_records() -> dict:
    """Top record per category across every guild."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT category, value, holder_id, holder_name, extra_json FROM records",
        )
        rows = await cur.fetchall()
    result = {}
    for cat, val, holder_id, holder_name, extra_json in rows:
        existing = result.get(cat)
        if existing is not None and val <= existing["value"]:
            continue
        entry = {"value": val, "holder_id": holder_id, "holder_name": holder_name}
        if extra_json:
            entry.update(json.loads(extra_json))
        result[cat] = entry
    return result


async def save_records(guild_id: int, records: dict):
    async with with_cursor() as cur:
        for cat, data in records.items():
            known_keys = {"value", "holder_id", "holder_name"}
            extra = {k: v for k, v in data.items() if k not in known_keys}
            await cur.execute(
                "INSERT INTO records (guild_id, category, value, holder_id, holder_name, extra_json)"
                " VALUES (%s,%s,%s,%s,%s,%s)"
                " ON DUPLICATE KEY UPDATE value=VALUES(value), holder_id=VALUES(holder_id),"
                " holder_name=VALUES(holder_name), extra_json=VALUES(extra_json)",
                (guild_id, cat, data["value"], data["holder_id"], data["holder_name"],
                 json.dumps(extra) if extra else None),
            )


async def try_set_record(guild_id: int, category: str, value: int, holder_id: int, holder_name: str, **meta) -> bool:
    if guild_id is None:
        return False
    records = await load_records(guild_id)
    current = records.get(category, {})
    if value > current.get("value", -1):
        records[category] = {"value": value, "holder_id": holder_id, "holder_name": holder_name, **meta}
        await save_records(guild_id, records)
        return True
    return False


async def is_global_top(category: str, value: int, source_guild_id: int) -> bool:
    """Return True iff *value* strictly beats the best value held by any guild
    other than *source_guild_id* for *category*. Used to decide whether a new
    per-guild record also constitutes a new global record.

    Call this AFTER try_set_record has persisted the new value — the source
    guild is excluded so its own freshly-written row doesn't shadow the
    comparison.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT MAX(value) FROM records WHERE category=%s AND guild_id<>%s",
            (category, source_guild_id),
        )
        row = await cur.fetchone()
    other_max = row[0] if row and row[0] is not None else -1
    return value > other_max
