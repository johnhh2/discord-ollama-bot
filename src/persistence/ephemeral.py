from src.db import with_cursor


async def save_restart_msg(channel_id: int, message_id: int):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO restart_msg (id, channel_id, message_id) VALUES (1,%s,%s)"
            " ON DUPLICATE KEY UPDATE channel_id=VALUES(channel_id), message_id=VALUES(message_id)",
            (channel_id, message_id),
        )


async def load_restart_msg() -> dict:
    async with with_cursor() as cur:
        await cur.execute("SELECT channel_id, message_id FROM restart_msg WHERE id=1")
        row = await cur.fetchone()
    if row:
        return {"channel_id": row[0], "message_id": row[1]}
    return {}


async def clear_restart_msg():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM restart_msg WHERE id=1")


async def add_ephemeral_msg(channel_id: int, message_id: int):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO ephemeral_msgs (channel_id, message_id) VALUES (%s,%s)",
            (channel_id, message_id),
        )


async def load_and_clear_ephemeral_msgs() -> list:
    async with with_cursor() as cur:
        await cur.execute("SELECT id, channel_id, message_id FROM ephemeral_msgs")
        rows = await cur.fetchall()
        if rows:
            # Delete only what we fetched — a row inserted by a concurrent
            # add_ephemeral_msg between the SELECT and a blanket DELETE would
            # vanish without ever being returned (message never cleaned up).
            await cur.execute(
                "DELETE FROM ephemeral_msgs WHERE id <= %s",
                (max(r[0] for r in rows),),
            )
    return [{"channel_id": r[1], "message_id": r[2]} for r in rows]
