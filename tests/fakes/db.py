"""Fake aiomysql Pool/Conn/Cursor backed by sqlite3 :memory: for tests.

Lets persistence tests exercise real `save_*`/`load_*` SQL paths without a real
MariaDB. The translator handles the small number of MariaDB-isms the codebase
actually uses (`%s` placeholders, `INSERT IGNORE`,
`INSERT ... ON DUPLICATE KEY UPDATE col=VALUES(col)`, and a handful of MariaDB
type/syntax bits that appear in migration files).

If a query string here ever fails to translate, prefer extending the translator
over rewriting production SQL — production has to keep speaking MariaDB.

Schema in tests is built by running `src.migrations.run_migrations()` against
the SQLite DB through this same translator — so test-prod parity is the
migration files themselves, not a parallel hand-translated schema.
"""
import re
import sqlite3


# Per-table primary key column(s). Keyed by table name (lowercase).
# Used to translate `ON DUPLICATE KEY UPDATE` -> `ON CONFLICT(pk) DO UPDATE`.
# Must stay in sync with the migration files in migrations/.
_TABLE_PKS = {
    "economy_users": ("user_id",),
    "economy_meta": ("key_name",),
    "guild_house_balance": ("guild_id",),
    "guild_settings": ("guild_id",),
    "bot_roles": ("guild_id", "role_id"),
    "godmode_users": ("user_id",),
    "bot_settings": ("key_name",),
    "shop_insurance": ("user_id",),
    "shop_effects": ("guild_id", "user_id", "effect_type"),
    "user_artifacts": ("user_id", "artifact_id"),
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
    "chess_reports": ("report_id",),
    "ai_threads": ("thread_id",),
    "leveling": ("guild_id", "user_id"),
    "gambler_streak": ("user_id",),
    "command_streak": ("user_id",),
    "recap_usage": ("guild_id", "user_id"),
    "voice_pings": ("channel_id", "user_id"),
    "voice_ping_ignores": ("guild_id", "user_id", "ignored_user_id"),
    "quote_log": ("id",),
    "saved_quotes": ("id",),
    "balance_history": ("snapshot_date", "bucket", "user_id"),
    "bot_stats_history": ("snapshot_date", "bucket"),
    "bot_command_usage_history": ("snapshot_date", "bucket", "cog_name"),
    "crime_history": ("snapshot_date", "bucket", "guild_id", "user_id"),
    "gambling_history": ("snapshot_date", "bucket", "guild_id", "user_id"),
    "levelup_history": ("snapshot_date", "bucket", "guild_id", "user_id"),
    "restart_msg": ("id",),
    "ephemeral_msgs": ("id",),
    "command_perms": ("command_name",),
    "user_perm_overrides": ("guild_id", "user_id"),
    "blocklist": ("guild_id", "user_id"),
    "global_blocklist": ("user_id",),
    "schema_migrations": ("version",),
    "error_mutes": ("mute_key",),
    "bounties": ("id",),
    "bounty_claims": ("id",),
    "mc_ping_samples": ("ts",),
    "daily_counters": ("day", "counter"),
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

    # Strip MariaDB-only table options at the end of CREATE TABLE statements.
    # SQLite rejects ENGINE=InnoDB / DEFAULT CHARSET=utf8mb4.
    out = re.sub(
        r"\)\s*ENGINE\s*=\s*\w+(?:\s+DEFAULT\s+CHARSET\s*=\s*\w+)?",
        ")",
        out,
        flags=re.IGNORECASE,
    )

    # ── MariaDB-isms that show up in migration files ─────────────────────
    # JSON_ARRAY() default -> '[]' (SQLite has no JSON_ARRAY function).
    out = re.sub(r"JSON_ARRAY\s*\(\s*\)", "'[]'", out, flags=re.IGNORECASE)
    # ENUM('a','b','c') -> TEXT (SQLite has no ENUM type).
    out = re.sub(r"\bENUM\s*\([^)]*\)", "TEXT", out, flags=re.IGNORECASE)
    # `<inttype> [UNSIGNED] NOT NULL AUTO_INCREMENT PRIMARY KEY`
    #     -> `INTEGER PRIMARY KEY AUTOINCREMENT`
    # SQLite only accepts AUTOINCREMENT after `INTEGER PRIMARY KEY`, so we
    # normalize the whole MariaDB idiom.
    out = re.sub(
        r"\b(?:BIGINT|INT|TINYINT|SMALLINT)(?:\s+UNSIGNED)?\s+NOT\s+NULL\s+AUTO_INCREMENT\s+PRIMARY\s+KEY\b",
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        out,
        flags=re.IGNORECASE,
    )
    # Catch any remaining bare AUTO_INCREMENT.
    out = re.sub(r"\bAUTO_INCREMENT\b", "AUTOINCREMENT", out, flags=re.IGNORECASE)
    # `ALTER TABLE foo ADD COLUMN IF NOT EXISTS bar` — SQLite supports
    # ADD COLUMN but not the IF NOT EXISTS clause for it. Strip just that
    # token; do NOT touch CREATE TABLE IF NOT EXISTS (valid in SQLite).
    out = re.sub(
        r"\b(ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN)\s+IF\s+NOT\s+EXISTS\b",
        r"\1",
        out,
        flags=re.IGNORECASE,
    )
    # `ALTER TABLE foo DROP COLUMN IF EXISTS bar` — SQLite 3.35+ supports
    # DROP COLUMN but not the IF EXISTS clause. Strip just that token.
    out = re.sub(
        r"\b(ALTER\s+TABLE\s+\w+\s+DROP\s+COLUMN)\s+IF\s+EXISTS\b",
        r"\1",
        out,
        flags=re.IGNORECASE,
    )
    # Inline `INDEX idx_x (col)` inside CREATE TABLE — SQLite doesn't support this
    # form. Strip the line; tests don't rely on these indexes for correctness.
    out = re.sub(r",\s*INDEX\s+\w+\s*\([^)]*\)", "", out, flags=re.IGNORECASE)
    # `ALTER TABLE foo MODIFY COLUMN ...` — MariaDB-only (used to widen an
    # ENUM, change a type, etc.). SQLite has no MODIFY COLUMN and, since ENUMs
    # are already TEXT here, the change is a no-op for the test DB. Drop it.
    if re.match(r"\s*ALTER\s+TABLE\s+\w+\s+MODIFY\s+COLUMN\b", out, flags=re.IGNORECASE):
        return ""
    # SQLite's ALTER TABLE doesn't support DROP PRIMARY KEY. Drop the
    # statement entirely; the matching ADD PRIMARY KEY below handles the
    # full PK rewrite by recreating the table.
    if re.match(r"\s*ALTER\s+TABLE\s+\w+\s+DROP\s+PRIMARY\s+KEY\b", out, flags=re.IGNORECASE):
        return ""
    # `ALTER TABLE foo ADD PRIMARY KEY (a, b, c)`. SQLite can't change a PK
    # in place, so we mark the statement for table-rebuild handling in
    # FakeCursor.execute (which has access to the connection). Returning a
    # sentinel string keeps the translation pure; the cursor recognizes it.
    m_addpk = re.match(
        r"\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s+PRIMARY\s+KEY\s*\(([^)]+)\)\s*$",
        out, flags=re.IGNORECASE,
    )
    if m_addpk:
        table = m_addpk.group(1)
        cols = m_addpk.group(2).strip()
        return f"--REBUILD-PK {table} ({cols})"
    # information_schema.tables → sqlite_master shim used by run_migrations'
    # _table_exists. The MariaDB form has a `DATABASE()` builtin SQLite lacks;
    # rather than rewrite the whole query, the runner already has a fallback,
    # so we let the original query fail naturally and rely on that fallback.

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
        if not translated:  # translator dropped this statement (e.g. SQLite-incompatible ALTER)
            return
        # Rebuild-PK sentinel: SQLite can't alter a PK in place, so the
        # translator emits this marker and we do a copy-and-rename here.
        m_pk = re.match(r"^--REBUILD-PK (\w+) \(([^)]+)\)$", translated)
        if m_pk:
            self._rebuild_pk(m_pk.group(1), m_pk.group(2))
            return
        if params is None:
            params = ()
        self._cur.execute(translated, tuple(params))

    async def executemany(self, sql, seq_of_params):
        translated = _translate(sql)
        if not translated:
            return
        self._cur.executemany(translated, [tuple(p) for p in seq_of_params])

    def _rebuild_pk(self, table: str, pk_cols: str):
        """SQLite-only: recreate `table` with PRIMARY KEY (`pk_cols`).
        Preserves all rows and columns."""
        self._cur.execute(f"PRAGMA table_info({table})")
        info = self._cur.fetchall()  # (cid, name, type, notnull, dflt_value, pk)
        col_defs = []
        col_names = []
        for _cid, name, ctype, notnull, dflt, _pk in info:
            parts = [name]
            if ctype:
                parts.append(ctype)
            if notnull:
                parts.append("NOT NULL")
            if dflt is not None:
                parts.append(f"DEFAULT {dflt}")
            col_defs.append(" ".join(parts))
            col_names.append(name)
        col_defs_sql = ",\n  ".join(col_defs)
        col_names_sql = ", ".join(col_names)
        tmp = f"{table}__rebuild"
        self._cur.execute(f"CREATE TABLE {tmp} (\n  {col_defs_sql},\n  PRIMARY KEY ({pk_cols})\n)")
        self._cur.execute(f"INSERT INTO {tmp} ({col_names_sql}) SELECT {col_names_sql} FROM {table}")
        self._cur.execute(f"DROP TABLE {table}")
        self._cur.execute(f"ALTER TABLE {tmp} RENAME TO {table}")

    async def fetchall(self):
        return self._cur.fetchall()

    async def fetchone(self):
        return self._cur.fetchone()

    async def fetchmany(self, size=1):
        return self._cur.fetchmany(size)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid


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

    async def begin(self):
        # isolation_level=None means autocommit; an explicit BEGIN opens a
        # real transaction so with_transaction()'s commit/rollback work.
        self._sqlite.execute("BEGIN")

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


async def make_fake_pool() -> FakePool:
    """Build a fresh in-memory SQLite, apply migrations through the same runner
    production uses, and return a FakePool.

    Test-prod parity comes from the migration files themselves — there is no
    parallel hand-translated test schema to drift out of sync.
    """
    # Avoid an import-time cycle (src.migrations -> src.db is fine, but keeping
    # this import local matches the rest of this module's lazy-import style).
    from src import migrations as _migrations

    # isolation_level=None -> autocommit, mirroring aiomysql's autocommit=True.
    conn = sqlite3.connect(":memory:", isolation_level=None)
    pool = FakePool(conn)

    # Apply migrations directly against a cursor we hand the runner. We can't
    # rely on `with_cursor()` here because the global pool/get_pool patch
    # hasn't been wired yet (this function builds the pool that patch points at).
    fake_cur = FakeCursor(conn.cursor())
    # Reset the runner's per-process guard so each fresh pool gets a fresh run.
    _migrations._done = False
    await _migrations.run_migrations(cur=fake_cur)
    return pool
