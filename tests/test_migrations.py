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


# ── Reverse migrations ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revert_runs_down_and_removes_row(tmp_path, fake_cur):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE foo (id INTEGER PRIMARY KEY);")
    _write(tmp_path, "0002_add_bar.sql", "CREATE TABLE bar (id INTEGER PRIMARY KEY);")
    _write(tmp_path, "0002_add_bar.down.sql", "DROP TABLE bar;")

    await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)

    name = await _migrations.revert_migration(2, migrations_dir=tmp_path, cur=fake_cur)
    assert name == "0002_add_bar"

    # Row gone from schema_migrations
    await fake_cur.execute("SELECT version FROM schema_migrations ORDER BY version")
    rows = await fake_cur.fetchall()
    assert rows == [(1,)]

    # bar table actually dropped
    await fake_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bar'")
    assert await fake_cur.fetchone() is None

    # And re-applying the forward migration on next boot just works
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [2]
    await fake_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bar'")
    assert await fake_cur.fetchone() is not None


@pytest.mark.asyncio
async def test_revert_without_down_file_raises(tmp_path, fake_cur):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE foo (id INTEGER PRIMARY KEY);")
    # No 0001_first.down.sql.
    await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)

    with pytest.raises(RuntimeError, match="no .down.sql"):
        await _migrations.revert_migration(1, migrations_dir=tmp_path, cur=fake_cur)


@pytest.mark.asyncio
async def test_revert_version_never_applied_raises(tmp_path, fake_cur):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE foo (id INTEGER PRIMARY KEY);")
    _write(tmp_path, "0001_first.down.sql", "DROP TABLE foo;")
    # schema_migrations table will be created by revert_migration but stay empty
    # because we never call run_migrations.

    with pytest.raises(RuntimeError, match="not recorded as applied"):
        await _migrations.revert_migration(1, migrations_dir=tmp_path, cur=fake_cur)


@pytest.mark.asyncio
async def test_down_files_dont_count_as_forward_migrations(tmp_path, fake_cur):
    """A NNNN_xxx.down.sql with no matching forward at NNNN must not create a
    phantom forward migration that would trigger gap/duplicate errors.
    """
    _write(tmp_path, "0001_first.sql", "CREATE TABLE foo (id INTEGER PRIMARY KEY);")
    _write(tmp_path, "0001_first.down.sql", "DROP TABLE foo;")
    _write(tmp_path, "0002_second.sql", "CREATE TABLE bar (id INTEGER PRIMARY KEY);")
    _write(tmp_path, "0002_second.down.sql", "DROP TABLE bar;")

    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [1, 2]


