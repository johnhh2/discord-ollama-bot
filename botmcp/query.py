"""The only way the sidecar reaches the database: one-table SELECTs built
from the catalog.

Nothing here takes SQL from a caller. A tool names a table, the columns (or
expressions over them) it wants, a WHERE with `%s` placeholders, and the
values go in `params`. Every identifier is checked against the catalog, so
a hidden column or a never-table can't be named even by a bug in a tool;
quotes, semicolons and comments are refused outright, so a literal can't be
smuggled into an expression. Joins are deliberately absent — compose in
Python with two queries. The DB account's column-level grants are the
backstop if this layer is ever bypassed.
"""
from __future__ import annotations

import datetime
import re
from decimal import Decimal
from typing import Iterable, Sequence

from botmcp import catalog, db

MAX_ROWS = 500

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ALIAS_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_FORBIDDEN_RE = re.compile(r"""['"`;\\]|--|/\*|\*/""")
# SQL words an expression may use besides the table's own columns. Kept
# small on purpose: a tool that needs more is probably doing a join or a
# subquery, which belongs in Python.
SQL_WORDS = frozenset({
    "and", "or", "not", "is", "null", "in", "as", "asc", "desc", "between", "like",
    "sum", "count", "max", "min", "avg", "coalesce", "ifnull", "if", "abs", "round", "floor", "ceil", "length",
    "case", "when", "then", "else", "end", "distinct", "true", "false",
    "unix_timestamp", "from_unixtime", "now", "cast", "unsigned", "signed", "integer",
})


class QueryError(ValueError):
    """A tool asked for something the catalog doesn't allow. A bug, never user input."""


def _check_expr(expr: str, allowed: Iterable[str], what: str) -> None:
    if _FORBIDDEN_RE.search(expr):
        raise QueryError(f"{what}: quotes, comments and semicolons are not allowed: {expr!r}")
    allowed_set = set(allowed) | SQL_WORDS
    for ident in _IDENT_RE.findall(expr.replace("%s", " ")):
        if ident.lower() not in allowed_set:
            raise QueryError(f"{what}: {ident!r} is not a readable column of this table")


def _column_sql(col: "str | tuple[str, str]", allowed: Sequence[str]) -> tuple[str, str]:
    """-> (sql fragment, result key)."""
    if isinstance(col, tuple):
        expr, alias = col
        if not _ALIAS_RE.match(alias):
            raise QueryError(f"bad alias {alias!r}")
        _check_expr(expr, allowed, "column expression")
        return f"{expr} AS {alias}", alias
    if col not in allowed:
        raise QueryError(f"{col!r} is not a readable column")
    return col, col


def build(
    table: str,
    columns: Sequence["str | tuple[str, str]"],
    *,
    where: str | None = None,
    group_by: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
) -> tuple[str, list[str]]:
    """-> (sql, result keys). Pure; `select` runs it."""
    entry = catalog.TABLES.get(table)
    if entry is None or not entry.readable:
        raise QueryError(f"table {table!r} is not readable")
    if not columns:
        raise QueryError("no columns")
    allowed = entry.columns
    parts, keys = [], []
    for col in columns:
        sql, key = _column_sql(col, allowed)
        parts.append(sql)
        keys.append(key)
    # nosec B608 — every identifier above was checked against the catalog; values travel as params.
    out = f"SELECT {', '.join(parts)} FROM {table}"  # nosec B608
    if where:
        _check_expr(where, allowed, "where")
        out += f" WHERE {where}"
    if group_by:
        _check_expr(group_by, allowed, "group by")
        out += f" GROUP BY {group_by}"
    if order_by:
        # ORDER BY may name a result alias as well as a column.
        _check_expr(order_by, list(allowed) + keys, "order by")
        out += f" ORDER BY {order_by}"
    out += f" LIMIT {clamp_limit(limit)}"
    return out, keys


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return MAX_ROWS
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise QueryError(f"limit must be a positive integer, got {limit!r}")
    return min(limit, MAX_ROWS)


async def select(
    table: str,
    columns: Sequence["str | tuple[str, str]"],
    *,
    where: str | None = None,
    params: Sequence = (),
    group_by: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    sql, keys = build(table, columns, where=where, group_by=group_by, order_by=order_by, limit=limit)
    rows = await db.fetch_all(sql, tuple(params))
    return [dict(zip(keys, (_plain(v) for v in row))) for row in rows]


def _plain(value):
    # MariaDB returns SUM()/AVG() as Decimal, which the SDK serialises as a
    # string ("9") — an agent would then do string maths on it.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


async def select_one(table: str, columns: Sequence, *, where: str, params: Sequence = ()) -> "dict | None":
    rows = await select(table, columns, where=where, params=params, limit=1)
    return rows[0] if rows else None
