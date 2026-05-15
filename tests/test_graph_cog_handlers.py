"""End-to-end tests for the GraphCog handlers — the layer above the
build_series_*/render_combined unit tests.

These pin the user-visible fallback paths in `_build_and_render` that the
unit tests miss because they call the build/render functions in isolation:

  - No history at all → "📊 No Data" embed.
  - Only one data point → "📊 Not Enough Data" embed.
  - parse_tokens error → "📊 Invalid Combination" embed.

The original `s.x_dates` regression after the bucket refactor would have
been caught by `test_no_data_fallback_uses_x_points` — `_build_and_render`
references SeriesData attributes by name, and any rename without a
matching cog update breaks the no-data path silently.
"""
from types import SimpleNamespace

import pytest

import src.state as _state
import src.economy as _economy
from src import graph_series
from src.cogs.graph_cog import _build_and_render


pytestmark = pytest.mark.asyncio


def _stub_member(uid: int = 1, name: str = "tester", guild_id: int = 42):
    m = SimpleNamespace()
    m.id = uid
    m.display_name = name
    # build_series_crime/gambling read member.guild.id (guild-scoped since
    # migration 0018) — give the stub a guild so those paths don't AttributeError.
    m.guild = SimpleNamespace(id=guild_id)
    return m


def _stub_ctx(author: SimpleNamespace | None = None, guild_id: int = 42):
    """Minimal ctx for _build_and_render — has author, guild, message, send.
    Records sent embeds on ctx.sent_embeds for assertion. The cog sends a
    "Rendering…" placeholder first and then `placeholder.edit(...)` with the
    final embed or file, so the stubbed send returns a Message-like object
    whose edit() forwards into the same lists. The placeholder embed itself
    is recorded in ctx.placeholder_embeds, separate from the final embeds
    in ctx.sent_embeds, to keep assertions about the *result* clean.
    """
    ctx = SimpleNamespace()
    ctx.author = author or _stub_member()
    ctx.bot = SimpleNamespace()
    ctx.guild = SimpleNamespace(
        id=guild_id,
        get_member=lambda _id: _stub_member(_id, f"u{_id}"),
    )
    ctx.message = SimpleNamespace(mentions=[])
    ctx.command = SimpleNamespace(qualified_name="graph economy")
    ctx.sent_embeds = []
    ctx.sent_files = []
    ctx.placeholder_embeds = []

    class _StubMessage:
        async def edit(self, *, embed=None, attachments=None, **kwargs):
            if embed is not None:
                ctx.sent_embeds.append(embed)
            if attachments:
                ctx.sent_files.extend(attachments)

    async def _send(content=None, *, embed=None, file=None, **kwargs):
        # Direct ctx.send embeds (parse-error path) go straight to sent_embeds.
        # Placeholder embeds (the "Rendering…" message) get separated out so
        # tests can ignore them.
        if embed is not None:
            title = embed.title or ""
            if "Rendering" in title:
                ctx.placeholder_embeds.append(embed)
            else:
                ctx.sent_embeds.append(embed)
        if file is not None:
            ctx.sent_files.append(file)
        return _StubMessage()

    ctx.send = _send
    return ctx


@pytest.fixture
def patch_member_converter(monkeypatch):
    """Resolve numeric tokens as members so parse_tokens can complete."""
    from discord.ext.commands import MemberConverter, BadArgument

    async def _convert(self, ctx, argument):
        if argument.isdigit():
            return _stub_member(int(argument), f"u{argument}")
        raise BadArgument(argument)

    monkeypatch.setattr(MemberConverter, "convert", _convert)


# ── No-data and not-enough-data fallbacks ────────────────────────────────────


async def test_empty_history_for_economy_series_uses_x_points(monkeypatch, patch_member_converter):
    """REGRESSION: This is the exact scenario from the prod crash —
    `!graph economy` invoked when balance_history is empty. Before the
    bucket refactor, `_build_and_render` referenced `s.x_dates`, which
    AttributeError'd here because SeriesData was renamed to `x_points`.

    With empty history, build_series_economy still appends a live "now"
    point (sums in-memory wallets), so x_points has length 1 → the
    'Not Enough Data' guard fires, not 'No Data'. Either way the cog must
    NOT crash with AttributeError; the relevant invariant is that the
    attribute access at line ~50 succeeds and the user gets a friendly
    fallback embed.
    """
    async def _empty(): return {}
    monkeypatch.setattr(graph_series, "load_balance_history", _empty)

    ctx = _stub_ctx()
    # The bug fired at attribute-access time. If `s.x_points` is the wrong
    # name, this line AttributeErrors before any guard logic runs.
    await _build_and_render(ctx, tokens=(), entry_spec=graph_series.find_spec("economy"))

    # Either fallback is acceptable — we're pinning that the attribute
    # access works and a fallback embed is delivered (no file).
    assert len(ctx.sent_embeds) == 1
    assert ctx.sent_files == []
    title = ctx.sent_embeds[0].title or ""
    assert any(label in title for label in ("No Data", "Not Enough Data"))


