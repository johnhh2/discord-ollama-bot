from src.db import with_cursor


# ── Balance / bot stats history ───────────────────────────────────────────────

async def load_balance_history() -> dict:
    """Return {date_str: {bucket: {uid_str: {"wallet": int, "savings": int}}}}.
    The bucket layer (0..3) splits each calendar day into 6h CT windows.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT snapshot_date, bucket, user_id, wallet, savings FROM balance_history"
        )
        rows = await cur.fetchall()
    result: dict = {}
    for date_str, bucket, uid, wallet, savings in rows:
        result.setdefault(date_str, {}).setdefault(int(bucket), {})[str(uid)] = {
            "wallet": wallet, "savings": savings,
        }
    return result


async def save_balance_history(history: dict):
    """Accepts {date_str: {bucket: {uid_str: {"wallet": …, "savings": …}}}}."""
    async with with_cursor() as cur:
        for date_str, by_bucket in history.items():
            for bucket, users in by_bucket.items():
                for uid_str, vals in users.items():
                    await cur.execute(
                        "INSERT INTO balance_history"
                        " (snapshot_date, bucket, user_id, wallet, savings)"
                        " VALUES (%s,%s,%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE wallet=VALUES(wallet), savings=VALUES(savings)",
                        (date_str, int(bucket), int(uid_str),
                         vals.get("wallet", 0), vals.get("savings", 0)),
                    )


async def load_bot_stats_history() -> dict:
    """Return {date_str: {bucket: {"messages": …, "commands": …, ...}}}."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT snapshot_date, bucket, messages, commands, ai_responses, ai_up, memory_mb, ping_ms"
            " FROM bot_stats_history"
        )
        rows = await cur.fetchall()
    result: dict = {}
    for date_str, bucket, msgs, cmds, ai_resp, ai_up, mem, ping in rows:
        result.setdefault(date_str, {})[int(bucket)] = {
            "messages": msgs, "commands": cmds, "ai_responses": ai_resp,
            "ai_up": bool(ai_up), "memory_mb": mem, "ping_ms": ping,
        }
    return result


async def save_bot_stats_history(history: dict):
    """Accepts {date_str: {bucket: {"messages": …, ...}}}."""
    async with with_cursor() as cur:
        for date_str, by_bucket in history.items():
            for bucket, vals in by_bucket.items():
                await cur.execute(
                    "INSERT INTO bot_stats_history"
                    " (snapshot_date, bucket, messages, commands, ai_responses, ai_up, memory_mb, ping_ms)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE messages=VALUES(messages), commands=VALUES(commands),"
                    " ai_responses=VALUES(ai_responses), ai_up=VALUES(ai_up), memory_mb=VALUES(memory_mb),"
                    " ping_ms=VALUES(ping_ms)",
                    (date_str, int(bucket), vals.get("messages", 0), vals.get("commands", 0),
                     vals.get("ai_responses", 0), vals.get("ai_up", False), vals.get("memory_mb", 0.0),
                     vals.get("ping_ms")),
                )


async def load_command_usage_history() -> dict:
    """Return {date_str: {bucket: {cog_name: count}}}."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT snapshot_date, bucket, cog_name, count FROM bot_command_usage_history"
        )
        rows = await cur.fetchall()
    result: dict = {}
    for date_str, bucket, cog, count in rows:
        result.setdefault(date_str, {}).setdefault(int(bucket), {})[cog] = count
    return result


async def save_command_usage_history(history: dict):
    """Accepts {date_str: {bucket: {cog_name: count}}}."""
    async with with_cursor() as cur:
        for date_str, by_bucket in history.items():
            for bucket, cogs in by_bucket.items():
                for cog, count in cogs.items():
                    await cur.execute(
                        "INSERT INTO bot_command_usage_history"
                        " (snapshot_date, bucket, cog_name, count) VALUES (%s,%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE count=VALUES(count)",
                        (date_str, int(bucket), cog, int(count)),
                    )


async def load_crime_history() -> dict:
    """Returns {date_str: {bucket: {(guild_id_int, uid_str): {"gained", "lost"}}}}.

    Keyed by (guild_id, user) — crime P/L is per-server (see migration
    0018). Pre-0018 rows carry guild_id=0 and naturally age out.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT snapshot_date, bucket, guild_id, user_id, gained, lost FROM crime_history"
        )
        rows = await cur.fetchall()
    result: dict = {}
    for date_str, bucket, gid, uid, gained, lost in rows:
        result.setdefault(date_str, {}).setdefault(int(bucket), {})[(int(gid), str(uid))] = {
            "gained": gained, "lost": lost,
        }
    return result


