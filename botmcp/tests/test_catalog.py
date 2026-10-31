"""The catalog must cover the live schema exactly, and the committed grants
must be what the catalog generates — the two checks that stop a new
migration or a catalog edit from exposing something nobody classified."""
from pathlib import Path

import pytest

from botmcp import catalog, grants

REPO_ROOT = Path(__file__).resolve().parents[2]


def _live_schema(pool) -> dict:
    cur = pool._sqlite.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: {r[1] for r in cur.execute(f"PRAGMA table_info({t})")} for t in tables}


async def test_every_live_table_is_classified(pool):
    live = _live_schema(pool)
    missing = sorted(set(live) - set(catalog.TABLES))
    assert not missing, (
        f"tables with no entry in botmcp/catalog.py: {missing}. Classify each as readable "
        "(every column listed, sensitive ones under hidden=) or never=."
    )
    stale = sorted(set(catalog.TABLES) - set(live))
    assert not stale, f"catalog entries for tables that no longer exist: {stale}"


async def test_every_column_of_a_readable_table_is_listed_or_hidden(pool):
    live = _live_schema(pool)
    for name, table in catalog.readable().items():
        listed = set(table.columns) | set(table.hidden)
        unclassified = sorted(live[name] - listed)
        assert not unclassified, f"{name}: columns neither readable nor hidden: {unclassified}"
        unknown = sorted(listed - live[name])
        assert not unknown, f"{name}: catalog names columns the schema lacks: {unknown}"
        assert not (set(table.columns) & set(table.hidden)), f"{name}: a column is both readable and hidden"


def test_never_tables_have_no_columns():
    for name, table in catalog.TABLES.items():
        if not table.readable:
            assert not table.columns and not table.hidden, f"{name}: never= entries carry no columns"
            assert table.never


def test_committed_grants_match_catalog():
    committed = (REPO_ROOT / "scripts" / "mcp_db_user.sql").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert committed == grants.render(), "scripts/mcp_db_user.sql is stale: python -m botmcp.grants --template > scripts/mcp_db_user.sql"


def test_grants_from_env_substitute_and_escape():
    sql = grants.render_from_env({"MCP_DB_USER": "reader", "MCP_DB_PASSWORD": r"it's\x", "DB_NAME": "bot_db", "MCP_DB_ACCOUNT_HOST": "172.18.0.%"})
    assert r"CREATE USER IF NOT EXISTS 'reader'@'172.18.0.%' IDENTIFIED BY 'it''s\\x';" in sql
    assert "ON `bot_db`.`economy_users` TO 'reader'@'172.18.0.%';" in sql
    assert grants.TEMPLATE_PASSWORD not in sql
    with pytest.raises(SystemExit):
        grants.render_from_env({"MCP_DB_USER": "reader"})


def test_grants_never_name_a_hidden_column_or_never_table():
    sql = grants.render()
    grant_lines = [line for line in sql.splitlines() if line.startswith("GRANT ")]
    for name, table in catalog.TABLES.items():
        mine = [line for line in grant_lines if f"`{name}`" in line]
        if not table.readable:
            assert not mine, f"{name} is never-readable but has a GRANT"
            continue
        assert len(mine) == 1
        granted = {c.strip() for c in mine[0].split("(", 1)[1].split(")", 1)[0].split(",")}
        assert granted == set(table.columns)
        assert not (granted & set(table.hidden))
    assert "GRANT ALL" not in sql and "GRANT SELECT ON" not in sql
