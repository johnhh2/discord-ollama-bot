import json

from src.db import with_cursor

# Categories whose ties break on the lower holder user id instead of on
# "whoever got there first". A strict `>` comparison already gives every
# other category first-come-wins: the incumbent keeps the record until
# somebody strictly beats it. For these, an equal value held by a HIGHER
# user id is displaced by the lower one, so the holder is a pure function
# of (value, user id) rather than of arrival order. Both `try_set_record`
# and `load_global_records` consult this so the per-guild and cross-guild
# views agree on who holds a tied record.
UID_TIEBREAK_CATEGORIES = {"command_streak"}


def _beats(category: str, value: int, holder_id: int, current: dict) -> bool:
    """Does (value, holder_id) take `category` from the `current` holder?"""
    cur_val = current.get("value")
    if cur_val is None:
        return True
    if value != cur_val:
        return value > cur_val
    if category not in UID_TIEBREAK_CATEGORIES:
        return False
    cur_holder = current.get("holder_id")
    return cur_holder is not None and int(holder_id) < int(cur_holder)


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
        if existing is not None and not _beats(cat, val, holder_id, existing):
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
    if _beats(category, value, holder_id, current):
        records[category] = {"value": value, "holder_id": holder_id, "holder_name": holder_name, **meta}
        await save_records(guild_id, records)
        return True
    return False


async def is_global_top(category: str, value: int, source_guild_id: int) -> bool:
    """Return True iff *value* strictly beats the best value held by any guild
    other than *source_guild_id* for *category*. Used to decide whether a new
    per-guild record also constitutes a new global record.

    Deliberately strict even for UID_TIEBREAK_CATEGORIES: tying another
    guild's value shouldn't fan a "New Global Record!" embed out to every
    configured records channel, even when the uid tiebreak means this guild
    would win the display in `load_global_records`.

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
