"""Tier E: scratchoff daily-limit logic.

The eligibility check at the top of cmd_scratchoff was extracted into
scratchoff_attempts_remaining(user, today) so the rollover + cap can be
tested in isolation. The full command (RNG card draw, payout tiers,
streak/role logic) is integration territory; the daily-limit invariant
is what matters here.
"""
import pytest

from src.gambling.scratchoff import scratchoff_attempts_remaining


class TestScratchoffAttemptsRemaining:
    def test_fresh_user_gets_three(self):
        user = {}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 3
        # Side effect: scratch_date set to today, scratch_used initialized.
        assert user["scratch_date"] == "2026-05-02"
        assert user["scratch_used"] == 0

    def test_partial_today(self):
        user = {"scratch_date": "2026-05-02", "scratch_used": 1}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 2
        # Same-day call doesn't reset.
        assert user["scratch_used"] == 1

    def test_exhausted_today(self):
        user = {"scratch_date": "2026-05-02", "scratch_used": 3}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 0

    def test_stale_date_resets_to_full(self):
        """User had used all 3 yesterday — today they're back to 3."""
        user = {"scratch_date": "2026-05-01", "scratch_used": 3}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 3
        assert user["scratch_date"] == "2026-05-02"
        assert user["scratch_used"] == 0

    def test_stale_partial_also_resets(self):
        user = {"scratch_date": "2026-05-01", "scratch_used": 1}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 3
        assert user["scratch_used"] == 0

    def test_missing_scratch_used_treated_as_zero(self):
        """A user record that has scratch_date but lacks scratch_used (stored
        from an older schema) shouldn't crash."""
        user = {"scratch_date": "2026-05-02"}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 3
        # Side effect normalizes the field.
        assert user["scratch_used"] == 0

    def test_overflow_scratch_used_clamped_at_zero(self):
        """Defensive: a corrupted scratch_used > 3 must not produce a
        negative remaining count."""
        user = {"scratch_date": "2026-05-02", "scratch_used": 99}
        assert scratchoff_attempts_remaining(user, "2026-05-02") == 0

    def test_does_not_mutate_when_date_matches(self):
        """When scratch_date already == today, no reset is performed."""
        user = {"scratch_date": "2026-05-02", "scratch_used": 2}
        scratchoff_attempts_remaining(user, "2026-05-02")
        assert user == {"scratch_date": "2026-05-02", "scratch_used": 2}
