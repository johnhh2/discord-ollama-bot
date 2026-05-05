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
