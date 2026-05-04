"""Tests for the !graph crime series and the crime-event tracking that feeds it.

Covers:
  - record_crime_event aggregates by user across multiple events.
  - snapshot_crime round-trips through the persistence layer.
  - build_series_crime returns Gained/Lost segments and skips inactive days.
  - parse_tokens resolves "crime" alias and pairs with @member.
  - "crime" combines with "balance" / "economy" (all coins group).
"""
from types import SimpleNamespace

import pytest

import src.economy as _economy
import src.persistence as _persistence
import src.state as _state
from src import graph_series


def _stub_member(uid: int, name: str = "tester"):
    m = SimpleNamespace()
    m.id = uid
    m.display_name = name
    return m


# ── record_crime_event ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_crime_event_aggregates_per_user():
    await _economy.record_crime_event(7, gained=100)
    await _economy.record_crime_event(7, lost=30)
    await _economy.record_crime_event(7, gained=50, lost=10)
    await _economy.record_crime_event(8, lost=40)

    assert _state.crime_today_by_user["7"] == {"gained": 150, "lost": 40}
    assert _state.crime_today_by_user["8"] == {"gained": 0, "lost": 40}


@pytest.mark.asyncio
async def test_record_crime_event_zero_is_noop():
    await _economy.record_crime_event(7, gained=0, lost=0)
    assert "7" not in _state.crime_today_by_user


# ── snapshot_crime round-trip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_crime_event_writes_atomically_to_disk(db):
    """The whole point of the async upsert: every record_crime_event call
    must hit disk immediately, not wait for the 6h scheduler. This pins that
    a bot crash 1ms after the call still has the event durable."""
    await _economy.record_crime_event(42, gained=500, lost=100)
    await _economy.record_crime_event(43, lost=250)

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_crime_history()
    assert today in history
    assert history[today]["42"] == {"gained": 500, "lost": 100}
    assert history[today]["43"] == {"gained": 0, "lost": 250}


@pytest.mark.asyncio
async def test_record_crime_event_increments_existing_row(db):
    """Two calls for the same user same day should add, not replace.
    MariaDB's `gained = gained + VALUES(gained)` is doing the work."""
    await _economy.record_crime_event(42, gained=100)
    await _economy.record_crime_event(42, gained=50, lost=20)
    await _economy.record_crime_event(42, lost=30)

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_crime_history()
    assert history[today]["42"] == {"gained": 150, "lost": 50}


@pytest.mark.asyncio
async def test_init_db_state_hydrates_today_crime_dict(db):
    """After a "restart" (clear in-memory + re-run init_db_state), the dict
    is repopulated from disk so the graph cog's live-today read is correct."""
    await _economy.record_crime_event(42, gained=500, lost=100)
    # Simulate restart: dict cleared.
    _state.crime_today_by_user.clear()
    # Re-run init_db_state.
    await _persistence.init_db_state()
    assert _state.crime_today_by_user["42"] == {"gained": 500, "lost": 100}


@pytest.mark.asyncio
async def test_snapshot_crime_persists_and_loads(db):
    await _economy.record_crime_event(42, gained=500, lost=100)
    await _economy.record_crime_event(43, lost=250)

    await _economy.snapshot_crime()

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_crime_history()
    assert today in history
    assert history[today]["42"] == {"gained": 500, "lost": 100}
    assert history[today]["43"] == {"gained": 0, "lost": 250}


@pytest.mark.asyncio
async def test_snapshot_crime_refreshes_today_on_repeat(db):
    """Multiple snapshots within the same gameplay-day should overwrite, not append."""
    await _economy.record_crime_event(42, gained=200)
    await _economy.snapshot_crime()

    await _economy.record_crime_event(42, gained=300)
    await _economy.snapshot_crime()

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_crime_history()
    assert history[today]["42"]["gained"] == 500


@pytest.mark.asyncio
async def test_snapshot_crime_with_no_events_doesnt_overwrite_existing_day(db):
    """If the in-memory dict is empty (e.g. just after do_daily_reset cleared it
    on a no-activity day), snapshot_crime should NOT wipe the existing row."""
    await _economy.record_crime_event(42, gained=200)
    await _economy.snapshot_crime()

    _state.crime_today_by_user.clear()
    await _economy.snapshot_crime()

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_crime_history()
    assert history[today]["42"]["gained"] == 200


