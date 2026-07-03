"""Tests for the graph-series combiner.

Covers:
  - parse_tokens: alias resolution, member at any position, dedupe, same-group.
  - render_combined: doesn't crash for single & combined modes within each group.
  - Solo balance with default member (invoker).
"""
import datetime
from types import SimpleNamespace

import pytest

import src.state as _state
from src import graph_series


pytestmark = pytest.mark.asyncio


# Render tests need matplotlib; in the dev environment it may be absent.
try:
    import matplotlib  # noqa: F401
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
needs_matplotlib = pytest.mark.skipif(not HAS_MATPLOTLIB, reason="matplotlib not installed")


def _stub_member(uid: int, name: str = "tester"):
    m = SimpleNamespace()
    m.id = uid
    m.display_name = name
    return m


def _stub_ctx(invoker_id: int = 100):
    """Minimal ctx-like object for parse_tokens. Provides .author and lets
    MemberConverter.convert() succeed for `<@N>` patterns by patching it on
    the test side (see _patch_member_converter)."""
    ctx = SimpleNamespace()
    ctx.author = _stub_member(invoker_id, "invoker")
    ctx.bot = SimpleNamespace()
    ctx.guild = SimpleNamespace(id=42, get_member=lambda _id: _stub_member(_id, f"u{_id}"))
    ctx.message = SimpleNamespace(mentions=[])
    return ctx


@pytest.fixture
def patch_member_converter(monkeypatch):
    """Make MemberConverter.convert resolve `<@N>` strings to a stub member."""
    from discord.ext.commands import MemberConverter, BadArgument

    async def _convert(self, ctx, argument):
        s = argument.strip()
        if s.startswith("<@") and s.endswith(">"):
            digits = s.lstrip("<@!&").rstrip(">")
            if digits.isdigit():
                return _stub_member(int(digits), f"u{digits}")
        # Plain numeric id is also a valid mention input.
        if s.isdigit():
            return _stub_member(int(s), f"u{s}")
        raise BadArgument(f"could not resolve {argument!r}")

    monkeypatch.setattr(MemberConverter, "convert", _convert)


# ── parse_tokens ──────────────────────────────────────────────────────────────


async def test_parse_resolves_aliases(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("eco", "bal", "<@42>"))
    assert parsed.error is None
    names = [s.name for s in parsed.specs]
    assert names == ["economy", "balance"]
    assert parsed.member.id == 42


async def test_parse_member_first_position(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("<@42>", "balance", "economy"))
    assert parsed.error is None
    assert parsed.member.id == 42
    assert {s.name for s in parsed.specs} == {"balance", "economy"}


async def test_parse_member_middle_position(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("balance", "<@42>", "economy"))
    assert parsed.error is None
    assert parsed.member.id == 42


async def test_parse_no_member_defaults_to_invoker_for_balance(patch_member_converter):
    ctx = _stub_ctx(invoker_id=999)
    parsed = await graph_series.parse_tokens(ctx, ("balance",))
    assert parsed.error is None
    assert parsed.member.id == 999


async def test_parse_no_member_for_non_balance_series(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("server", "ai"))
    assert parsed.error is None
    assert parsed.member is None


async def test_parse_rejects_cross_group(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("balance", "memory"))
    assert parsed.error is not None
    assert "incompatible" in parsed.error


async def test_parse_rejects_duplicate_alias(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("balance", "bal"))
    assert parsed.error is not None
    assert "duplicate" in parsed.error


async def test_parse_rejects_unknown_token(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("balance", "nonsense"))
    assert parsed.error is not None
    assert "unknown token" in parsed.error


async def test_parse_rejects_two_member_mentions(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("balance", "<@1>", "<@2>"))
    assert parsed.error is not None
    assert "more than one user" in parsed.error


async def test_parse_two_mentions_of_same_user_is_ok(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("balance", "<@7>", "<@7>"))
    assert parsed.error is None
    assert parsed.member.id == 7


# ── rendering smoke tests ─────────────────────────────────────────────────────


async def _seed_history(monkeypatch):
    """Stub out the persistence loaders so build_series_* doesn't hit the DB.

    Shape is {date: {bucket: payload}} — one bucket per day is enough for
    a render smoke test; full bucket-rollover semantics are covered in the
    domain-specific test files.
    """
    today = graph_series._ct_today_date()
    yest = today - datetime.timedelta(days=1)
    bal_history = {
        yest.isoformat(): {0: {"42": {"wallet": 100, "savings": 50}}},
        today.isoformat(): {2: {"42": {"wallet": 200, "savings": 80}}},
    }
    bot_history = {
        yest.isoformat(): {0: {"messages": 10, "commands": 3, "ai_responses": 1, "ai_up": True, "memory_mb": 80.0, "ping_ms": 45.0}},
        # today's bucket has no ping_ms — pre-migration row / unmeasured.
        today.isoformat(): {2: {"messages": 22, "commands": 7, "ai_responses": 4, "ai_up": True, "memory_mb": 95.0}},
    }
    cmd_history = {
        yest.isoformat(): {0: {"GraphCog": 2, "EconomyCog": 5}},
        today.isoformat(): {2: {"GraphCog": 4, "EconomyCog": 9, "AICog": 1}},
    }

    async def _bal(): return bal_history
    async def _bot(): return bot_history
    async def _cmd(): return cmd_history

    monkeypatch.setattr(graph_series, "load_balance_history", _bal)
    monkeypatch.setattr(graph_series, "load_bot_stats_history", _bot)
    monkeypatch.setattr(graph_series, "load_command_usage_history", _cmd)

    # in-memory state for the live "today" append
    _state.economy["users"]["42"] = {"balance": 200, "savings": []}
    _state.stats_messages_today = 22
    _state.stats_commands_today = 7
    _state.stats_ai_responses_today = 4
    _state.stats_commands_today_by_cog = {"GraphCog": 4, "EconomyCog": 9, "AICog": 1}


