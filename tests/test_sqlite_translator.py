"""Lock in tests/fakes/db.py:_translate() coverage of the MariaDB-isms used
by src/persistence.py.

The translator is what lets the rest of the test suite run real save_*/load_*
SQL against an in-memory SQLite. If a future persistence change introduces
a SQL pattern the translator can't handle, those tests would silently
"pass against the wrong shape" or raise at runtime in production. This
file makes sure every pattern persistence.py actually uses is exercised
directly against the translator + a real sqlite3 connection.

Three layers of coverage:
1. Pure-string translation (tests _translate() with known inputs).
2. Translator-dialect parity (every distinct SQL pattern in persistence.py
   gets executed against in-memory sqlite to confirm round-trip).
3. Schema-pin: every table in persistence.py's SQL has a _TABLE_PKS entry
   so ON DUPLICATE KEY UPDATE doesn't ValueError at translation time.
"""
import re
from pathlib import Path

import pytest

from tests.fakes.db import _translate, _TABLE_PKS, make_fake_pool


pytestmark = pytest.mark.asyncio


# ── _translate() string-level contract ────────────────────────────────────────

async def test_translate_replaces_percent_s_placeholders():
    sql = "INSERT INTO foo (a, b) VALUES (%s, %s)"
    out = _translate(sql)
    assert out == "INSERT INTO foo (a, b) VALUES (?, ?)"


async def test_translate_rewrites_insert_ignore():
    sql = "INSERT IGNORE INTO command_perms (command_name, tier, hidden) VALUES (%s, %s, %s)"
    out = _translate(sql)
    assert "INSERT OR IGNORE INTO" in out
    assert "?" in out and "%s" not in out


async def test_translate_rewrites_on_duplicate_key_update_single_column():
    sql = (
        "INSERT INTO slots_jackpot (id, jackpot) VALUES (1, %s)"
        " ON DUPLICATE KEY UPDATE jackpot=VALUES(jackpot)"
    )
    out = _translate(sql)
    assert "ON CONFLICT(id) DO UPDATE SET jackpot=excluded.jackpot" in out
    assert "ON DUPLICATE KEY UPDATE" not in out
    assert "VALUES(jackpot)" not in out


async def test_translate_rewrites_on_duplicate_key_update_multi_column():
    """save_lottery uses a multi-column ON DUPLICATE KEY UPDATE clause —
    the most complex pattern in persistence.py."""
    sql = (
        "INSERT INTO lottery (guild_id, prize_pool, last_posted_week, last_drawn_week)"
        " VALUES (%s, %s, %s, %s)"
        " ON DUPLICATE KEY UPDATE prize_pool=VALUES(prize_pool),"
        " last_posted_week=VALUES(last_posted_week),"
        " last_drawn_week=VALUES(last_drawn_week)"
    )
    out = _translate(sql)
    assert "ON CONFLICT(guild_id) DO UPDATE SET" in out
    assert "prize_pool=excluded.prize_pool" in out
    assert "last_posted_week=excluded.last_posted_week" in out
    assert "last_drawn_week=excluded.last_drawn_week" in out


async def test_translate_resolves_composite_primary_key():
    """records and balance_history have multi-column PKs."""
    sql = (
        "INSERT INTO records (guild_id, category, value) VALUES (%s, %s, %s)"
        " ON DUPLICATE KEY UPDATE value=VALUES(value)"
    )
    out = _translate(sql)
    # Composite PK: (guild_id, category) — both must appear in ON CONFLICT.
    assert "ON CONFLICT(guild_id, category)" in out


async def test_translate_raises_for_unknown_table_in_on_duplicate():
    """If persistence.py adds a table without updating _TABLE_PKS, the
    translator should fail loudly — not silently produce wrong SQL."""
    sql = (
        "INSERT INTO nonexistent_table (k, v) VALUES (%s, %s)"
        " ON DUPLICATE KEY UPDATE v=VALUES(v)"
    )
    with pytest.raises(ValueError, match=r"No PK mapping for table 'nonexistent_table'"):
        _translate(sql)


async def test_translate_passes_through_plain_select_unchanged():
    sql = "SELECT user_id, balance FROM economy_users WHERE user_id=?"
    assert _translate(sql) == sql


async def test_translate_handles_multiline_sql():
    """persistence.py uses multiline SQL strings; the regex must work
    across newlines."""
    sql = """INSERT INTO economy_users (user_id, balance, last_daily)
             VALUES (%s, %s, %s)
             ON DUPLICATE KEY UPDATE balance=VALUES(balance)"""
    out = _translate(sql)
    assert "ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance" in out


# ── Translator-dialect parity: round-trip against real sqlite3 ────────────────

async def test_upsert_round_trip_through_in_memory_sqlite():
    """Insert, then re-insert with a different value, then SELECT — the
    second insert should UPDATE not duplicate."""
    pool = await make_fake_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO slots_jackpot (id, jackpot) VALUES (%s, %s)"
                " ON DUPLICATE KEY UPDATE jackpot=VALUES(jackpot)",
                (1, 5000),
            )
            await cur.execute(
                "INSERT INTO slots_jackpot (id, jackpot) VALUES (%s, %s)"
                " ON DUPLICATE KEY UPDATE jackpot=VALUES(jackpot)",
                (1, 9999),
            )
            await cur.execute("SELECT jackpot FROM slots_jackpot WHERE id=?", (1,))
            row = await cur.fetchone()
    assert row == (9999,)
    await pool.close()


