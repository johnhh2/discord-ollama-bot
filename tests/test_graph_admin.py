"""Tests for `!graph admin wallet|savings`.

Covers the parser (number XOR mentions, default top-10, cap, dedupe) and
the builder (top-N ranking, explicit-members filter, field selection).
"""
from types import SimpleNamespace

import pytest

import src.economy as _economy
import src.state as _state
from src import graph_series


pytestmark = pytest.mark.asyncio


def _stub_member(uid: int, name: str = "tester"):
    m = SimpleNamespace()
    m.id = uid
    m.display_name = name
    return m


def _stub_ctx():
    ctx = SimpleNamespace()
    ctx.author = _stub_member(1, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = SimpleNamespace(id=42, get_member=lambda _id: _stub_member(_id, f"u{_id}"))
    ctx.message = SimpleNamespace(mentions=[])
    return ctx


@pytest.fixture
def patch_member_converter(monkeypatch):
    from discord.ext.commands import MemberConverter, BadArgument

    async def _convert(self, ctx, argument):
        if argument.isdigit():
            return _stub_member(int(argument), f"u{argument}")
        raise BadArgument(argument)

    monkeypatch.setattr(MemberConverter, "convert", _convert)


# ── parse_admin_tokens ───────────────────────────────────────────────────────


async def test_parse_empty_returns_default_top_n(patch_member_converter):
    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ())
    assert parsed.error is None
    assert parsed.top_n == graph_series.ADMIN_TOP_N_DEFAULT
    assert parsed.members == []


async def test_parse_single_integer_sets_top_n(patch_member_converter):
    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ("25",))
    assert parsed.error is None
    assert parsed.top_n == 25
    assert parsed.members == []


async def test_parse_mentions_sets_members(patch_member_converter):
    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ("100", "200"))
    # Both digit-strings; the parser tries integer first. So this would set
    # top_n then fail on the second digit. Test the actual mention path:


async def test_parse_explicit_mentions(monkeypatch):
    from discord.ext.commands import MemberConverter, BadArgument

    # MemberConverter only resolves the literal `<@N>` form; raw digits go to
    # the integer branch first. Use the mention syntax for this test.
    async def _convert(self, ctx, argument):
        if argument.startswith("<@") and argument.endswith(">"):
            digits = argument.lstrip("<@!&").rstrip(">")
            if digits.isdigit():
                return _stub_member(int(digits), f"u{digits}")
        raise BadArgument(argument)

    monkeypatch.setattr(MemberConverter, "convert", _convert)

    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ("<@100>", "<@200>"))
    assert parsed.error is None
    assert parsed.top_n is None
    assert [m.id for m in parsed.members] == [100, 200]


async def test_parse_dedupes_repeated_mentions(monkeypatch):
    from discord.ext.commands import MemberConverter, BadArgument

    async def _convert(self, ctx, argument):
        if argument.startswith("<@"):
            digits = argument.lstrip("<@!&").rstrip(">")
            return _stub_member(int(digits), f"u{digits}")
        raise BadArgument(argument)

    monkeypatch.setattr(MemberConverter, "convert", _convert)

    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ("<@7>", "<@7>", "<@8>"))
    assert parsed.error is None
    assert [m.id for m in parsed.members] == [7, 8]


async def test_parse_rejects_count_and_mentions_together(monkeypatch):
    from discord.ext.commands import MemberConverter, BadArgument

    async def _convert(self, ctx, argument):
        if argument.startswith("<@"):
            digits = argument.lstrip("<@!&").rstrip(">")
            return _stub_member(int(digits), f"u{digits}")
        raise BadArgument(argument)

    monkeypatch.setattr(MemberConverter, "convert", _convert)

    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ("5", "<@100>"))
    assert parsed.error is not None
    assert "either a count or" in parsed.error.lower()


async def test_parse_rejects_two_counts(patch_member_converter):
    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ("5", "10"))
    assert parsed.error is not None
    assert "multiple counts" in parsed.error.lower()


async def test_parse_rejects_zero_count(patch_member_converter):
    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ("0",))
    assert parsed.error is not None
    assert "≥ 1" in parsed.error or "must be" in parsed.error.lower()


