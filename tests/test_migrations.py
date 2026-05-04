"""Tests for the schema migration runner.

Cover the four invariants that matter:
  1. Empty DB → migrations apply in order, schema_migrations is populated.
  2. Re-run → no-op (idempotent).
  3. Edited file → checksum mismatch raises.
  4. Gap in version numbering → discovery raises.

Each test builds an isolated in-memory SQLite + a fresh `migrations/` dir
under tmp_path, so tests don't touch the real migration files.
"""
import sqlite3
from pathlib import Path

import pytest

from src import migrations as _migrations
from tests.fakes.db import FakeCursor


@pytest.fixture
def fake_cur():
    """In-memory SQLite cursor wrapped in the FakeCursor translator. Auto-commit."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    yield FakeCursor(conn.cursor())
    conn.close()


@pytest.fixture(autouse=True)
def reset_runner_done():
    """Reset the runner's per-process guard so tests can call it freely."""
    _migrations._done = False
    yield
    _migrations._done = False


def _write(dir_path: Path, name: str, contents: str) -> Path:
    p = dir_path / name
    p.write_text(contents, encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_empty_db_applies_all_migrations_in_order(tmp_path, fake_cur):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE foo (id INTEGER PRIMARY KEY);")
    _write(tmp_path, "0002_second.sql", "CREATE TABLE bar (id INTEGER PRIMARY KEY);")

    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)

    assert applied == [1, 2]
    await fake_cur.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    rows = await fake_cur.fetchall()
    assert rows == [(1, "0001_first"), (2, "0002_second")]
    # Real tables exist
    await fake_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('foo','bar')")
    table_names = {r[0] for r in await fake_cur.fetchall()}
    assert table_names == {"foo", "bar"}


@pytest.mark.asyncio
async def test_rerun_is_noop(tmp_path, fake_cur):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE foo (id INTEGER PRIMARY KEY);")

    first = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert first == [1]

    # The per-process _done guard would short-circuit a no-cur call. With cur=
    # explicit (test path), the runner re-checks the DB and finds nothing pending.
    second = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert second == []


@pytest.mark.asyncio
async def test_checksum_mismatch_raises(tmp_path, fake_cur):
    path = _write(tmp_path, "0001_first.sql", "CREATE TABLE foo (id INTEGER PRIMARY KEY);")
    await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)

    # Edit the file post-application — simulating someone tweaking a migration
    # that's already been deployed.
    path.write_text("CREATE TABLE foo (id INTEGER PRIMARY KEY, extra TEXT);", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)


@pytest.mark.asyncio
async def test_gap_in_version_numbering_raises(tmp_path, fake_cur):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE foo (id INTEGER PRIMARY KEY);")
    _write(tmp_path, "0003_third.sql", "CREATE TABLE baz (id INTEGER PRIMARY KEY);")

    with pytest.raises(RuntimeError, match="gap in migration versions"):
        await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)


@pytest.mark.asyncio
async def test_baseline_heuristic_marks_existing_db_without_running(tmp_path, fake_cur):
    # Simulate an existing prod DB: create the sentinel table directly, then
    # point the runner at a migration set whose first file would otherwise
    # try to recreate it (with a deliberately-broken statement we want to
    # confirm does NOT execute).
    await fake_cur.execute("CREATE TABLE economy_users (user_id INTEGER PRIMARY KEY)")
    _write(
        tmp_path,
        "0001_baseline.sql",
        "INTENTIONALLY INVALID SQL — must not be executed against existing DB;",
    )
    _write(tmp_path, "0002_add_thing.sql", "CREATE TABLE thing (id INTEGER PRIMARY KEY);")

    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)

    # 0001 was marked applied via heuristic (not executed); 0002 ran normally.
    assert applied == [2]
    await fake_cur.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    rows = await fake_cur.fetchall()
    assert rows == [(1, "0001_baseline"), (2, "0002_add_thing")]
    # `thing` table exists; the broken baseline never ran.
    await fake_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='thing'")
    assert await fake_cur.fetchone() is not None


@pytest.mark.asyncio
async def test_real_migrations_apply_against_empty_db(fake_cur):
    """Smoke test: the actual `migrations/` files in the repo apply cleanly
    against a fresh SQLite DB. Catches MariaDB-isms in real migrations that
    the translator doesn't yet handle.
    """
    real_dir = Path(__file__).resolve().parent.parent / "migrations"
    applied = await _migrations.run_migrations(migrations_dir=real_dir, cur=fake_cur)
    assert applied  # at least one migration applied

    await fake_cur.execute("SELECT COUNT(*) FROM schema_migrations")
    (count,) = await fake_cur.fetchone()
    assert count == len(applied)
