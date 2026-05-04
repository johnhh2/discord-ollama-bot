"""Tests for the !graph gambling series and the gambling-event tracking
that feeds it.

Covers:
  - record_gambling_event aggregates by user across multiple events.
  - snapshot_gambling round-trips through the persistence layer.
  - build_series_gambling returns Gained / Lost / Net segments with the
    correct net = gained - lost relationship.
  - parse_tokens resolves "gambling" / "gamble" / "games" aliases.
  - "gambling" combines with other coins-group series.
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


# ── record_gambling_event ─────────────────────────────────────────────────────


def test_record_gambling_event_aggregates_per_user():
    _economy.record_gambling_event(7, gained=100)
    _economy.record_gambling_event(7, lost=30)
    _economy.record_gambling_event(7, gained=200)
    _economy.record_gambling_event(8, lost=50)

    assert _state.gambling_today_by_user["7"] == {"gained": 300, "lost": 30}
    assert _state.gambling_today_by_user["8"] == {"gained": 0, "lost": 50}


def test_record_gambling_event_zero_is_noop():
    _economy.record_gambling_event(7, gained=0, lost=0)
    assert "7" not in _state.gambling_today_by_user


# ── snapshot_gambling round-trip ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_gambling_persists_and_loads(db):
    _economy.record_gambling_event(42, gained=500, lost=100)
    _economy.record_gambling_event(43, lost=250)

    await _economy.snapshot_gambling()

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_gambling_history()
    assert today in history
    assert history[today]["42"] == {"gained": 500, "lost": 100}
    assert history[today]["43"] == {"gained": 0, "lost": 250}


@pytest.mark.asyncio
async def test_snapshot_gambling_refreshes_today_on_repeat(db):
    _economy.record_gambling_event(42, gained=200)
    await _economy.snapshot_gambling()

    _economy.record_gambling_event(42, gained=300)
    await _economy.snapshot_gambling()

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_gambling_history()
    assert history[today]["42"]["gained"] == 500


@pytest.mark.asyncio
async def test_snapshot_gambling_preserves_existing_day_when_buffer_empty(db):
    """If the in-memory dict is empty at snapshot time (just after the 5am
    daily clear, on a no-activity day), the existing row should not be wiped.
    """
    _economy.record_gambling_event(42, gained=200)
    await _economy.snapshot_gambling()

    _state.gambling_today_by_user.clear()
    await _economy.snapshot_gambling()

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_gambling_history()
    assert history[today]["42"]["gained"] == 200


# ── build_series_gambling ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_series_gambling_returns_gained_lost_net(monkeypatch):
    today = graph_series._ct_today_date()
    yest = today.replace(day=today.day - 1) if today.day > 1 else None
    if yest is None:
        pytest.skip("Edge case: today is the 1st; skipping date-arithmetic shortcut.")

    fake_history = {
        yest.isoformat(): {"42": {"gained": 100, "lost": 50}},
        today.isoformat(): {"42": {"gained": 300, "lost": 80}},
    }
    async def _load(): return fake_history
    monkeypatch.setattr(graph_series, "load_gambling_history", _load)
    _state.gambling_today_by_user["42"] = {"gained": 300, "lost": 80}

    member = _stub_member(42, "alice")
    data = await graph_series.build_series_gambling(member)

    labels = [seg.label for seg in data.segments]
    assert labels == ["Gained", "Lost", "Net"]

    gained = next(s for s in data.segments if s.label == "Gained")
    lost = next(s for s in data.segments if s.label == "Lost")
    net = next(s for s in data.segments if s.label == "Net")

    # Net = gained - lost for every entry.
    for g, l, n in zip(gained.y_values, lost.y_values, net.y_values):
        assert n == g - l


@pytest.mark.asyncio
async def test_build_series_gambling_skips_inactive_days(monkeypatch):
    today = graph_series._ct_today_date()
    yest = today.replace(day=today.day - 1) if today.day > 1 else None
    if yest is None:
        pytest.skip("Edge case: today is the 1st; skipping date-arithmetic shortcut.")

    fake_history = {
        yest.isoformat(): {"42": {"gained": 100, "lost": 50}},
        today.isoformat(): {"99": {"gained": 1, "lost": 1}},  # other user only
    }
    async def _load(): return fake_history
    monkeypatch.setattr(graph_series, "load_gambling_history", _load)

    member = _stub_member(42, "alice")
    data = await graph_series.build_series_gambling(member)

    assert len(data.x_dates) == 1
    assert data.x_dates[0] == yest


# ── token parsing ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_resolves_gambling_aliases(monkeypatch):
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

    for alias in ("gambling", "gamble", "games", "game"):
        parsed = await graph_series.parse_tokens(ctx, (alias,))
        assert parsed.error is None, f"alias `{alias}` should resolve"
        assert parsed.specs[0].name == "gambling"


@pytest.mark.asyncio
async def test_gambling_combines_with_other_coins_series(monkeypatch):
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

    parsed = await graph_series.parse_tokens(
        ctx, ("balance", "crime", "gambling", "42")
    )
    assert parsed.error is None
    assert {s.name for s in parsed.specs} == {"balance", "crime", "gambling"}
    assert parsed.member.id == 42


@pytest.mark.asyncio
async def test_gambling_rejected_when_combined_with_counts():
    ctx = SimpleNamespace()
    ctx.author = _stub_member(100, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = None
    ctx.message = SimpleNamespace(mentions=[])

    parsed = await graph_series.parse_tokens(ctx, ("gambling", "server"))
    assert parsed.error is not None
    assert "incompatible" in parsed.error
