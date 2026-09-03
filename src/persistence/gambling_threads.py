"""gambling_threads table — the !session registry (src/gambling/session.py).

One row per open gambling thread, mirroring state.gambling_threads so a
reboot keeps the thread's command gate, its P/L tally (the thread name)
and `!stop` close working.
"""
import json

from src import state
from src.db import with_cursor


_UPSERT_SQL = (
    "INSERT INTO gambling_threads (thread_id, owner_id, guild_id, parent_id, created_at, tally_json) "
    "VALUES (%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE owner_id=VALUES(owner_id), guild_id=VALUES(guild_id), "
    "parent_id=VALUES(parent_id), created_at=VALUES(created_at), tally_json=VALUES(tally_json)"
)


def _tally_json(row: dict) -> str:
    return json.dumps({
        str(uid): {"net": int(entry.get("net", 0)), "name": entry.get("name")}
        for uid, entry in (row.get("tally") or {}).items()
    })


def parse_tally(raw) -> dict:
    """tally_json → {uid: {"net": int, "name": str|None}} with int keys
    (JSON object keys are always strings)."""
    return {
        int(uid): {"net": int(entry.get("net", 0)), "name": entry.get("name")}
        for uid, entry in (json.loads(raw) if raw else {}).items()
    }


async def save_gambling_thread(thread_id: int) -> None:
    """Mirror state.gambling_threads[thread_id] to the DB (delete when absent)."""
    row = state.gambling_threads.get(thread_id)
    async with with_cursor() as cur:
        if row is None:
            await cur.execute("DELETE FROM gambling_threads WHERE thread_id=%s", (int(thread_id),))
            return
        await cur.execute(_UPSERT_SQL, (
            int(thread_id), int(row["owner_id"]), int(row["guild_id"]),
            int(row["parent_id"]), int(row["created_at"]), _tally_json(row),
        ))


async def delete_gambling_thread(thread_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM gambling_threads WHERE thread_id=%s", (int(thread_id),))
