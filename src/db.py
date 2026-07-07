import asyncio
import os
from contextlib import asynccontextmanager

import aiomysql

_pool = None
_pool_lock: asyncio.Lock | None = None


async def get_pool() -> aiomysql.Pool:
    global _pool, _pool_lock
    if _pool is not None:
        return _pool
    # Lazy lock creation keeps module import free of event-loop requirements.
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        if _pool is None:
            _pool = await _create_pool()
    return _pool


async def _create_pool() -> aiomysql.Pool:
    return await aiomysql.create_pool(
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


@asynccontextmanager
async def with_transaction():
    """Like with_cursor(), but the whole block is one transaction: statements
    commit together on success and roll back if the block raises.

    The pool is autocommit=True, so the delete-then-reinsert savers otherwise
    run each statement standalone — a crash or dropped connection between the
    DELETE and the reinsert loop permanently destroys every row of that type.
    Use this for any multi-statement write that must be all-or-nothing.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                yield cur
        except BaseException:
            await conn.rollback()
            raise
        await conn.commit()