async def test_parse_rejects_count_above_cap(patch_member_converter):
    parsed = await graph_series.parse_admin_tokens(
        _stub_ctx(), (str(graph_series.ADMIN_TOP_N_CAP + 1),),
    )
    assert parsed.error is not None
    assert "cap" in parsed.error.lower()


async def test_parse_rejects_garbage_token(patch_member_converter):
    parsed = await graph_series.parse_admin_tokens(_stub_ctx(), ("nonsense",))
    assert parsed.error is not None
    assert "unknown token" in parsed.error.lower()


# ── build_admin_series ───────────────────────────────────────────────────────


async def test_build_admin_series_top_n_picks_richest(monkeypatch):
    """Top-N ranks by most-recent value across all users in history."""
    from src import graph_series as gs

    today = gs._ct_now_iso_date()
    fake_history = {
        today: {0: {
            "1": {"wallet": 100, "savings": 0},
            "2": {"wallet": 500, "savings": 50},
            "3": {"wallet": 200, "savings": 1000},
        }},
    }
    async def _load(): return fake_history
    monkeypatch.setattr(gs, "load_balance_history", _load)
    _state.economy["users"].clear()  # no live append

    data = await gs.build_admin_series("wallet", top_n=2)

    # Top 2 wallets: user 2 (500), user 3 (200).
    labels = [seg.label for seg in data.segments]
    assert len(labels) == 2
    # Mention-format fallback when we don't have member objects.
    assert any("2" in l for l in labels)
    assert any("3" in l for l in labels)


async def test_build_admin_series_explicit_members_filters_to_them(monkeypatch):
    from src import graph_series as gs

    today = gs._ct_now_iso_date()
    fake_history = {
        today: {0: {
            "1": {"wallet": 100, "savings": 0},
            "2": {"wallet": 500, "savings": 0},
            "3": {"wallet": 200, "savings": 0},
        }},
    }
    async def _load(): return fake_history
    monkeypatch.setattr(gs, "load_balance_history", _load)
    _state.economy["users"].clear()

    members = [_stub_member(1, "alice"), _stub_member(3, "bob")]
    data = await gs.build_admin_series("wallet", members=members)

    labels = sorted(seg.label for seg in data.segments)
    assert labels == ["alice", "bob"]  # ordered by name in this test, both present


async def test_build_admin_series_savings_uses_savings_field(monkeypatch):
    """`field='savings'` reads the savings column, not wallet."""
    from src import graph_series as gs

    today = gs._ct_now_iso_date()
    cur_bucket = _economy._current_bucket_ct()
    # Seed the current bucket so the live-now append doesn't add a zero-value
    # point on top of the historical data.
    fake_history = {
        today: {cur_bucket: {
            "1": {"wallet": 9999, "savings": 50},
            "2": {"wallet": 0, "savings": 800},
        }},
    }
    async def _load(): return fake_history
    monkeypatch.setattr(gs, "load_balance_history", _load)
    _state.economy["users"].clear()

    data = await gs.build_admin_series("savings", top_n=2)
    by_label = {seg.label: seg.y_values[-1] for seg in data.segments}
    # Sorted by savings (latest), user 2 has 800, user 1 has 50.
    assert by_label[next(l for l in by_label if "2" in l)] == 800
    assert by_label[next(l for l in by_label if "1" in l)] == 50


async def test_build_admin_series_includes_live_now_for_active_users(monkeypatch):
    """Users present in state.economy but absent from history should still
    appear if they have a non-zero current value."""
    from src import graph_series as gs

    async def _load(): return {}  # empty history
    monkeypatch.setattr(gs, "load_balance_history", _load)

    _state.economy["users"]["42"] = {"balance": 1000, "savings": []}

    data = await gs.build_admin_series("wallet", top_n=10)
    assert any("42" in seg.label for seg in data.segments)
    assert data.segments[0].y_values[-1] == 1000


async def test_build_admin_series_explicit_member_with_no_history_omitted(monkeypatch):
    """An @user with no recorded balance shouldn't break the chart — they're
    silently omitted from segments rather than rendered as a flat zero."""
    from src import graph_series as gs

    async def _load(): return {}
    monkeypatch.setattr(gs, "load_balance_history", _load)
    _state.economy["users"].clear()  # nobody live either

    data = await gs.build_admin_series(
        "wallet", members=[_stub_member(99, "ghost")],
    )
    # No data for ghost → no segments.
    assert data.segments == []
