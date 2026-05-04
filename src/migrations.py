"""Schema migration runner.

Apply pending `migrations/NNNN_*.sql` files in order, tracking applied
versions in `schema_migrations`. Runs once at boot (before init_db_state),
crashes loudly on failure rather than booting with a stale schema.

Adding a new migration: drop a `migrations/NNNN_short_description.sql` file.
The runner picks it up on next boot. Write idempotent SQL where possible
(`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE … ADD COLUMN IF NOT EXISTS`)
so a half-applied migration can be retried by simply rerunning.

Reverse (.down.sql) is optional, never automatic. If you ship a
`migrations/NNNN_name.down.sql` alongside the forward file, an operator
can run `python -m src.migrations down N` to undo migration N. Without
a .down.sql, reverts must be done manually (or by restoring a backup).
"""
import argparse
import asyncio
import hashlib
import logging
import re
import sys
from pathlib import Path

from src.db import with_cursor


_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_FILE_RE = re.compile(r"^(\d{4})_[a-zA-Z0-9_]+\.sql$")
_DOWN_RE = re.compile(r"^(\d{4})_[a-zA-Z0-9_]+\.down\.sql$")
_BASELINE_SENTINEL_TABLE = "economy_users"

_done = False


def _discover(migrations_dir: Path) -> list[tuple[int, str, Path]]:
    """Return [(version, name, path), ...] of forward migrations sorted by version.

    Validates filenames + ordering. Skips .down.sql files (they're paired
    reverses, looked up on demand by `_find_down`).
    """
    if not migrations_dir.is_dir():
        raise RuntimeError(f"migrations dir not found: {migrations_dir}")

    found: list[tuple[int, str, Path]] = []
    for path in sorted(migrations_dir.iterdir()):
        if not path.is_file() or not path.name.endswith(".sql"):
            continue
        # Paired reverse migrations are picked up by version, not enumerated here.
        if path.name.endswith(".down.sql"):
            if not _DOWN_RE.match(path.name):
                raise RuntimeError(
                    f"reverse migration file does not match NNNN_name.down.sql: {path.name}"
                )
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


def _find_down(migrations_dir: Path, version: int) -> Path | None:
    """Return the .down.sql for `version` if one exists, else None."""
    for path in migrations_dir.iterdir():
        if not path.is_file():
            continue
        m = _DOWN_RE.match(path.name)
        if m and int(m.group(1)) == version:
            return path
    return None


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


async def _revert_one(cur, version: int, migrations_dir: Path) -> str:
    """Run version N's .down.sql and delete its row from schema_migrations.
    Returns the name of the reverted migration. Caller manages connection.
    """
    await _ensure_tracking_table(cur)
    applied = await _load_applied(cur)
    if version not in applied:
        raise RuntimeError(
            f"migration {version} is not recorded as applied; nothing to revert"
        )
    name, _checksum_unused = applied[version]

    down_path = _find_down(migrations_dir, version)
    if down_path is None:
        raise RuntimeError(
            f"no .down.sql for migration {version} ({name}); "
            f"reverse must be performed manually or by restoring a backup"
        )

    logging.info("[migrations] reverting %s using %s", name, down_path.name)
    sql = down_path.read_text(encoding="utf-8")
    try:
        for stmt in _split_statements(sql):
            await cur.execute(stmt)
        await cur.execute(
            "DELETE FROM schema_migrations WHERE version = %s",
            (version,),
        )
    except Exception:
        logging.exception("[migrations] failed to revert %s", name)
        raise
    logging.info("[migrations] reverted %s", name)
    return name


async def revert_migration(
    version: int,
    *,
    migrations_dir: Path = _MIGRATIONS_DIR,
    cur=None,
) -> str:
    """Run the .down.sql for `version` and remove its row from schema_migrations.

    Returns the name of the reverted migration. Raises if the version isn't
    recorded as applied, or if no .down.sql exists for it. Never invoked
    automatically — operator-only emergency tool, exposed via the
    `python -m src.migrations down N` CLI.
    """
    if cur is not None:
        return await _revert_one(cur, version, migrations_dir)
    async with with_cursor() as own_cur:
        return await _revert_one(own_cur, version, migrations_dir)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli_main(argv: list[str] | None = None) -> int:
    """`python -m src.migrations down N [--yes]` — revert one applied migration."""
    parser = argparse.ArgumentParser(prog="python -m src.migrations")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_down = sub.add_parser("down", help="revert a single applied migration")
    p_down.add_argument("version", type=int, help="migration version to revert (e.g. 7)")
    p_down.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "down":
        if not args.yes:
            answer = input(
                f"About to revert migration {args.version} against the live DB. "
                f"Type 'yes' to continue: "
            ).strip().lower()
            if answer != "yes":
                print("aborted", file=sys.stderr)
                return 1
        try:
            name = asyncio.run(revert_migration(args.version))
        except Exception as e:
            print(f"revert failed: {e}", file=sys.stderr)
            return 2
        print(f"reverted {name}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())