async def test_no_data_fallback_for_crime_with_no_history(monkeypatch, patch_member_converter):
    """Crime/gambling/levels skip the live append when there's no in-memory
    entry for the user — so with empty history, x_points really IS empty.
    This test exercises the genuine `not s.x_points` branch."""
    async def _empty(): return {}
    monkeypatch.setattr(graph_series, "load_crime_history", _empty)

    _state.crime_today_by_user.clear()  # no live entry for any user

    ctx = _stub_ctx()
    await _build_and_render(ctx, tokens=(), entry_spec=graph_series.find_spec("crime"))

    assert len(ctx.sent_embeds) == 1
    title = ctx.sent_embeds[0].title or ""
    assert "No Data" in title


async def test_not_enough_data_fallback(monkeypatch, patch_member_converter):
    """One data point on disk, no live append → 'Not Enough Data' embed.

    Use commands (no live append from in-memory state when the dict is
    empty), and seed exactly one (date, bucket) row.
    """
    today = _economy._ct_now().date().isoformat()
    fake = {today: {0: {"GraphCog": 5}}}

    async def _load(): return fake
    monkeypatch.setattr(graph_series, "load_command_usage_history", _load)

    # Make sure the live append doesn't fire (no current-bucket activity).
    _state.stats_commands_today_by_cog = {}
    # And the bucket from the on-disk row matches today's current bucket so
    # the live append's `x_points[-1] != now_point` check stays True only
    # when we have ONE point. Force the test history's bucket to match
    # current bucket.
    cur_b = _economy._current_bucket_ct()
    fake[today] = {cur_b: {"GraphCog": 5}}

    ctx = _stub_ctx()
    await _build_and_render(ctx, tokens=(), entry_spec=graph_series.find_spec("commands"))

    assert len(ctx.sent_embeds) == 1
    title = ctx.sent_embeds[0].title or ""
    assert "Not Enough Data" in title


# ── parse_tokens error path ─────────────────────────────────────────────────


async def test_invalid_combination_sends_error_embed(monkeypatch, patch_member_converter):
    """User passes incompatible series tokens (coins + counts) → cog sends
    the 'Invalid Combination' error embed instead of crashing."""
    ctx = _stub_ctx()
    # entry=balance (coins), token=server (counts) → cross-group rejection.
    await _build_and_render(
        ctx, tokens=("server",), entry_spec=graph_series.find_spec("balance"),
    )

    assert len(ctx.sent_embeds) == 1
    title = ctx.sent_embeds[0].title or ""
    assert "Invalid Combination" in title
    desc = ctx.sent_embeds[0].description or ""
    assert "incompatible" in desc.lower()


async def test_unknown_token_sends_error_embed(monkeypatch, patch_member_converter):
    """User mistypes a series name → 'Invalid Combination' embed."""
    ctx = _stub_ctx()
    await _build_and_render(
        ctx, tokens=("nonsense",), entry_spec=graph_series.find_spec("balance"),
    )

    assert len(ctx.sent_embeds) == 1
    desc = ctx.sent_embeds[0].description or ""
    assert "unknown token" in desc.lower()


async def test_levels_in_dm_sends_error_embed(monkeypatch, patch_member_converter):
    """levels series requires ctx.guild — in a DM it should reject cleanly."""
    ctx = _stub_ctx()
    ctx.guild = None  # DM context

    await _build_and_render(
        ctx, tokens=(), entry_spec=graph_series.find_spec("levels"),
    )

    assert len(ctx.sent_embeds) == 1
    desc = ctx.sent_embeds[0].description or ""
    assert "per-server" in desc.lower() or "dm" in desc.lower()


# ── Successful path renders a file ──────────────────────────────────────────


async def test_successful_render_sends_file_not_embed(monkeypatch, patch_member_converter):
    """When data is present, the cog sends a discord.File (the PNG), not an
    error embed. This pins the happy-path glue between parse → build →
    render → send."""
    pytest.importorskip("matplotlib")

    today = _economy._ct_now().date().isoformat()
    yest = _economy._ct_now() - __import__("datetime").timedelta(days=1)
    yest_iso = yest.date().isoformat()
    fake = {
        yest_iso: {0: {"GraphCog": 3, "EconomyCog": 7}},
        today: {0: {"GraphCog": 5, "EconomyCog": 2}},
    }
    async def _load(): return fake
    monkeypatch.setattr(graph_series, "load_command_usage_history", _load)

    ctx = _stub_ctx()
    await _build_and_render(ctx, tokens=(), entry_spec=graph_series.find_spec("commands"))

    assert ctx.sent_files, "expected a discord.File attachment, got embeds: " + repr(
        [(e.title, e.description) for e in ctx.sent_embeds]
    )
    # The file should be a PNG with non-empty content.
    file = ctx.sent_files[0]
    assert file.filename.endswith(".png")
