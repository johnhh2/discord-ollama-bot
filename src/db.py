import os
from contextlib import asynccontextmanager

import aiomysql

_pool = None


async def get_pool() -> aiomysql.Pool:
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", 3306)),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            db=os.environ["DB_NAME"],
            autocommit=True,
            charset="utf8mb4",
            minsize=2,
            maxsize=10,
            init_command="SET sql_notes=0",
        )
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


@asynccontextmanager
async def with_cursor():
    """`async with with_cursor() as cur:` — opens a cursor on a pooled
    connection and yields it. Replaces the 3-line
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
    boilerplate that occurs ~25 times across persistence.py.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            yield cur