@pytest.mark.asyncio
async def test_0055_collapses_per_guild_insurance_to_global(tmp_path, fake_cur):
    """The real 0055 data migration: per-guild insurance rows collapse into one
    guild_id=0 row per user carrying the LATEST expiry; multi-guild subs
    collapse to one; other effect types are untouched; re-run is a no-op."""
    _write(tmp_path, "0001_shop_effects.sql", """
        CREATE TABLE shop_effects (
            guild_id INTEGER NOT NULL DEFAULT 0,
            user_id INTEGER NOT NULL,
            effect_type TEXT NOT NULL,
            expires_at DOUBLE NULL,
            history_json TEXT NULL,
            PRIMARY KEY (guild_id, user_id, effect_type)
        );
    """)
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [1]

    # Old-style per-guild rows: user 10 insured in two guilds (different
    # expiries) and subscribed in both; user 12 subscribed elsewhere; a tax
    # row that must survive untouched.
    seed = [
        (1, 10, "insurance", 1_000.0, '["steal"]'),
        (2, 10, "insurance", 2_000.0, '["tax"]'),
        (3, 11, "insurance", 500.0, '["mock"]'),
        (1, 10, "insurance_sub", None, None),
        (2, 10, "insurance_sub", None, None),
        (5, 12, "insurance_sub", None, None),
        (7, 13, "tax", 999.0, None),
    ]
    for row in seed:
        await fake_cur.execute(
            "INSERT INTO shop_effects (guild_id, user_id, effect_type, expires_at, history_json)"
            " VALUES (%s,%s,%s,%s,%s)", row,
        )

    # Ship the REAL 0055 file as the next migration in this sandbox.
    real_sql = (Path(__file__).parent.parent / "migrations" / "0055_make_insurance_global.sql").read_text(encoding="utf-8")
    _write(tmp_path, "0002_make_insurance_global.sql", real_sql)
    _migrations._done = False
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [2]

    async def _rows(effect_type):
        await fake_cur.execute(
            "SELECT guild_id, user_id, expires_at FROM shop_effects"
            " WHERE effect_type=%s ORDER BY user_id", (effect_type,),
        )
        return await fake_cur.fetchall()

    # One global insurance row per user, at the user's latest expiry.
    assert await _rows("insurance") == [(0, 10, 2_000.0), (0, 11, 500.0)]
    # Multi-guild subs collapsed to one global row each.
    assert [(g, u) for g, u, _ in await _rows("insurance_sub")] == [(0, 10), (0, 12)]
    # Non-insurance effects untouched.
    assert await _rows("tax") == [(7, 13, 999.0)]

    # Retry-safety: force the runner to re-execute 0002's statements (simulating
    # a crash after a partial apply) — the NOT IN guards make it a no-op.
    await fake_cur.execute("DELETE FROM schema_migrations WHERE version=2")
    _migrations._done = False
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [2]
    assert await _rows("insurance") == [(0, 10, 2_000.0), (0, 11, 500.0)]


@pytest.mark.asyncio
async def test_0063_chess_ticket_period_rename_keeps_september_grants(tmp_path, fake_cur):
    """The real 0063 data migration: chess_week is renamed to chess_period in
    place (data kept), every ISO-week key — all from the September 2026
    lottery, the first with free grants — folds into its "2026-09" period,
    NULL gates stay NULL, and a re-run (CHANGE COLUMN IF EXISTS on a column
    that's already gone) is a no-op."""
    _write(tmp_path, "0001_ticket_grants.sql", """
        CREATE TABLE lottery_ticket_grants (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            daily_day TEXT NULL,
            chess_week TEXT NULL,
            chess_tickets INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
    """)
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [1]

    seed = [
        (1, 10, "2026-09-02", "2026-W36", 2),
        (2, 10, None, "2026-W37", 1),
        (1, 11, "2026-09-02", None, 0),
    ]
    for row in seed:
        await fake_cur.execute(
            "INSERT INTO lottery_ticket_grants"
            " (guild_id, user_id, daily_day, chess_week, chess_tickets)"
            " VALUES (%s,%s,%s,%s,%s)", row,
        )

    # Ship the REAL 0063 file as the next migration in this sandbox.
    real_sql = (Path(__file__).parent.parent / "migrations" / "0063_chess_ticket_monthly_period.sql").read_text(encoding="utf-8")
    _write(tmp_path, "0002_chess_ticket_monthly_period.sql", real_sql)
    _migrations._done = False
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [2]

    async def _rows():
        await fake_cur.execute(
            "SELECT guild_id, user_id, daily_day, chess_period, chess_tickets"
            " FROM lottery_ticket_grants ORDER BY user_id, guild_id"
        )
        return await fake_cur.fetchall()

    expected = [
        (1, 10, "2026-09-02", "2026-09", 2),
        (2, 10, None, "2026-09", 1),
        (1, 11, "2026-09-02", None, 0),
    ]
    assert await _rows() == expected

    # Retry-safety: force the runner to re-execute 0002's statements
    # (simulating a crash after a partial apply) — the rename is skipped
    # and no week keys remain to fold.
    await fake_cur.execute("DELETE FROM schema_migrations WHERE version=2")
    _migrations._done = False
    applied = await _migrations.run_migrations(migrations_dir=tmp_path, cur=fake_cur)
    assert applied == [2]
    assert await _rows() == expected
