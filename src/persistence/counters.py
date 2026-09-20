"""counters / counter_values / counter_perms tables — !count and !counter
(src/cogs/counter_cog.py). Mirrors state.counters and state.counter_perms."""
import time

from src import state
from src.db import with_cursor


async def save_counter(guild_id: int, name: str) -> None:
    """Mirror the definition of state.counters[guild_id][name] (not its values)."""
    row = state.counters[guild_id][name]
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO counters (guild_id, name, description, user_required, kind, created_by, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE description=VALUES(description), "
            "user_required=VALUES(user_required), kind=VALUES(kind), "
            "created_by=VALUES(created_by), created_at=VALUES(created_at)",
            (
                int(guild_id), name, row["description"], int(row["user_required"]),
                row["kind"], int(row["created_by"]), int(row["created_at"]),
            ),
        )


async def delete_counter(guild_id: int, name: str) -> None:
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM counter_values WHERE guild_id=%s AND name=%s", (int(guild_id), name))
        await cur.execute("DELETE FROM counters WHERE guild_id=%s AND name=%s", (int(guild_id), name))


async def save_counter_value(guild_id: int, name: str, user_id: int, value: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO counter_values (guild_id, name, user_id, value) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE value=VALUES(value)",
            (int(guild_id), name, int(user_id), int(value)),
        )


async def save_counter_perm(guild_id: int, user_id: int, granted_by: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO counter_perms (guild_id, user_id, granted_by, granted_at) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE granted_by=VALUES(granted_by), granted_at=VALUES(granted_at)",
            (int(guild_id), int(user_id), int(granted_by), int(time.time())),
        )


async def delete_counter_perm(guild_id: int, user_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM counter_perms WHERE guild_id=%s AND user_id=%s", (int(guild_id), int(user_id)),
        )
