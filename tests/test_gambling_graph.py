"""Tests for the !graph gambling series and the gambling-event tracking
that feeds it.

Covers:
  - record_gambling_event aggregates by user across multiple events.
  - record_gambling_event persists each event atomically (no data loss on restart).
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


@pytest.mark.asyncio
async def test_record_gambling_event_aggregates_per_user():
    await _economy.record_gambling_event(7, gained=100)
    await _economy.record_gambling_event(7, lost=30)
    await _economy.record_gambling_event(7, gained=200)
    await _economy.record_gambling_event(8, lost=50)

    assert _state.gambling_today_by_user["7"] == {"gained": 300, "lost": 30}
    assert _state.gambling_today_by_user["8"] == {"gained": 0, "lost": 50}


@pytest.mark.asyncio
async def test_record_gambling_event_zero_is_noop():
    await _economy.record_gambling_event(7, gained=0, lost=0)
    assert "7" not in _state.gambling_today_by_user


# ── atomic-write contract ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_gambling_event_writes_atomically_to_disk(db):
    """Atomic-write contract: a record_gambling_event call must persist
    immediately, so a bot crash mid-game-session can't lose stats."""
    await _economy.record_gambling_event(42, gained=500, lost=100)
    await _economy.record_gambling_event(43, lost=250)

    today = _economy._ct_now().date().isoformat()
    bucket = _economy._current_bucket_ct()
    history = await _persistence.load_gambling_history()
    assert history[today][bucket]["42"] == {"gained": 500, "lost": 100}
    assert history[today][bucket]["43"] == {"gained": 0, "lost": 250}


@pytest.mark.asyncio
async def test_record_gambling_event_increments_existing_row(db):
    await _economy.record_gambling_event(42, gained=100)
    await _economy.record_gambling_event(42, gained=50, lost=20)
    today = _economy._ct_now().date().isoformat()
    bucket = _economy._current_bucket_ct()
    history = await _persistence.load_gambling_history()
    assert history[today][bucket]["42"] == {"gained": 150, "lost": 20}


@pytest.mark.asyncio
async def test_record_gambling_event_clears_dict_on_bucket_rollover(db, monkeypatch):
    """Bucket rollover invariant for gambling — same as crime."""
    monkeypatch.setattr(_economy, "_current_bucket_ct", lambda: 0)
    await _economy.record_gambling_event(42, gained=300)
    assert _state.gambling_today_by_user["42"]["gained"] == 300

    monkeypatch.setattr(_economy, "_current_bucket_ct", lambda: 1)
    await _economy.record_gambling_event(42, lost=100)

    # Cache reflects only the new bucket.
    assert _state.gambling_today_by_user["42"] == {"gained": 0, "lost": 100}

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_gambling_history()
    assert history[today][0]["42"]["gained"] == 300
    assert history[today][1]["42"]["lost"] == 100


@pytest.mark.asyncio
async def test_gambling_dict_survives_do_daily_reset(db, monkeypatch):
    """Gambling totals are calendar-keyed; the 5am gameplay reset must NOT
    clear them. Same invariant as crime."""
    await _economy.record_gambling_event(42, gained=500, lost=100)

    async def _ollama_up(): return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ollama_up)

    await _economy.do_daily_reset()

    assert _state.gambling_today_by_user["42"] == {"gained": 500, "lost": 100}


@pytest.mark.asyncio
async def test_init_db_state_hydrates_today_gambling_dict(db):
    await _economy.record_gambling_event(42, gained=500, lost=100)
    _state.gambling_today_by_user.clear()
    await _persistence.init_db_state()
    assert _state.gambling_today_by_user["42"] == {"gained": 500, "lost": 100}


# ── build_series_gambling ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_series_gambling_returns_gained_lost_net(monkeypatch):
    today = graph_series._ct_today_date()
    yest = today.replace(day=today.day - 1) if today.day > 1 else None
    if yest is None:
        pytest.skip("Edge case: today is the 1st; skipping date-arithmetic shortcut.")

    cur_bucket = graph_series._current_bucket_ct()
    fake_history = {
        yest.isoformat(): {0: {"42": {"gained": 100, "lost": 50}}},
        today.isoformat(): {cur_bucket: {"42": {"gained": 300, "lost": 80}}},
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
        yest.isoformat(): {0: {"42": {"gained": 100, "lost": 50}}},
        today.isoformat(): {0: {"99": {"gained": 1, "lost": 1}}},  # other user only
    }
    async def _load(): return fake_history
    monkeypatch.setattr(graph_series, "load_gambling_history", _load)

    member = _stub_member(42, "alice")
    data = await graph_series.build_series_gambling(member)

    assert len(data.x_points) == 1
    assert data.x_points[0].astimezone(_economy._ct_now().tzinfo).date() == yest


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