async def load_gambling_history() -> dict:
    """Returns {date_str: {bucket: {(guild_id_int, uid_str): {"gained", "lost"}}}}.

    Keyed by (guild_id, user) — gambling P/L is per-server (see migration
    0018). Pre-0018 rows carry guild_id=0 and naturally age out.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT snapshot_date, bucket, guild_id, user_id, gained, lost FROM gambling_history"
        )
        rows = await cur.fetchall()
    result: dict = {}
    for date_str, bucket, gid, uid, gained, lost in rows:
        result.setdefault(date_str, {}).setdefault(int(bucket), {})[(int(gid), str(uid))] = {
            "gained": gained, "lost": lost,
        }
    return result


async def prune_balance_history(*, before_date: str):
    """DELETE all rows with snapshot_date < before_date. Server-side
    prune (not load/filter/save) — these tables can grow large over
    years and we don't want to round-trip every row through Python."""
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM balance_history WHERE snapshot_date < %s",
            (before_date,),
        )


async def prune_bot_stats_history(*, before_date: str):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM bot_stats_history WHERE snapshot_date < %s",
            (before_date,),
        )


async def prune_command_usage_history(*, before_date: str):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM bot_command_usage_history WHERE snapshot_date < %s",
            (before_date,),
        )


async def prune_crime_history(*, before_date: str):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM crime_history WHERE snapshot_date < %s",
            (before_date,),
        )


async def prune_gambling_history(*, before_date: str):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM gambling_history WHERE snapshot_date < %s",
            (before_date,),
        )


async def prune_levelup_history(*, before_date: str):
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM levelup_history WHERE snapshot_date < %s",
            (before_date,),
        )


async def upsert_crime_delta(date_str: str, bucket: int, guild_id: int, uid: int, *, gained: int = 0, lost: int = 0):
    """Atomically add `gained` and `lost` deltas to the
    (date, bucket, guild, user) row in crime_history. Caller passes the
    per-event delta, not the running total — MariaDB increments in place
    via x = x + VALUES(x).
    """
    if gained == 0 and lost == 0:
        return
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO crime_history (snapshot_date, bucket, guild_id, user_id, gained, lost)"
            " VALUES (%s,%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE"
            " gained = gained + VALUES(gained),"
            " lost   = lost   + VALUES(lost)",
            (date_str, int(bucket), int(guild_id), int(uid), int(gained), int(lost)),
        )


async def upsert_gambling_delta(date_str: str, bucket: int, guild_id: int, uid: int, *, gained: int = 0, lost: int = 0):
    if gained == 0 and lost == 0:
        return
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO gambling_history (snapshot_date, bucket, guild_id, user_id, gained, lost)"
            " VALUES (%s,%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE"
            " gained = gained + VALUES(gained),"
            " lost   = lost   + VALUES(lost)",
            (date_str, int(bucket), int(guild_id), int(uid), int(gained), int(lost)),
        )


async def upsert_levelup_delta(date_str: str, bucket: int, guild_id: int, uid: int, *, count: int = 0):
    if count == 0:
        return
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO levelup_history (snapshot_date, bucket, guild_id, user_id, count)"
            " VALUES (%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE count = count + VALUES(count)",
            (date_str, int(bucket), int(guild_id), int(uid), int(count)),
        )


# Boot-time hydration helpers — load TODAY's row from each activity table so
# the in-memory dicts (used as a fast-read cache by the graph cog) reflect
# the truth on disk. Called from init_db_state.

async def load_today_crime_row(date_str: str, bucket: int) -> dict:
    """{(guild_id_int, uid_str): {"gained", "lost"}} for today's (date, bucket)."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT guild_id, user_id, gained, lost FROM crime_history"
            " WHERE snapshot_date = %s AND bucket = %s",
            (date_str, int(bucket)),
        )
        rows = await cur.fetchall()
    return {
        (int(gid), str(uid)): {"gained": int(g), "lost": int(l)}
        for gid, uid, g, l in rows
    }


async def load_today_gambling_row(date_str: str, bucket: int) -> dict:
    """{(guild_id_int, uid_str): {"gained", "lost"}} for today's (date, bucket)."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT guild_id, user_id, gained, lost FROM gambling_history"
            " WHERE snapshot_date = %s AND bucket = %s",
            (date_str, int(bucket)),
        )
        rows = await cur.fetchall()
    return {
        (int(gid), str(uid)): {"gained": int(g), "lost": int(l)}
        for gid, uid, g, l in rows
    }


async def load_today_levelups_row(date_str: str, bucket: int) -> dict:
    """{(guild_id_int, uid_str): count} for today's (date, bucket)."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT guild_id, user_id, count FROM levelup_history"
            " WHERE snapshot_date = %s AND bucket = %s",
            (date_str, int(bucket)),
        )
        rows = await cur.fetchall()
    return {(int(gid), str(uid)): int(c) for gid, uid, c in rows}


async def load_levelup_history() -> dict:
    """Returns {date_str: {bucket: {(guild_id_int, uid_str): count}}}."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT snapshot_date, bucket, guild_id, user_id, count FROM levelup_history"
        )
        rows = await cur.fetchall()
    result: dict = {}
    for date_str, bucket, gid, uid, count in rows:
        result.setdefault(date_str, {}).setdefault(int(bucket), {})[(int(gid), str(uid))] = int(count)
    return result
