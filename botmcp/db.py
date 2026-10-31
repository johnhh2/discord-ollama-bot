"""The sidecar's own aiomysql pool, on the SELECT-only account.

Separate from the bot's src/db.py on purpose: different credentials, a
read-only session, a statement time limit, and nothing else in this
process can write. `get_pool` is the seam the tests patch.
"""
from __future__ import annotations

import asyncio
import os

import aiomysql

_pool: "aiomysql.Pool | None" = None
_lock: "asyncio.Lock | None" = None

# Both belt and braces with the account's grants: a read-only session can't
# write even if a grant were ever widened, and a runaway SELECT is cut off
# before it can hold a pool slot or a table lock.
INIT_COMMAND = "SET SESSION tx_read_only=1, max_statement_time=5"


async def get_pool() -> aiomysql.Pool:
    global _pool, _lock
    if _pool is not None:
        return _pool
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _pool is None:
            _pool = await aiomysql.create_pool(
                host=os.environ["DB_HOST"],
                port=int(os.environ.get("DB_PORT") or 3306),
                user=os.environ["MCP_DB_USER"],
                password=os.environ["MCP_DB_PASSWORD"],
                db=os.environ["DB_NAME"],
                autocommit=True,
                charset="utf8mb4",
                minsize=1,
                maxsize=4,
                init_command=INIT_COMMAND,
            )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


async def fetch_all(sql: str, params: tuple = ()) -> list[tuple]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())