async def test_composite_pk_upsert_round_trip():
    """records uses (guild_id, category) — verify the composite ON CONFLICT
    works against real sqlite."""
    pool = await make_fake_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO records (guild_id, category, value, holder_id, holder_name)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE value=VALUES(value), holder_id=VALUES(holder_id),"
                " holder_name=VALUES(holder_name)",
                (42, "highest_balance", 1000, 1, "alice"),
            )
            # Same (guild_id, category) key — should overwrite.
            await cur.execute(
                "INSERT INTO records (guild_id, category, value, holder_id, holder_name)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE value=VALUES(value), holder_id=VALUES(holder_id),"
                " holder_name=VALUES(holder_name)",
                (42, "highest_balance", 5000, 2, "bob"),
            )
            # Different category — should insert as a new row.
            await cur.execute(
                "INSERT INTO records (guild_id, category, value, holder_id, holder_name)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE value=VALUES(value)",
                (42, "best_streak", 7, 3, "carol"),
            )
            await cur.execute(
                "SELECT category, value, holder_name FROM records WHERE guild_id=? ORDER BY category",
                (42,),
            )
            rows = await cur.fetchall()
    assert rows == [
        ("best_streak", 7, "carol"),
        ("highest_balance", 5000, "bob"),
    ]
    await pool.close()


async def test_insert_or_ignore_skips_duplicate_keys():
    """The command_perms migration uses INSERT IGNORE; subsequent inserts
    on the same key must be no-ops (not overwrites)."""
    pool = await make_fake_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT IGNORE INTO command_perms (command_name, tier, hidden) VALUES (%s, %s, %s)",
                ("godmode", "bot_admin", True),
            )
            await cur.execute(
                "INSERT IGNORE INTO command_perms (command_name, tier, hidden) VALUES (%s, %s, %s)",
                ("godmode", "everyone", False),  # would change tier if not IGNORE
            )
            await cur.execute("SELECT tier, hidden FROM command_perms WHERE command_name=?", ("godmode",))
            row = await cur.fetchone()
    assert row == ("bot_admin", 1)  # SQLite stores BOOL as int; original bot_admin tier preserved.
    await pool.close()


# ── Schema-pin: persistence.py sources stay in-sync with _TABLE_PKS ──────────

def _persistence_sql_text() -> str:
    """Concatenate every .py file under src/persistence/ so the on-duplicate
    scanner sees all SQL whether persistence is a flat module or a package."""
    pkg_dir = Path("src/persistence")
    if pkg_dir.is_dir():
        return "\n".join(p.read_text(encoding="utf-8") for p in sorted(pkg_dir.rglob("*.py")))
    return Path("src/persistence.py").read_text(encoding="utf-8")


def _tables_used_in_on_duplicate() -> set[str]:
    """Find every `INSERT INTO <table> ... ON DUPLICATE KEY UPDATE` table name
    in the persistence layer. Each must have a _TABLE_PKS entry or the test
    suite would fail when that save_X is exercised under the `db` fixture."""
    text = _persistence_sql_text()
    # Find INSERT INTO <table> within statements that also contain ON DUPLICATE.
    # Crude but sufficient — split on ON DUPLICATE, look for the most recent
    # INSERT INTO before each occurrence.
    tables: set[str] = set()
    for chunk in text.split("ON DUPLICATE"):
        # Look at the tail of the previous chunk — the most recent INSERT INTO.
        ms = re.findall(r"INSERT\s+(?:OR\s+\w+\s+|IGNORE\s+)?INTO\s+(\w+)", chunk, re.IGNORECASE)
        if ms:
            tables.add(ms[-1].lower())
    return tables


async def test_every_on_duplicate_table_has_a_pk_mapping():
    """If persistence.py introduces a new table that uses ON DUPLICATE KEY
    UPDATE, _TABLE_PKS must have a matching entry — otherwise tests using
    the `db` fixture would ValueError at runtime."""
    used = _tables_used_in_on_duplicate()
    missing = used - set(_TABLE_PKS)
    # The first chunk of the split-on-"ON DUPLICATE" doesn't refer to a real
    # ON DUPLICATE KEY UPDATE site (it's everything BEFORE the first one).
    # Ignore tables that appear only in plain INSERT statements.
    # We re-check by ensuring each "missing" table actually has an
    # `INSERT ... ON DUPLICATE` in the source.
    text = _persistence_sql_text()
    truly_missing = set()
    for table in missing:
        pat = re.compile(
            rf"INSERT\s+(?:OR\s+\w+\s+|IGNORE\s+)?INTO\s+{re.escape(table)}\b"
            rf".*?ON\s+DUPLICATE\s+KEY\s+UPDATE",
            re.IGNORECASE | re.DOTALL,
        )
        if pat.search(text):
            truly_missing.add(table)
    assert truly_missing == set(), (
        f"persistence.py uses ON DUPLICATE KEY UPDATE on tables with no _TABLE_PKS"
        f" entry in tests/fakes/db.py: {truly_missing}. Add the PK mapping."
    )
