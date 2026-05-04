"""Graph-series registry powering combinable `!graph` subcommands.

Each subcommand (`balance`, `economy`, `commands`, `server`, `ai`, `memory`)
contributes a `SeriesSpec` declaring its aliases, group, and a build function
that returns the standardized `SeriesData` shape. The combiner picks rendering
style from (number-of-series, group):

  - 1 series, native style "line"  → overlaid lines (preserves multi-segment).
  - 1 series, native style "bar"   → stacked bar (segments stack within each bar).
  - N series, group "coins"        → overlaid lines (all segments).
  - N series, group "counts"       → grouped bars per day, each bar internally
                                     stacked from its segments.

`memory` is its own group of size 1; combination with anything else is rejected.

Adding a new graph series: write a `build_series_xxx()` async function returning
`SeriesData`, then append a `SeriesSpec(...)` entry to `REGISTRY` below.
"""
from __future__ import annotations

import datetime
import time as _time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import discord

from src import state
from src.economy import _ct_today_date, get_balance
from src.helpers import get_memory_mb
from src.persistence import (
    load_balance_history, load_bot_stats_history, load_command_usage_history,
)


# ── Group identifiers ────────────────────────────────────────────────────────

GROUP_COINS = "coins"
GROUP_COUNTS = "counts"
GROUP_MB = "mb"

_GROUP_LABEL = {
    GROUP_COINS: "coins",
    GROUP_COUNTS: "counts",
    GROUP_MB: "MB",
}


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass
class Segment:
    """One coloured slice within a series (a single bar segment, or one line)."""
    label: str
    color: str
    y_values: list[float]


@dataclass
class SeriesData:
    """Result of building one series. Multi-segment when the source produces
    more than one stripe (e.g. economy = wallets/savings/total)."""
    title: str            # short identifier for legend prefixing in combined mode
    segments: list[Segment]
    x_dates: list[datetime.date]
    native_style: str     # "line" or "bar"
    # Optional extras only used by ai in single-mode:
    extras: dict = field(default_factory=dict)


# A build function may take an optional discord.Member (only `balance` uses it).
BuildFn = Callable[..., Awaitable[SeriesData]]


@dataclass
class SeriesSpec:
    name: str
    aliases: tuple[str, ...]
    group: str
    build: BuildFn
    accepts_member: bool = False
    y_unit_label: str = ""        # e.g. "🪙 Coins", "Count", "MB"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sorted_dates(history: dict, limit: int = 14) -> list[str]:
    return sorted(history.keys())[-limit:]


def _date_to_iso(d: datetime.date) -> str:
    return d.isoformat()


# ── build_series for each registered command ─────────────────────────────────


async def build_series_balance(member: discord.Member) -> SeriesData:
    history = await load_balance_history()
    dates = _sorted_dates(history)
    uid_str = str(member.id)

    x_dates: list[datetime.date] = []
    y_wallet: list[float] = []
    for d in dates:
        snap = history[d].get(uid_str)
        if snap is not None:
            x_dates.append(datetime.date.fromisoformat(d))
            y_wallet.append(snap["wallet"])

    today = _ct_today_date()
    live_wallet = await get_balance(member.id)
    if not x_dates or x_dates[-1] != today:
        x_dates.append(today)
        y_wallet.append(live_wallet)

    return SeriesData(
        title=f"{member.display_name}'s Wallet",
        segments=[Segment(label="Wallet", color="#2ecc71", y_values=y_wallet)],
        x_dates=x_dates,
        native_style="line",
    )