# ── build_series_crime ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_series_crime_returns_gained_and_lost_segments(monkeypatch):
    today = graph_series._ct_today_date()
    yest = today.replace(day=today.day - 1) if today.day > 1 else None
    if yest is None:
        pytest.skip("Edge case: today is the 1st; skipping date-arithmetic shortcut.")

    fake_history = {
        yest.isoformat(): {"42": {"gained": 100, "lost": 50}},
        today.isoformat(): {"42": {"gained": 200, "lost": 80}},
    }

    async def _load(): return fake_history
    monkeypatch.setattr(graph_series, "load_crime_history", _load)
    _state.crime_today_by_user["42"] = {"gained": 200, "lost": 80}

    member = _stub_member(42, "alice")
    data = await graph_series.build_series_crime(member)

    labels = {seg.label for seg in data.segments}
    assert labels == {"Gained", "Lost"}
    assert len(data.x_dates) >= 2

    gained = next(s for s in data.segments if s.label == "Gained")
    lost = next(s for s in data.segments if s.label == "Lost")
    # Today's entry should appear once (driven by either the persisted snapshot
    # or the live append, but never doubled).
    assert gained.y_values[-1] == 200
    assert lost.y_values[-1] == 80


@pytest.mark.asyncio
async def test_build_series_crime_skips_days_user_was_inactive(monkeypatch):
    today = graph_series._ct_today_date()
    yest = today.replace(day=today.day - 1) if today.day > 1 else None
    if yest is None:
        pytest.skip("Edge case: today is the 1st; skipping date-arithmetic shortcut.")

    # Day-1 has activity for user 42, day-2 doesn't.
    fake_history = {
        yest.isoformat(): {"42": {"gained": 100, "lost": 50}},
        today.isoformat(): {"99": {"gained": 1, "lost": 1}},  # other user only
    }

    async def _load(): return fake_history
    monkeypatch.setattr(graph_series, "load_crime_history", _load)

    member = _stub_member(42, "alice")
    data = await graph_series.build_series_crime(member)

    # User 42 only has yesterday — no live today entry, so just one date.
    assert len(data.x_dates) == 1
    assert data.x_dates[0] == yest


# ── token parsing ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_resolves_crime_alias(monkeypatch):
    from discord.ext.commands import MemberConverter, BadArgument

    async def _convert(self, ctx, argument):
        if argument.isdigit():
            return _stub_member(int(argument), f"u{argument}")
        raise BadArgument(argument)

    monkeypatch.setattr(MemberConverter, "convert", _convert)

    ctx = SimpleNamespace()
    ctx.author = _stub_member(100, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = None
    ctx.message = SimpleNamespace(mentions=[])

    parsed = await graph_series.parse_tokens(ctx, ("crime", "42"))
    assert parsed.error is None
    assert [s.name for s in parsed.specs] == ["crime"]
    assert parsed.member.id == 42


@pytest.mark.asyncio
async def test_crime_combines_with_balance_and_economy(monkeypatch):
    from discord.ext.commands import MemberConverter, BadArgument

    async def _convert(self, ctx, argument):
        if argument.isdigit():
            return _stub_member(int(argument), f"u{argument}")
        raise BadArgument(argument)

    monkeypatch.setattr(MemberConverter, "convert", _convert)

    ctx = SimpleNamespace()
    ctx.author = _stub_member(100, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = None
    ctx.message = SimpleNamespace(mentions=[])

    parsed = await graph_series.parse_tokens(ctx, ("balance", "economy", "crime", "42"))
    assert parsed.error is None
    assert {s.name for s in parsed.specs} == {"balance", "economy", "crime"}
    assert parsed.member.id == 42


@pytest.mark.asyncio
async def test_crime_rejected_when_combined_with_counts(monkeypatch):
    ctx = SimpleNamespace()
    ctx.author = _stub_member(100, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = None
    ctx.message = SimpleNamespace(mentions=[])

    parsed = await graph_series.parse_tokens(ctx, ("crime", "server"))
    assert parsed.error is not None
    assert "incompatible" in parsed.error
