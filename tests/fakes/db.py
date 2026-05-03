"""Fake aiomysql Pool/Conn/Cursor backed by sqlite3 :memory: for tests.

Lets persistence tests exercise real `save_*`/`load_*` SQL paths without a real
MariaDB. The translator handles the small number of MariaDB-isms the codebase
actually uses (`%s` placeholders, `INSERT IGNORE`, and
`INSERT ... ON DUPLICATE KEY UPDATE col=VALUES(col)`).

If a query string here ever fails to translate, prefer extending the translator
over rewriting production SQL — production has to keep speaking MariaDB.
"""
import re
import sqlite3
from pathlib import Path


_SCHEMA_PATH = Path(__file__).parent / "schema_sqlite.sql"


# Per-table primary key column(s). Keyed by table name (lowercase).
# Used to translate `ON DUPLICATE KEY UPDATE` -> `ON CONFLICT(pk) DO UPDATE`.
# Must stay in sync with tests/fakes/schema_sqlite.sql.
_TABLE_PKS = {
    "economy_users": ("user_id",),
    "economy_meta": ("key_name",),
    "guild_house_balance": ("guild_id",),
    "guild_settings": ("guild_id",),
    "bot_roles": ("role_id",),
    "godmode_users": ("user_id",),
    "bot_settings": ("key_name",),
    "shop_insurance": ("user_id",),
    "shop_effects": ("user_id", "effect_type"),
    "rigged_slots": ("user_id",),
    "rigged_flips": ("user_id",),
    "rigged_scratch": ("user_id",),
    "rigged_steal": ("user_id",),
    "slots_jackpot": ("id",),
    "lottery": ("guild_id",),
    "lottery_players": ("guild_id", "user_id"),
    "records": ("guild_id", "category"),
    "channel_prompts": ("channel_id",),
    "chess_games": ("channel_id",),
    "ai_threads": ("thread_id",),
    "leveling": ("guild_id", "user_id"),
    "gambler_streak": ("user_id",),
    "quote_log": ("id",),
    "saved_quotes": ("id",),
    "balance_history": ("snapshot_date", "user_id"),
    "bot_stats_history": ("snapshot_date",),
    "restart_msg": ("id",),
    "ephemeral_msgs": ("id",),
    "command_perms": ("command_name",),
}


_INSERT_TABLE_RE = re.compile(
    r"\binsert\s+(?:or\s+\w+\s+)?(?:ignore\s+)?into\s+`?(\w+)`?",
    re.IGNORECASE,
)
_ON_DUP_RE = re.compile(
    r"\s+ON\s+DUPLICATE\s+KEY\s+UPDATE\s+(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_VALUES_FN_RE = re.compile(r"VALUES\s*\(\s*(\w+)\s*\)", re.IGNORECASE)


def _translate(sql: str) -> str:
    """Rewrite MariaDB-isms to SQLite-compatible SQL."""
    # %s placeholders -> ?
    out = sql.replace("%s", "?")

    # INSERT IGNORE -> INSERT OR IGNORE
    out = re.sub(r"\bINSERT\s+IGNORE\b", "INSERT OR IGNORE", out, flags=re.IGNORECASE)

    # INSERT ... ON DUPLICATE KEY UPDATE col=VALUES(col), ...
    #   -> INSERT ... ON CONFLICT(<pk_cols>) DO UPDATE SET col=excluded.col, ...
    m = _ON_DUP_RE.search(out)
    if m:
        update_clause = m.group(1)
        # Find the table being inserted into to resolve PK.
        table_match = _INSERT_TABLE_RE.search(out)
        if not table_match:
            raise ValueError(f"Could not find INSERT INTO table in: {sql!r}")
        table = table_match.group(1).lower()
        pks = _TABLE_PKS.get(table)
        if pks is None:
            raise ValueError(
                f"No PK mapping for table {table!r} in tests/fakes/db.py:_TABLE_PKS. "
                f"Add one if a new table was introduced."
            )
        # Replace VALUES(col) with excluded.col
        update_clause = _VALUES_FN_RE.sub(lambda mm: f"excluded.{mm.group(1)}", update_clause)
        conflict_cols = ", ".join(pks)
        new_tail = f" ON CONFLICT({conflict_cols}) DO UPDATE SET {update_clause}"
        out = out[: m.start()] + new_tail

    return out


class FakeCursor:
    def __init__(self, sqlite_cur: sqlite3.Cursor):
        self._cur = sqlite_cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._cur.close()
        return False

    async def execute(self, sql, params=()):
        translated = _translate(sql)
        if params is None:
            params = ()
        self._cur.execute(translated, tuple(params))

    async def executemany(self, sql, seq_of_params):
        translated = _translate(sql)
        self._cur.executemany(translated, [tuple(p) for p in seq_of_params])

    async def fetchall(self):
        return self._cur.fetchall()

    async def fetchone(self):
        return self._cur.fetchone()

    async def fetchmany(self, size=1):
        return self._cur.fetchmany(size)

    @property
    def rowcount(self):
        return self._cur.rowcount


class FakeConn:
    def __init__(self, sqlite_conn: sqlite3.Connection):
        self._sqlite = sqlite_conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # Pool will keep the underlying connection open; nothing to do here.
        return False

    def cursor(self):
        # aiomysql's `conn.cursor()` is itself an async context manager.
        return FakeCursor(self._sqlite.cursor())

    async def commit(self):
        self._sqlite.commit()

    async def rollback(self):
        self._sqlite.rollback()


class _AcquireCM:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    """Holds a single sqlite3 :memory: connection (autocommit) and hands it out
    via async context managers that mimic aiomysql.Pool.acquire()."""

    def __init__(self, sqlite_conn: sqlite3.Connection):
        self._sqlite = sqlite_conn
        self._conn = FakeConn(sqlite_conn)

    def acquire(self):
        return _AcquireCM(self._conn)

    async def close(self):
        self._sqlite.close()

    async def wait_closed(self):
        return None


def _load_schema_sql() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


async def make_fake_pool() -> FakePool:
    """Build a fresh in-memory SQLite, run the test schema, return a FakePool."""
    # isolation_level=None -> autocommit, mirroring aiomysql's autocommit=True.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_load_schema_sql())
    return FakePool(conn)
