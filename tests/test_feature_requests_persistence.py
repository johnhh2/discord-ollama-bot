"""Round-trip tests for src/persistence/feature_requests.py.

Covers insert / lookup by message_id / lookup by feature_issue_id /
status update / linking to a spawned feature issue.
"""
import pytest

import src.persistence as _persistence

pytestmark = pytest.mark.asyncio


async def test_insert_and_get_feature_request_round_trip(db):
    rid = await _persistence.insert_feature_request(
        guild_id=42, channel_id=200, message_id=5000,
        reporter_id=7, description="awesome idea",
    )
    assert rid > 0
    row = await _persistence.get_feature_request_by_message(5000)
    assert row is not None
    assert row["id"] == rid
    assert row["guild_id"] == 42
    assert row["channel_id"] == 200
    assert row["message_id"] == 5000
    assert row["reporter_id"] == 7
    assert row["description"] == "awesome idea"
    assert row["status"] == "open"
    assert row["feature_issue_id"] is None


async def test_get_feature_request_by_message_missing_returns_none(db):
    assert await _persistence.get_feature_request_by_message(99999) is None


async def test_update_feature_request_status(db):
    await _persistence.insert_feature_request(
        guild_id=42, channel_id=200, message_id=5001,
        reporter_id=7, description="x",
    )
    await _persistence.update_feature_request_status(5001, "accepted", resolved_by=99)
    row = await _persistence.get_feature_request_by_message(5001)
    assert row["status"] == "accepted"
    assert row["resolved_by"] == 99
    assert row["resolved_at"] is not None


async def test_link_feature_to_request_and_reverse_lookup(db):
    """Once linked, the request resolves both ways: by its own message_id
    and by the spawned feature issue's id."""
    await _persistence.insert_feature_request(
        guild_id=42, channel_id=200, message_id=5002,
        reporter_id=7, description="link me",
    )
    # Mint a "feature issue id" — in reality this comes from insert_issue;
    # here we just need any plausible value to back-fill into the FK column.
    feature_issue_id = 12345
    await _persistence.link_feature_to_request(5002, feature_issue_id)

    by_msg = await _persistence.get_feature_request_by_message(5002)
    assert by_msg["feature_issue_id"] == feature_issue_id

    by_feature = await _persistence.get_feature_request_by_feature_id(feature_issue_id)
    assert by_feature is not None
    assert by_feature["message_id"] == 5002


async def test_get_by_feature_id_missing_returns_none(db):
    assert await _persistence.get_feature_request_by_feature_id(99999) is None