async def test_build_series_balance_has_wallet_savings_total_segments(monkeypatch):
    """Pin the 3-segment shape: Wallet + Savings + Total (with Total = Wallet
    + Savings at every point). Catches accidental drops of the savings line
    in a future refactor."""
    await _seed_history(monkeypatch)
    # Give the user some live savings so the live-now point also has both
    # halves populated.
    import time
    _state.economy["users"]["42"] = {
        "balance": 200,
        "savings": [{"amount": 80, "deposited_at": time.time()}],
    }

    member = _stub_member(42, "alice")
    data = await graph_series.build_series_balance(member)

    labels = [seg.label for seg in data.segments]
    assert labels == ["Wallet", "Savings", "Total"]

    wallet = next(s for s in data.segments if s.label == "Wallet")
    savings = next(s for s in data.segments if s.label == "Savings")
    total = next(s for s in data.segments if s.label == "Total")

    # Total must equal Wallet + Savings at every point.
    assert len(total.y_values) == len(wallet.y_values) == len(savings.y_values)
    for w, s, t in zip(wallet.y_values, savings.y_values, total.y_values):
        assert t == w + s


@needs_matplotlib
async def test_render_single_balance(monkeypatch):
    await _seed_history(monkeypatch)
    member = _stub_member(42, "alice")
    data = await graph_series.build_series_balance(member)
    buf = await graph_series.render_combined(
        [data], graph_series.GROUP_COINS, "🪙 Coins", "Test Single Balance",
    )
    assert buf.getbuffer().nbytes > 0


@needs_matplotlib
async def test_render_single_economy(monkeypatch):
    await _seed_history(monkeypatch)
    data = await graph_series.build_series_economy()
    buf = await graph_series.render_combined(
        [data], graph_series.GROUP_COINS, "🪙 Coins", "Test Single Economy",
    )
    assert buf.getbuffer().nbytes > 0


@needs_matplotlib
async def test_render_combined_coins(monkeypatch):
    await _seed_history(monkeypatch)
    member = _stub_member(42, "alice")
    bal = await graph_series.build_series_balance(member)
    eco = await graph_series.build_series_economy()
    buf = await graph_series.render_combined(
        [bal, eco], graph_series.GROUP_COINS, "🪙 Coins", "Test Combined Coins",
    )
    assert buf.getbuffer().nbytes > 0


@needs_matplotlib
async def test_render_single_commands_stacked(monkeypatch):
    await _seed_history(monkeypatch)
    data = await graph_series.build_series_commands()
    buf = await graph_series.render_combined(
        [data], graph_series.GROUP_COUNTS, "Count", "Test Single Commands",
    )
    assert buf.getbuffer().nbytes > 0


@needs_matplotlib
async def test_render_combined_counts_grouped_stacked(monkeypatch):
    """Combine commands (stacked) + server (line-native) + ai (bar-native)
    in counts group → grouped bars, each internally stacked from segments.
    """
    await _seed_history(monkeypatch)
    cmds = await graph_series.build_series_commands()
    srv = await graph_series.build_series_server()
    ai = await graph_series.build_series_ai()
    buf = await graph_series.render_combined(
        [cmds, srv, ai], graph_series.GROUP_COUNTS, "Count", "Test Combined Counts",
    )
    assert buf.getbuffer().nbytes > 0


async def test_build_series_ping_skips_unmeasured_and_appends_live(monkeypatch):
    """Buckets without a ping_ms value (pre-migration rows) are skipped, and
    the live now-point comes from bot.latency in ms."""
    await _seed_history(monkeypatch)
    bot = SimpleNamespace(latency=0.042)
    data = await graph_series.build_series_ping(bot)
    assert [seg.label for seg in data.segments] == ["Gateway Ping"]
    # yesterday's 45.0 survives, today's unmeasured bucket is skipped, and
    # the live 42ms point is appended.
    assert data.segments[0].y_values == [45.0, pytest.approx(42.0)]
    assert len(data.x_points) == 2


async def test_build_series_ping_nan_latency_skips_live_point(monkeypatch):
    """Before the first heartbeat ack bot.latency is nan — no live point."""
    await _seed_history(monkeypatch)
    bot = SimpleNamespace(latency=float("nan"))
    data = await graph_series.build_series_ping(bot)
    assert data.segments[0].y_values == [45.0]
    assert len(data.x_points) == 1


async def test_parse_rejects_ping_with_memory(patch_member_converter):
    ctx = _stub_ctx()
    parsed = await graph_series.parse_tokens(ctx, ("ping", "memory"))
    assert parsed.error is not None
    assert "incompatible" in parsed.error


@needs_matplotlib
async def test_render_single_ping(monkeypatch):
    await _seed_history(monkeypatch)
    data = await graph_series.build_series_ping(SimpleNamespace(latency=0.05))
    buf = await graph_series.render_combined(
        [data], graph_series.GROUP_MS, "ms", "Test Single Ping",
    )
    assert buf.getbuffer().nbytes > 0


@needs_matplotlib
async def test_render_ai_uptime_strip_single_mode(monkeypatch):
    await _seed_history(monkeypatch)
    data = await graph_series.build_series_ai()
    buf = graph_series.render_ai_uptime_strip(data)
    assert buf.getbuffer().nbytes > 0
