"""Round-trip tests for src/persistence/issues.py against the in-memory
SQLite DB. Exercises insert / lookup / status update / soft delete / listing
and the error_mutes table.

All tests use the `db` fixture; without it the real `with_cursor()` calls
would try to talk to a real MariaDB pool.
"""
import pytest

import src.persistence as _persistence

pytestmark = pytest.mark.asyncio


# ── issues table ─────────────────────────────────────────────────────────────

async def test_insert_and_get_issue_round_trip(db):
    issue_id = await _persistence.insert_issue(
        guild_id=42,
        channel_id=100,
        message_id=1000,
        reporter_id=7,
        report="something is broken",
    )
    assert issue_id > 0
    row = await _persistence.get_issue_by_message(1000)
    assert row is not None
    assert row["id"] == issue_id
    assert row["guild_id"] == 42
    assert row["channel_id"] == 100
    assert row["message_id"] == 1000
    assert row["reporter_id"] == 7
    assert row["report"] == "something is broken"
    assert row["status"] == "not_started"
    assert row["kind"] == "bug"
    assert row["mute_key"] is None
    assert row["deleted"] is False


async def test_get_issue_by_message_missing_returns_none(db):
    assert await _persistence.get_issue_by_message(99999) is None


async def test_insert_issue_with_kind_and_mute_key(db):
    issue_id = await _persistence.insert_issue(
        guild_id=None,
        channel_id=100,
        message_id=1001,
        reporter_id=7,
        report="boom",
        kind="error",
        mute_key="flip:ValueError:bad",
    )
    row = await _persistence.get_issue_by_message(1001)
    assert row["id"] == issue_id
    assert row["kind"] == "error"
    assert row["mute_key"] == "flip:ValueError:bad"
    assert row["guild_id"] is None


async def test_get_issue_by_id(db):
    issue_id = await _persistence.insert_issue(
        guild_id=42, channel_id=100, message_id=1002, reporter_id=7,
        report="lookup-by-id",
    )
    row = await _persistence.get_issue_by_id(issue_id)
    assert row is not None
    assert row["message_id"] == 1002

    assert await _persistence.get_issue_by_id(99999) is None


async def test_update_issue_status_persists(db):
    await _persistence.insert_issue(
        guild_id=42, channel_id=100, message_id=1003, reporter_id=7,
        report="status test",
    )
    await _persistence.update_issue_status(1003, "wip", resolved_by=99)
    row = await _persistence.get_issue_by_message(1003)
    assert row["status"] == "wip"
    assert row["resolved_by"] == 99
    assert row["resolved_at"] is not None

    # Subsequent transitions overwrite cleanly.
    await _persistence.update_issue_status(1003, "completed", resolved_by=100)
    row = await _persistence.get_issue_by_message(1003)
    assert row["status"] == "completed"
    assert row["resolved_by"] == 100


async def test_soft_delete_issue_is_idempotent(db):
    await _persistence.insert_issue(
        guild_id=42, channel_id=100, message_id=1004, reporter_id=7, report="x",
    )
    row = await _persistence.get_issue_by_message(1004)
    assert row["deleted"] is False
    await _persistence.soft_delete_issue(row["id"])
    row = await _persistence.get_issue_by_message(1004)
    assert row["deleted"] is True
    # Re-deleting the same id is a no-op (no exception, still flagged).
    await _persistence.soft_delete_issue(row["id"])
    row = await _persistence.get_issue_by_message(1004)
    assert row["deleted"] is True


# ── list_issues ──────────────────────────────────────────────────────────────

async def _seed_issues_for_listing(db):
    """Seed a known set of issues across statuses + a soft-deleted row.

    Returns the issue ids in insertion order (1..6) so individual tests can
    refer to them without re-querying.
    """
    ids = []
    for i in range(1, 7):
        iid = await _persistence.insert_issue(
            guild_id=42, channel_id=100, message_id=2000 + i,
            reporter_id=7, report=f"row {i}",
        )
        ids.append(iid)
    # Re-stamp statuses so we have one of each kind + one deleted.
    await _persistence.update_issue_status(2002, "wip", resolved_by=10)
    await _persistence.update_issue_status(2003, "completed", resolved_by=10)
    await _persistence.update_issue_status(2004, "rejected", resolved_by=10)
    await _persistence.update_issue_status(2005, "not_started", resolved_by=10)
    await _persistence.soft_delete_issue(ids[5])  # message_id 2006 → deleted
    return ids


async def test_list_issues_default_excludes_deleted_and_orders_desc(db):
    ids = await _seed_issues_for_listing(db)
    rows = await _persistence.list_issues()
    returned_ids = [r["id"] for r in rows]
    # Deleted row (ids[5]) excluded; remainder in id-desc order.
    assert ids[5] not in returned_ids
    assert returned_ids == sorted(returned_ids, reverse=True)
    assert len(rows) == 5


async def test_list_issues_status_filter(db):
    await _seed_issues_for_listing(db)
    # 2001 keeps the seeded default 'not_started'; 2005 was explicitly
    # re-stamped 'not_started'. So the not_started filter yields 2 rows.
    not_started_rows = await _persistence.list_issues(statuses=("not_started",))
    statuses = [r["status"] for r in not_started_rows]
    assert all(s == "not_started" for s in statuses)
    assert len(statuses) == 2

    multi = await _persistence.list_issues(statuses=("wip", "completed"))
    multi_statuses = sorted(r["status"] for r in multi)
    assert multi_statuses == ["completed", "wip"]


async def test_list_issues_include_deleted_returns_everything(db):
    ids = await _seed_issues_for_listing(db)
    rows = await _persistence.list_issues(include_deleted=True)
    returned_ids = [r["id"] for r in rows]
    assert ids[5] in returned_ids
    assert len(rows) == 6


async def test_list_issues_limit_caps_count(db):
    for i in range(10):
        await _persistence.insert_issue(
            guild_id=42, channel_id=100, message_id=3000 + i,
            reporter_id=7, report=f"row {i}",
        )
    rows = await _persistence.list_issues(limit=3)
    assert len(rows) == 3


# ── error_mutes table ───────────────────────────────────────────────────────

async def test_insert_and_load_error_mute(db):
    await _persistence.insert_error_mute("flip:ValueError:bad", muted_by=1)
    mutes = await _persistence.load_error_mutes()
    assert "flip:ValueError:bad" in mutes


async def test_insert_error_mute_is_idempotent_on_same_key(db):
    await _persistence.insert_error_mute("k:RuntimeError:1", muted_by=1)
    # Re-inserting the same key with a different muted_by should NOT raise
    # (the upsert keeps a single row keyed by mute_key).
    await _persistence.insert_error_mute("k:RuntimeError:1", muted_by=2)
    mutes = await _persistence.load_error_mutes()
    assert mutes == {"k:RuntimeError:1"}


async def test_delete_error_mute(db):
    await _persistence.insert_error_mute("k:RuntimeError:2", muted_by=1)
    await _persistence.delete_error_mute("k:RuntimeError:2")
    mutes = await _persistence.load_error_mutes()
    assert "k:RuntimeError:2" not in mutes


async def test_load_error_mutes_returns_empty_when_no_rows(db):
    mutes = await _persistence.load_error_mutes()
    assert mutes == set()
