"""Migration 0059: house pots stranded by the ungated 9/1/2026 lottery draws
move into the active September 2026 lotteries that started without them.

The draw was supposed to seed each fresh pool with the guild's house pot
(drain_bot_balance_into_lottery), but a scheduler tick before init_db_state
finished saw an empty in-memory guild_house and drained 0, leaving the coins
in the DB row. The migration moves those pots into the lotteries running the
202609 cycle and zeroes the corresponding house balances; everything else is
left alone.
"""
from pathlib import Path

import pytest

from src import migrations as _migrations
from src.db import with_cursor


pytestmark = pytest.mark.asyncio

MIGRATION = "0059_backfill_house_pot_into_active_lotteries.sql"

SEPT = 202609
AUG = 202608


async def _apply() -> None:
    """Run the migration file's statements against the test DB.

    The `db` fixture already applied every migration to an empty DB, so this
    both exercises the backfill against seeded data AND proves the file is
    safe to run twice.
    """
    sql = (Path(_migrations._MIGRATIONS_DIR) / MIGRATION).read_text(encoding="utf-8")
    async with with_cursor() as cur:
        for stmt in _migrations._split_statements(sql):
            await cur.execute(stmt)


async def _seed_lottery(gid: int, pool: int, drawn: int, posted: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO lottery (guild_id, prize_pool, last_posted_week, last_drawn_week)"
            " VALUES (%s,%s,%s,%s)",
            (gid, pool, posted, drawn),
        )


async def _seed_house(gid: int, balance: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO guild_house_balance (guild_id, balance) VALUES (%s,%s)",
            (gid, balance),
        )


async def _pool(gid: int) -> int:
    async with with_cursor() as cur:
        await cur.execute("SELECT prize_pool FROM lottery WHERE guild_id=%s", (gid,))
        return (await cur.fetchone())[0]


async def _house(gid: int) -> int:
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT balance FROM guild_house_balance WHERE guild_id=%s", (gid,)
        )
        return (await cur.fetchone())[0]


async def test_active_lottery_receives_house_pot(db):
    """A lottery drawn+announced in September gets its guild's house pot,
    and the house pot is zeroed (moved, not copied)."""
    await _seed_lottery(1, 52_000, drawn=SEPT, posted=SEPT)
    await _seed_house(1, 800_000)

    await _apply()

    assert await _pool(1) == 852_000
    assert await _house(1) == 0


async def test_posted_but_not_drawn_counts_as_active(db):
    """A lottery created fresh mid-September via !settings has
    last_drawn_week 0 but is running the 202609 cycle — it counts."""
    await _seed_lottery(2, 51_000, drawn=0, posted=SEPT)
    await _seed_house(2, 40_000)

    await _apply()

    assert await _pool(2) == 91_000
    assert await _house(2) == 0


async def test_stale_lottery_and_its_house_pot_untouched(db):
    """A guild still on an older cycle (lottery disabled, or the bot missed
    its draw) keeps both its pool and its house pot — the fixed drain moves
    the pot whenever that guild's next lottery actually starts."""
    await _seed_lottery(3, 9_000, drawn=AUG, posted=AUG)
    await _seed_house(3, 40_000)

    await _apply()

    assert await _pool(3) == 9_000
    assert await _house(3) == 40_000


async def test_active_lottery_without_house_row_unchanged(db):
    """No guild_house_balance row: COALESCE adds 0, pool unchanged."""
    await _seed_lottery(4, 52_000, drawn=SEPT, posted=SEPT)

    await _apply()

    assert await _pool(4) == 52_000


async def test_rerunning_whole_file_is_a_noop(db):
    """After the first pass the in-scope house balances are 0, so a rerun
    adds nothing and the pool doesn't double-count."""
    await _seed_lottery(5, 52_000, drawn=SEPT, posted=SEPT)
    await _seed_house(5, 10_000)

    await _apply()
    await _apply()

    assert await _pool(5) == 62_000
    assert await _house(5) == 0
