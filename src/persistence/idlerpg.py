"""idle_characters / idle_quests tables — !idle (src/cogs/idle_cog.py).
Mirrors state.idle_characters and state.idle_quests."""
import json

from src import state
from src.db import with_cursor


def parse_items(items_json: "str | None") -> dict:
    try:
        items = json.loads(items_json or "{}")
    except ValueError:
        return {}
    return items if isinstance(items, dict) else {}


def parse_members(members_json: "str | None") -> list:
    try:
        members = json.loads(members_json or "[]")
    except ValueError:
        return []
    return [int(uid) for uid in members] if isinstance(members, list) else []


async def save_idle_character(guild_id: int, user_id: int) -> None:
    """Mirror the whole of state.idle_characters[guild_id][user_id], so a
    call site can't drop a column."""
    c = state.idle_characters[guild_id][user_id]
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO idle_characters (guild_id, user_id, class_name, level, next_level_at, remaining, "
            "law, moral, prestige, penalty_total, last_seen, last_penalty_at, thread_id, created_at, "
            "align_changed_at, duel_day, items_json) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE class_name=VALUES(class_name), level=VALUES(level), "
            "next_level_at=VALUES(next_level_at), remaining=VALUES(remaining), law=VALUES(law), "
            "moral=VALUES(moral), prestige=VALUES(prestige), penalty_total=VALUES(penalty_total), "
            "last_seen=VALUES(last_seen), last_penalty_at=VALUES(last_penalty_at), "
            "thread_id=VALUES(thread_id), created_at=VALUES(created_at), "
            "align_changed_at=VALUES(align_changed_at), duel_day=VALUES(duel_day), "
            "items_json=VALUES(items_json)",
            (
                int(guild_id), int(user_id), c["class"], int(c["level"]), c["next_level_at"], c["remaining"],
                c["law"], c["moral"], int(c["prestige"]), int(c["penalty_total"]), int(c["last_seen"]),
                int(c["last_penalty_at"]), c["thread_id"], int(c["created_at"]),
                int(c["align_changed_at"]), c["duel_day"], json.dumps(c["items"]),
            ),
        )


async def delete_idle_character(guild_id: int, user_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM idle_characters WHERE guild_id=%s AND user_id=%s", (int(guild_id), int(user_id)),
        )


async def delete_idle_guild(guild_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM idle_characters WHERE guild_id=%s", (int(guild_id),))
        await cur.execute("DELETE FROM idle_quests WHERE guild_id=%s", (int(guild_id),))


async def save_idle_quest(guild_id: int) -> None:
    q = state.idle_quests[guild_id]
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO idle_quests (guild_id, members_json, description, ends_at, not_before) "
            "VALUES (%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE members_json=VALUES(members_json), description=VALUES(description), "
            "ends_at=VALUES(ends_at), not_before=VALUES(not_before)",
            (int(guild_id), json.dumps(q["members"]), q["description"], q["ends_at"], int(q["not_before"])),
        )
