"""Schema migration runner.

Apply pending `migrations/NNNN_*.sql` files in order, tracking applied
versions in `schema_migrations`. Runs once at boot (before init_db_state),
crashes loudly on failure rather than booting with a stale schema.

Adding a new migration: drop a `migrations/NNNN_short_description.sql` file.
The runner picks it up on next boot. Write idempotent SQL where possible
(`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`)
so a half-applied migration can be retried by simply rerunning.
"""
import hashlib
import logging
import re
from pathlib import Path

from src.db import with_cursor


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_FILE_RE = re.compile(r"^(\d{4})_[a-zA-Z0-9_]+\.sql$")
_BASELINE_SENTINEL_TABLE = "economy_users"

_done = False


def _discover(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    """Return [(version, name, path), ...] sorted by version. Validates filenames + ordering."""
    if not migrations_dir.is_dir():
        raise RuntimeError(f"migrations dir not found: {migrations_dir}")

    found: list[tuple[int, str, Path]] = []
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(".sql"):
            continue
        m = _FILE_RE.match(path.name)
        if not m:
            raise RuntimeError(
                f"migration file does not match NNNN_name.sql: {path.name}"
            )
        version = int(m.group(1))
        name = path.stem  # full "NNNN_name" — keep the prefix for clarity in logs/DB
        found.append((version, name, path))

    found.sort(key=lambda t: t[0])

    # No duplicates, no gaps. Versions must be 1, 2, 3, … contiguous.
    seen: set[int] = set()
    for i, (v, _, path) in enumerate(found, start=1):
        if v in seen:
            raise RuntimeError(f"duplicate migration version {v} ({path.name})")
        seen.add(v)
        if v != i:
            raise RuntimeError(
                f"gap in migration versions: expected {i}, found {v} ({path.name})"
            )

    return found


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into individual statements.

    Strips line-leading `--` comments and blank lines, then splits on `;` at
    statement boundaries. Sufficient for the SQL we write; does not handle
    `;` inside string literals or DELIMITER blocks. If a future migration
    needs those, swap in `sqlparse`.
    """
    cleaned: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("--"):
            continue
        cleaned.append(line)
    body = "\n".join(cleaned)
    return [s.strip() for s in body.split(";") if s.strip()]


async def _ensure_tracking_table(cur):
    await cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version    INT          NOT NULL PRIMARY KEY,"
        " name       VARCHAR(255) NOT NULL,"
        " applied_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,"
        " checksum   CHAR(64)     NOT NULL"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


async def _load_applied(cur) -> dict[int, tuple[str, str]]:
    """version -> (name, checksum)."""
    await cur.execute("SELECT version, name, checksum FROM schema_migrations")
    rows = await cur.fetchall()
    return {int(v): (n, c) for v, n, c in rows}


async def _table_exists(cur, table_name: str) -> bool:
    """Driver-agnostic table-exists check that works on both MariaDB and SQLite.

    Tries information_schema first (MariaDB); falls back to sqlite_master.
    """
    try:
        await cur.execute(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_schema = DATABASE() AND table_name = %s",
            (table_name,),
        )
        if await cur.fetchone():
            return True
        # information_schema query worked but table absent — definitive.
        return False
    except Exception:
        # SQLite (tests) doesn't have information_schema. Fall back.
        try:
            await cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s",
                (table_name,),
            )
            return (await cur.fetchone()) is not None
        except Exception:
            return False


async def _apply_pending(cur, files: list[tuple[int, str, Path]]) -> list[int]:
    """Apply pending migrations against an open cursor. Caller manages connection."""
    await _ensure_tracking_table(cur)
    applied = await _load_applied(cur)

    # Heuristic baseline detection: if schema_migrations is empty (just created
    # by us) but the sentinel table exists, this is an existing prod DB getting
    # the migration system for the first time. Mark version 1 as applied without
    # running it.
    if not applied and files:
        v1, name1, path1 = files[0]
        if v1 == 1 and await _table_exists(cur, _BASELINE_SENTINEL_TABLE):
            logging.info(
                "[migrations] detected pre-existing schema (%s present, "
                "schema_migrations empty); marking %s as applied without running",
                _BASELINE_SENTINEL_TABLE, name1,
            )
            await cur.execute(
                "INSERT INTO schema_migrations (version, name, checksum)"
                " VALUES (%s, %s, %s)",
                (v1, name1, _checksum(path1)),
            )
            applied = {v1: (name1, _checksum(path1))}

    applied_versions: list[int] = []
    for version, name, path in files:
        checksum = _checksum(path)
        if version in applied:
            prior_name, prior_checksum = applied[version]
            if prior_checksum != checksum:
                raise RuntimeError(
                    f"checksum mismatch on already-applied migration "
                    f"{version} ({prior_name}): file has been edited since "
                    f"it was applied. Add a new migration instead."
                )
            continue

        logging.info("[migrations] applying %s", name)
        sql = path.read_text(encoding="utf-8")
        try:
            for stmt in _split_statements(sql):
                await cur.execute(stmt)
            await cur.execute(
                "INSERT INTO schema_migrations (version, name, checksum)"
                " VALUES (%s, %s, %s)",
                (version, name, checksum),
            )
        except Exception:
            logging.exception("[migrations] failed to apply %s", name)
            raise
        applied_versions.append(version)
        logging.info("[migrations] applied %s", name)

    return applied_versions


async def run_migrations(*, migrations_dir: Path = _MIGRATIONS_DIR, cur=None) -> list[int]:
    """Apply any pending migrations in order. Idempotent — safe to call multiple times.

    Returns the list of versions applied during this call (empty if up-to-date).
    Raises if a checksum mismatches an already-applied migration, if there are
    gaps/duplicates in the file numbering, or if any migration statement fails.

    If `cur` is provided, runs against that cursor (used by tests / pool bootstrap
    where `with_cursor()` isn't yet wired). Otherwise opens its own cursor.
    """
    global _done
    if _done and cur is None:
        return []

    files = _discover(migrations_dir)

    if cur is not None:
        applied_versions = await _apply_pending(cur, files)
    else:
        async with with_cursor() as own_cur:
            applied_versions = await _apply_pending(own_cur, files)

    if applied_versions:
        logging.info("[migrations] applied %d migration(s) this boot", len(applied_versions))
    else:
        logging.info("[migrations] schema up-to-date")

    if cur is None:
        _done = True
    return applied_versions