async def build_series_economy() -> SeriesData:
    history = await load_balance_history()
    dates = _sorted_dates(history)

    x_dates: list[datetime.date] = []
    y_wallet: list[float] = []
    y_savings: list[float] = []
    for d in dates:
        snap = history[d]
        x_dates.append(datetime.date.fromisoformat(d))
        y_wallet.append(sum(u["wallet"] for u in snap.values()))
        y_savings.append(sum(u["savings"] for u in snap.values()))

    today = _ct_today_date()
    now = _time.time()
    live_wallet = sum(u.get("balance", 0) for u in state.economy["users"].values())
    live_savings = int(sum(
        e["amount"] * (1.01 ** ((now - e["deposited_at"]) / 86400.0))
        for u in state.economy["users"].values()
        for e in u.get("savings", [])
    ))
    if not x_dates or x_dates[-1] != today:
        x_dates.append(today)
        y_wallet.append(live_wallet)
        y_savings.append(live_savings)

    y_total = [w + s for w, s in zip(y_wallet, y_savings)]
    return SeriesData(
        title="Total Economy",
        segments=[
            Segment(label="Wallets", color="#2ecc71", y_values=y_wallet),
            Segment(label="Savings", color="#9b59b6", y_values=y_savings),
            Segment(label="Total",   color="#f1c40f", y_values=y_total),
        ],
        x_dates=x_dates,
        native_style="line",
    )


async def build_series_commands() -> SeriesData:
    history = await load_command_usage_history()
    dates = _sorted_dates(history)
    today = _ct_today_date()
    live_today = dict(state.stats_commands_today_by_cog)

    if dates and dates[-1] == today.isoformat():
        x_dates = [datetime.date.fromisoformat(d) for d in dates]
        per_day = [history[d] for d in dates]
    else:
        x_dates = [datetime.date.fromisoformat(d) for d in dates] + [today]
        per_day = [history[d] for d in dates] + [live_today]

    # Union of cog names across the window, sorted by total volume desc.
    totals: dict[str, int] = {}
    for day in per_day:
        for cog, count in day.items():
            totals[cog] = totals.get(cog, 0) + count
    cogs = sorted(totals.keys(), key=lambda c: totals[c], reverse=True)

    def _label(cog_name: str) -> str:
        return cog_name[:-3] if cog_name.endswith("Cog") else cog_name

    # tab20 palette so even ~11 cogs stay distinguishable.
    import matplotlib.pyplot as _plt
    palette = _plt.cm.tab20.colors

    segments = [
        Segment(
            label=_label(cog),
            color=palette[i % len(palette)],
            y_values=[float(day.get(cog, 0)) for day in per_day],
        )
        for i, cog in enumerate(cogs)
    ]
    return SeriesData(
        title="Commands by Cog",
        segments=segments,
        x_dates=x_dates,
        native_style="bar",
    )


async def build_series_server() -> SeriesData:
    history = await load_bot_stats_history()
    dates = _sorted_dates(history)
    x_dates: list[datetime.date] = []
    y_messages: list[float] = []
    y_commands: list[float] = []
    for d in dates:
        snap = history[d]
        x_dates.append(datetime.date.fromisoformat(d))
        y_messages.append(snap.get("messages", 0))
        y_commands.append(snap.get("commands", 0))

    today = _ct_today_date()
    if not x_dates or x_dates[-1] != today:
        x_dates.append(today)
        y_messages.append(state.stats_messages_today)
        y_commands.append(state.stats_commands_today)

    return SeriesData(
        title="Server Activity",
        segments=[
            Segment(label="Messages", color="#3498db", y_values=y_messages),
            Segment(label="Commands", color="#e67e22", y_values=y_commands),
        ],
        x_dates=x_dates,
        native_style="line",
    )


async def build_series_ai() -> SeriesData:
    history = await load_bot_stats_history()
    dates = _sorted_dates(history)
    x_dates: list[datetime.date] = []
    y_responses: list[float] = []
    ai_up_flags: list[Optional[bool]] = []
    for d in dates:
        snap = history[d]
        x_dates.append(datetime.date.fromisoformat(d))
        y_responses.append(snap.get("ai_responses", 0))
        ai_up_flags.append(snap.get("ai_up", False))

    today = _ct_today_date()
    if not x_dates or x_dates[-1] != today:
        x_dates.append(today)
        y_responses.append(state.stats_ai_responses_today)
        ai_up_flags.append(None)

    return SeriesData(
        title="AI Activity",
        segments=[Segment(label="AI Responses", color="#1abc9c", y_values=y_responses)],
        x_dates=x_dates,
        native_style="bar",
        extras={"ai_up_flags": ai_up_flags},
    )


