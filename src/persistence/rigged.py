from src import state
from src.db import with_transaction


async def save_rigged_slots():
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM rigged_slots")
        for uid, symbol in state.rigged_slots.items():
            await cur.execute(
                "INSERT INTO rigged_slots (user_id, symbol) VALUES (%s,%s)",
                (int(uid), symbol),
            )


async def save_rigged_flips():
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM rigged_flips")
        for uid, wins in state.rigged_flips.items():
            await cur.execute(
                "INSERT INTO rigged_flips (user_id, remaining_wins) VALUES (%s,%s)",
                (int(uid), wins),
            )


async def save_rigged_scratch():
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM rigged_scratch")
        for uid, count in state.rigged_scratch.items():
            await cur.execute(
                "INSERT INTO rigged_scratch (user_id, symbols_count) VALUES (%s,%s)",
                (int(uid), count),
            )


async def save_rigged_steal():
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM rigged_steal")
        for uid, remaining in state.rigged_steal.items():
            await cur.execute(
                "INSERT INTO rigged_steal (user_id, remaining_successes) VALUES (%s,%s)",
                (int(uid), remaining),
            )
