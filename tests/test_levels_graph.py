"""Tests for the !graph levels series and level-up event tracking."""
from types import SimpleNamespace

import pytest

import src.economy as _economy
import src.leveling as _leveling
import src.persistence as _persistence
import src.state as _state
from src import graph_series


def _stub_member(uid: int, name: str = "tester"):
    m = SimpleNamespace()
    m.id = uid
    m.display_name = name
    return m


# ── record_levelup ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_levelup_aggregates_per_guild_user():
    await _leveling.record_levelup(1, 7, count=1)
    await _leveling.record_levelup(1, 7, count=2)  # multi-step
    await _leveling.record_levelup(2, 7, count=1)  # different guild, same user
    await _leveling.record_levelup(1, 8, count=1)  # different user

    assert _state.levelups_today[(1, "7")] == 3
    assert _state.levelups_today[(2, "7")] == 1
    assert _state.levelups_today[(1, "8")] == 1


@pytest.mark.asyncio
async def test_record_levelup_zero_or_negative_is_noop():
    await _leveling.record_levelup(1, 7, count=0)
    await _leveling.record_levelup(1, 7, count=-1)
    assert (1, "7") not in _state.levelups_today


# ── atomic-write contract ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_levelup_writes_atomically_to_disk(db):
    """Atomic-write contract: every level boundary crossing must persist
    immediately."""
    await _leveling.record_levelup(100, 42, count=2)
    await _leveling.record_levelup(100, 99, count=1)

    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_levelup_history()
    assert history[today][(100, "42")] == 2
    assert history[today][(100, "99")] == 1


@pytest.mark.asyncio
async def test_record_levelup_increments_existing_row(db):
    await _leveling.record_levelup(100, 42, count=1)
    await _leveling.record_levelup(100, 42, count=3)
    today = _economy._ct_now().date().isoformat()
    history = await _persistence.load_levelup_history()
    assert history[today][(100, "42")] == 4


@pytest.mark.asyncio
async def test_levelups_dict_survives_do_daily_reset(db, monkeypatch):
    """Level-up counts are calendar-keyed on disk. The 5am gameplay reset
    must NOT clear them — clearing would desync the cache from the row that
    just keeps accumulating until calendar midnight."""
    await _leveling.record_levelup(100, 42, count=2)

    async def _ollama_up(): return True
    monkeypatch.setattr("src.ai.check_ollama_connected", _ollama_up)

    await _economy.do_daily_reset()

    assert _state.levelups_today[(100, "42")] == 2


@pytest.mark.asyncio
async def test_init_db_state_hydrates_today_levelups_dict(db):
    await _leveling.record_levelup(100, 42, count=2)
    _state.levelups_today.clear()
    await _persistence.init_db_state()
    assert _state.levelups_today[(100, "42")] == 2


# ── build_series_levels ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_series_levels_filters_to_guild_and_user(monkeypatch):
    today = graph_series._ct_today_date()
    yest = today.replace(day=today.day - 1) if today.day > 1 else None
    if yest is None:
        pytest.skip("Edge case: today is the 1st; skipping date-arithmetic shortcut.")

    fake_history = {
        yest.isoformat(): {
            (100, "42"): 1,
            (100, "99"): 5,   # different user, same guild — must NOT appear
            (200, "42"): 7,   # same user, different guild — must NOT appear
        },
        today.isoformat(): {
            (100, "42"): 2,
        },
    }

    async def _load(): return fake_history
    monkeypatch.setattr(graph_series, "load_levelup_history", _load)
    _state.levelups_today[(100, "42")] = 2

    member = _stub_member(42, "alice")
    data = await graph_series.build_series_levels(member, 100)

    seg = data.segments[0]
    assert seg.label == "Level-ups"
    # Two days of activity for (100, "42"): yesterday=1, today=2. Other rows ignored.
    assert seg.y_values == [1, 2]


@pytest.mark.asyncio
async def test_build_series_levels_skips_inactive_days(monkeypatch):
    today = graph_series._ct_today_date()
    yest = today.replace(day=today.day - 1) if today.day > 1 else None
    if yest is None:
        pytest.skip("Edge case: today is the 1st; skipping date-arithmetic shortcut.")

    fake_history = {
        yest.isoformat(): {(100, "42"): 1},
        today.isoformat(): {(100, "99"): 3},  # other user only
    }
    async def _load(): return fake_history
    monkeypatch.setattr(graph_series, "load_levelup_history", _load)

    member = _stub_member(42, "alice")
    data = await graph_series.build_series_levels(member, 100)

    assert len(data.x_dates) == 1
    assert data.x_dates[0] == yest


# ── token parsing ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_resolves_levels_aliases(monkeypatch):
    from discord.ext.commands import MemberConverter, BadArgument

    async def _convert(self, ctx, argument):
        if argument.isdigit():
            return _stub_member(int(argument), f"u{argument}")
        raise BadArgument(argument)

    monkeypatch.setattr(MemberConverter, "convert", _convert)

    ctx = SimpleNamespace()
    ctx.author = _stub_member(100, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = SimpleNamespace(id=42)
    ctx.message = SimpleNamespace(mentions=[])

    for alias in ("levels", "level", "lvl"):
        parsed = await graph_series.parse_tokens(ctx, (alias,))
        assert parsed.error is None, f"alias `{alias}` should resolve"
        assert parsed.specs[0].name == "levels"
        assert parsed.guild_id == 42


@pytest.mark.asyncio
async def test_parse_levels_in_dm_rejected():
    """No ctx.guild → reject with a helpful message."""
    ctx = SimpleNamespace()
    ctx.author = _stub_member(100, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = None
    ctx.message = SimpleNamespace(mentions=[])

    parsed = await graph_series.parse_tokens(ctx, ("levels",))
    assert parsed.error is not None
    assert "per-server" in parsed.error or "DM" in parsed.error


@pytest.mark.asyncio
async def test_levels_rejected_when_combined_with_other_groups():
    ctx = SimpleNamespace()
    ctx.author = _stub_member(100, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = SimpleNamespace(id=42)
    ctx.message = SimpleNamespace(mentions=[])

    # XP group is its own; can't combine with coins, counts, or MB.
    parsed = await graph_series.parse_tokens(ctx, ("levels", "balance"))
    assert parsed.error is not None
    assert "incompatible" in parsed.error


# ── grant_xp integration ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grant_xp_records_levelup_count(db, monkeypatch):
    """Verify wiring: a grant_xp call that crosses N level boundaries records N."""
    from src.leveling import grant_xp, _ensure_lvl_record

    # Seed user just below a level threshold so a single grant pushes them up.
    rec = _ensure_lvl_record(guild_id=100, uid=42)
    rec["xp"] = 99  # one msg of XP_MESSAGE will tip over level 0 → 1
    rec["level"] = 0

    xp, leveled = await grant_xp(uid=42, source="msg", guild_id=100)
    assert leveled is True
    assert _state.levelups_today.get((100, "42"), 0) >= 1