async def build_series_memory() -> SeriesData:
    history = await load_bot_stats_history()
    dates = _sorted_dates(history)
    x_dates: list[datetime.date] = []
    y_mem: list[float] = []
    for d in dates:
        mb = history[d].get("memory_mb", 0)
        if mb > 0:
            x_dates.append(datetime.date.fromisoformat(d))
            y_mem.append(mb)

    today = _ct_today_date()
    live_mem = get_memory_mb()
    if live_mem > 0 and (not x_dates or x_dates[-1] != today):
        x_dates.append(today)
        y_mem.append(live_mem)

    return SeriesData(
        title="Bot Memory",
        segments=[Segment(label="RSS Memory", color="#e74c3c", y_values=y_mem)],
        x_dates=x_dates,
        native_style="line",
    )


# ── Registry ─────────────────────────────────────────────────────────────────

REGISTRY: list[SeriesSpec] = [
    SeriesSpec("balance",  ("balance", "bal"),                          GROUP_COINS,  build_series_balance,  accepts_member=True,  y_unit_label="🪙 Coins"),
    SeriesSpec("economy",  ("economy", "eco", "total", "totalbalance"), GROUP_COINS,  build_series_economy,                          y_unit_label="🪙 Coins"),
    SeriesSpec("commands", ("commands", "cmd", "cmds"),                 GROUP_COUNTS, build_series_commands,                         y_unit_label="Count"),
    SeriesSpec("server",   ("server", "srv"),                           GROUP_COUNTS, build_series_server,                           y_unit_label="Count"),
    SeriesSpec("ai",       ("ai",),                                     GROUP_COUNTS, build_series_ai,                               y_unit_label="Count"),
    SeriesSpec("memory",   ("memory", "mem", "ram"),                    GROUP_MB,     build_series_memory,                           y_unit_label="MB"),
]


def find_spec(token: str) -> Optional[SeriesSpec]:
    """Resolve a token to a series spec, or None if it's not a known alias."""
    t = token.lower()
    for spec in REGISTRY:
        if t in spec.aliases:
            return spec
    return None


# ── Token parsing ────────────────────────────────────────────────────────────


@dataclass
class ParseResult:
    specs: list[SeriesSpec]
    member: Optional[discord.Member]
    error: Optional[str] = None


async def parse_tokens(
    ctx, tokens: tuple[str, ...]
) -> ParseResult:
    """Walk free-form tokens; classify each as series alias OR member mention.

    Member-mention tokens are resolved via discord.py's `MemberConverter`. Any
    token that is neither a known series alias nor a resolvable member is
    rejected with a clear error.

    Validates: at least one series, all in same group, no duplicates.
    """
    from discord.ext import commands as _cmds

    specs: list[SeriesSpec] = []
    seen_names: set[str] = set()
    member: Optional[discord.Member] = None
    converter = _cmds.MemberConverter()

    for tok in tokens:
        spec = find_spec(tok)
        if spec is not None:
            if spec.name in seen_names:
                return ParseResult([], None, f"duplicate series: `{spec.name}`")
            specs.append(spec)
            seen_names.add(spec.name)
            continue
        # Not a series alias — try to resolve as a member.
        try:
            resolved = await converter.convert(ctx, tok)
        except _cmds.BadArgument:
            return ParseResult(
                [], None,
                f"unknown token `{tok}` — expected a graph name or @user mention",
            )
        if member is not None and resolved.id != member.id:
            return ParseResult([], None, "more than one user mention provided")
        member = resolved

    if not specs:
        return ParseResult([], None, "no graph specified")

    groups = {s.group for s in specs}
    if len(groups) > 1:
        names = ", ".join(s.name for s in specs)
        readable = " + ".join(_GROUP_LABEL[g] for g in sorted(groups))
        return ParseResult(
            [], None,
            f"cannot combine `{names}` — incompatible y-axes ({readable})",
        )

    # `balance` requires a member (defaults to invoker if none given).
    if any(s.accepts_member for s in specs) and member is None:
        member = ctx.author

    return ParseResult(specs=specs, member=member)


