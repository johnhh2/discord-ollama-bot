"""Pure-function tests for the 6h CT bucket scheme.

Both helpers are foundational — every history row's PK and every graph's
x-axis depends on them computing the right thing. These tests exist
because a bucket-math bug would corrupt data silently for hours before
showing up on a chart.
"""
import datetime
from zoneinfo import ZoneInfo

import pytest

import src.economy as _economy


_CT = ZoneInfo("America/Chicago")
_UTC = datetime.timezone.utc


def _at_ct(year: int, month: int, day: int, hour: int) -> datetime.datetime:
    """Helper: build a CT-aware datetime at a given hour."""
    return datetime.datetime(year, month, day, hour, tzinfo=_CT)


# ── _current_bucket_ct ───────────────────────────────────────────────────────


class TestCurrentBucketCt:
    @pytest.mark.parametrize("hour,expected", [
        (0, 0), (1, 0), (5, 0),       # bucket 0: 00:00–05:59
        (6, 1), (8, 1), (11, 1),      # bucket 1: 06:00–11:59
        (12, 2), (15, 2), (17, 2),    # bucket 2: 12:00–17:59
        (18, 3), (20, 3), (23, 3),    # bucket 3: 18:00–23:59
    ])
    def test_bucket_for_each_hour(self, monkeypatch, hour, expected):
        """Every CT hour maps to exactly one of 4 buckets."""
        monkeypatch.setattr(_economy, "_ct_now",
                            lambda: _at_ct(2026, 5, 4, hour))
        assert _economy._current_bucket_ct() == expected


# ── _bucket_start_dt ─────────────────────────────────────────────────────────


class TestBucketStartDt:
    def test_bucket_0_is_midnight_ct(self):
        """Bucket 0 of 2026-05-04 starts at 2026-05-04 00:00 CT."""
        result = _economy._bucket_start_dt("2026-05-04", 0)
        # Round-trip through CT to compare against the expected wall-clock.
        in_ct = result.astimezone(_CT)
        assert in_ct == _at_ct(2026, 5, 4, 0)

    def test_bucket_1_is_six_am_ct(self):
        result = _economy._bucket_start_dt("2026-05-04", 1)
        assert result.astimezone(_CT) == _at_ct(2026, 5, 4, 6)

    def test_bucket_2_is_noon_ct(self):
        result = _economy._bucket_start_dt("2026-05-04", 2)
        assert result.astimezone(_CT) == _at_ct(2026, 5, 4, 12)

    def test_bucket_3_is_six_pm_ct(self):
        result = _economy._bucket_start_dt("2026-05-04", 3)
        assert result.astimezone(_CT) == _at_ct(2026, 5, 4, 18)

    def test_returns_utc_aware_datetime(self):
        """matplotlib expects UTC-aware datetimes for date axes."""
        result = _economy._bucket_start_dt("2026-05-04", 2)
        assert result.tzinfo == _UTC

    def test_handles_dst_spring_forward(self):
        """March 8 2026 is a CT spring-forward day. Bucket 1 starts at 6am
        CDT — the helper must still return a sensible UTC datetime.
        """
        # 2026-03-08 06:00 CT (CDT, UTC-5) = 11:00 UTC.
        result = _economy._bucket_start_dt("2026-03-08", 1)
        assert result.tzinfo == _UTC
        # In CT it should still be 06:00 wall-clock on that day.
        in_ct = result.astimezone(_CT)
        assert in_ct.year == 2026 and in_ct.month == 3 and in_ct.day == 8
        assert in_ct.hour == 6

    def test_consecutive_buckets_are_six_hours_apart(self):
        b0 = _economy._bucket_start_dt("2026-05-04", 0)
        b1 = _economy._bucket_start_dt("2026-05-04", 1)
        b2 = _economy._bucket_start_dt("2026-05-04", 2)
        b3 = _economy._bucket_start_dt("2026-05-04", 3)
        six_hours = datetime.timedelta(hours=6)
        assert b1 - b0 == six_hours
        assert b2 - b1 == six_hours
        assert b3 - b2 == six_hours
