import pytest

from botmcp import query
from botmcp.query import QueryError


def test_builds_plain_select():
    sql, keys = query.build("economy_users", ("user_id", "balance"), where="user_id = %s", order_by="balance DESC", limit=5)
    assert sql == "SELECT user_id, balance FROM economy_users WHERE user_id = %s ORDER BY balance DESC LIMIT 5"
    assert keys == ["user_id", "balance"]


def test_expression_columns_get_aliases():
    sql, keys = query.build("user_artifacts", ("user_id", ("SUM(quantity)", "total")), group_by="user_id", order_by="total DESC")
    assert "SUM(quantity) AS total" in sql and "GROUP BY user_id" in sql
    assert keys == ["user_id", "total"]


@pytest.mark.parametrize("table", ["ai_threads", "rigged_slots", "godmode_users", "no_such_table"])
def test_never_and_unknown_tables_are_refused(table):
    with pytest.raises(QueryError):
        query.build(table, ("user_id",))


def test_hidden_column_is_refused_everywhere():
    with pytest.raises(QueryError):
        query.build("economy_users", ("jail_reason",))
    with pytest.raises(QueryError):
        query.build("economy_users", ("user_id",), where="jail_reason IS NOT NULL")
    with pytest.raises(QueryError):
        query.build("economy_users", (("LENGTH(jail_reason)", "n"),))
    with pytest.raises(QueryError):
        query.build("records", ("category",), order_by="holder_name")


@pytest.mark.parametrize("where", [
    "user_id = 1; DROP TABLE economy_users",
    "balance > 0 -- x",
    "daily_date = 'x'",
    'daily_date = "x"',
    "balance > 0 /* c */",
    "user_id IN (SELECT user_id FROM godmode_users)",
])
def test_literals_comments_and_subqueries_are_refused(where):
    with pytest.raises(QueryError):
        query.build("economy_users", ("user_id",), where=where)


def test_limit_is_clamped_and_validated():
    assert query.clamp_limit(None) == query.MAX_ROWS
    assert query.clamp_limit(10 ** 9) == query.MAX_ROWS
    assert query.clamp_limit(3) == 3
    for bad in (0, -1, True, "5", 2.5):
        with pytest.raises(QueryError):
            query.clamp_limit(bad)
    sql, _ = query.build("economy_users", ("user_id",))
    assert sql.endswith(f"LIMIT {query.MAX_ROWS}")


def test_bad_alias_is_refused():
    with pytest.raises(QueryError):
        query.build("economy_users", (("COUNT(user_id)", "n; drop"),))


async def test_select_runs_against_the_schema(pool):
    from botmcp.tests.conftest import seed
    await seed(pool, "INSERT INTO economy_users (user_id, balance) VALUES (%s, %s)", (7, 100))
    rows = await query.select("economy_users", ("user_id", "balance"), where="user_id = %s", params=(7,))
    assert rows == [{"user_id": 7, "balance": 100}]
    assert await query.select_one("economy_users", ("balance",), where="user_id = %s", params=(8,)) is None


def test_aggregates_come_back_as_numbers_not_decimal_strings():
    # SQLite hands the suite ints, so the Decimal path is pinned directly.
    import datetime
    from decimal import Decimal
    assert query._plain(Decimal("9")) == 9 and isinstance(query._plain(Decimal("9")), int)
    assert query._plain(Decimal("2.5")) == 2.5
    assert query._plain(datetime.date(2026, 11, 1)) == "2026-11-01"
    assert query._plain("text") == "text" and query._plain(None) is None