# ── Rendering ────────────────────────────────────────────────────────────────


def _fmt_date(d: datetime.date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _common_axes(history_list: list[SeriesData]) -> list[datetime.date]:
    """Union of x_dates across all series, sorted."""
    seen: set[datetime.date] = set()
    for s in history_list:
        seen.update(s.x_dates)
    return sorted(seen)


def _aligned_y(seg: Segment, src_x: list[datetime.date], target_x: list[datetime.date]) -> list[float]:
    """Re-index a segment's y_values onto target_x. Missing dates → 0."""
    by_date = dict(zip(src_x, seg.y_values))
    return [by_date.get(d, 0.0) for d in target_x]


async def render_combined(serieses: list[SeriesData], group: str, y_unit_label: str, title: str):
    """Render `serieses` to a PNG buffer.

    Style rules:
      - 1 series, native "line"  → overlaid lines, one per segment.
      - 1 series, native "bar"   → stacked bar.
      - N series, group coins    → all segments overlaid as lines.
      - N series, group counts   → grouped bars; each bar internally stacked
                                   from its source's segments. Legend labels
                                   prefixed with [Source].
      - MB group is always size 1.

    Returns an `io.BytesIO` ready to send. Caller closes the figure.
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    if not serieses:
        raise ValueError("render_combined requires at least one series")

    fig, ax = plt.subplots(figsize=(9, 4))
    fig.patch.set_facecolor("#2f3136")
    ax.set_facecolor("#36393f")

    n = len(serieses)
    x_dates = _common_axes(serieses)

    if n == 1:
        s = serieses[0]
        if s.native_style == "line":
            for seg in s.segments:
                y = _aligned_y(seg, s.x_dates, x_dates)
                style = "--" if seg.label == "Total" else "-"
                marker = "s" if seg.label == "Total" else "o"
                ax.plot(x_dates, y, color=seg.color, linewidth=2, marker=marker,
                        markersize=4, linestyle=style, label=seg.label)
                if seg.label != "Total":
                    ax.fill_between(x_dates, y, alpha=0.10, color=seg.color)
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_date(mdates.num2date(v).date())))
            ax.xaxis.set_major_locator(mdates.DayLocator())
            fig.autofmt_xdate(rotation=35, ha="right")
        else:  # native_style == "bar" → stacked bar
            x_nums = list(range(len(x_dates)))
            bottoms = [0.0] * len(x_dates)
            for seg in s.segments:
                y = _aligned_y(seg, s.x_dates, x_dates)
                ax.bar(x_nums, y, bottom=bottoms, color=seg.color,
                       width=0.6, label=seg.label)
                bottoms = [b + v for b, v in zip(bottoms, y)]
            ax.set_xticks(x_nums)
            ax.set_xticklabels([_fmt_date(d) for d in x_dates], rotation=35, ha="right", fontsize=8)
    else:
        if group == GROUP_COINS:
            # Multiple series, all coins → overlaid lines, prefix labels with source.
            for s in serieses:
                for seg in s.segments:
                    y = _aligned_y(seg, s.x_dates, x_dates)
                    label = f"[{s.title}] {seg.label}"
                    ax.plot(x_dates, y, color=seg.color, linewidth=2, marker="o",
                            markersize=3, label=label)
            ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_date(mdates.num2date(v).date())))
            ax.xaxis.set_major_locator(mdates.DayLocator())
            fig.autofmt_xdate(rotation=35, ha="right")
        else:
            # Multiple series, all counts → grouped stacked bars.
            x_nums = list(range(len(x_dates)))
            group_total_width = 0.8
            bar_width = group_total_width / n
            for i, s in enumerate(serieses):
                offset = (i - (n - 1) / 2) * bar_width
                bar_x = [xn + offset for xn in x_nums]
                bottoms = [0.0] * len(x_dates)
                for seg in s.segments:
                    y = _aligned_y(seg, s.x_dates, x_dates)
                    label = f"[{s.title}] {seg.label}"
                    ax.bar(bar_x, y, bottom=bottoms, color=seg.color,
                           width=bar_width * 0.95, label=label)
                    bottoms = [b + v for b, v in zip(bottoms, y)]
            ax.set_xticks(x_nums)
            ax.set_xticklabels([_fmt_date(d) for d in x_dates], rotation=35, ha="right", fontsize=8)

    if y_unit_label == "MB":
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f} MB"))
    else:
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax.tick_params(colors="#dcddde", labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor("#4f545c")
    ax.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)

    ax.set_title(title, color="#ffffff", fontsize=12, pad=10)
    ax.set_ylabel(y_unit_label, color="#b9bbbe", fontsize=9)
    ax.legend(facecolor="#2f3136", edgecolor="#4f545c", labelcolor="#dcddde",
              fontsize=8, loc="upper left")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    plt.close(fig)
    return buf


def render_ai_uptime_strip(series: SeriesData):
    """Single-mode AI render: bar chart on top, uptime green/red/grey strip below.
    Only used when `ai` is the sole series — combination drops the strip.
    """
    import io
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    flags: list = series.extras.get("ai_up_flags", [])
    x_dates = series.x_dates
    seg = series.segments[0]
    y_responses = seg.y_values

    fig, (ax_bar, ax_line) = plt.subplots(
        2, 1, figsize=(9, 6), gridspec_kw={"height_ratios": [3, 1]}
    )
    fig.patch.set_facecolor("#2f3136")
    for ax in (ax_bar, ax_line):
        ax.set_facecolor("#36393f")

    x_nums = list(range(len(x_dates)))
    bar_colors = []
    for flag in flags:
        if flag is True:
            bar_colors.append("#2ecc71")
        elif flag is False:
            bar_colors.append("#e74c3c")
        else:
            bar_colors.append("#95a5a6")
    ax_bar.bar(x_nums, y_responses, color=bar_colors, alpha=0.85, width=0.6)
    ax_bar.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
    ax_bar.set_xticks(x_nums)
    ax_bar.set_xticklabels([_fmt_date(d) for d in x_dates], rotation=35, ha="right", fontsize=8)
    ax_bar.tick_params(colors="#dcddde", labelsize=8)
    for spine in ax_bar.spines.values():
        spine.set_edgecolor("#4f545c")
    ax_bar.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)
    ax_bar.set_title("AI Activity — Last 2 Weeks", color="#ffffff", fontsize=12, pad=10)
    ax_bar.set_ylabel("Responses", color="#b9bbbe", fontsize=9)
    legend_elements = [
        Patch(facecolor="#2ecc71", label="AI up"),
        Patch(facecolor="#e74c3c", label="AI down"),
        Patch(facecolor="#95a5a6", label="Unknown"),
    ]
    ax_bar.legend(handles=legend_elements, facecolor="#2f3136", edgecolor="#4f545c",
                  labelcolor="#dcddde", fontsize=8)

    for i, flag in enumerate(flags):
        color = "#2ecc71" if flag is True else ("#e74c3c" if flag is False else "#95a5a6")
        ax_line.barh(0, 1, left=i, color=color, height=1, alpha=0.85)
    ax_line.set_xlim(0, len(x_dates))
    ax_line.set_ylim(-0.5, 0.5)
    ax_line.set_xticks([])
    ax_line.set_yticks([])
    ax_line.set_ylabel("Uptime", color="#b9bbbe", fontsize=8)
    for spine in ax_line.spines.values():
        spine.set_edgecolor("#4f545c")

    fig.tight_layout(pad=1.5)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    plt.close(fig)
    return buf
