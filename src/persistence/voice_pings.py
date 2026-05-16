from src.db import with_cursor


async def save_voice_ping(guild_id: int, channel_id: int, user_id: int) -> None:
    """Insert (or upsert without resetting last_pinged_at) a voice-channel subscription."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO voice_pings (guild_id, channel_id, user_id, last_pinged_at)"
            " VALUES (%s,%s,%s,NULL)"
            " ON DUPLICATE KEY UPDATE guild_id=VALUES(guild_id)",
            (guild_id, channel_id, user_id),
        )


async def delete_voice_ping(channel_id: int, user_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM voice_pings WHERE channel_id=%s AND user_id=%s",
            (channel_id, user_id),
        )


async def update_voice_ping_last_pinged(channel_id: int, user_id: int, ts: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "UPDATE voice_pings SET last_pinged_at=%s WHERE channel_id=%s AND user_id=%s",
            (ts, channel_id, user_id),
        )


async def load_voice_pings() -> dict:
    """Return {(channel_id, user_id): {"guild_id": int, "last_pinged_at": int|None}}."""
    async with with_cursor() as cur:
        await cur.execute("SELECT guild_id, channel_id, user_id, last_pinged_at FROM voice_pings")
        return {
            (int(r[1]), int(r[2])): {
                "guild_id": int(r[0]),
                "last_pinged_at": int(r[3]) if r[3] is not None else None,
            }
            for r in await cur.fetchall()
        }
