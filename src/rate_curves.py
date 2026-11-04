"""Interest curves: savings and property rates that start high and fall.

One curve for the whole bot, because balances and deeds have no guild
dimension. `!settings interest` picks a mode and optionally how far into it
to start (`state.bot_settings`). The rate falls linearly from its start to
its end, then holds the end rate. With no mode set, the flat rates apply.
See CLAUDE.md: Interest curves.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from functools import lru_cache

from src import state
from src.config import ARTIFACT_SAVINGS_DAILY_MULT, SAVINGS_DAILY_MULT
from src.properties import PROPERTY_DAILY_REVENUE_PERMILLE

DAY_SECS = 86400.0
FLAT_PCT = {
    "savings": (SAVINGS_DAILY_MULT - 1.0) * 100,
    "property": PROPERTY_DAILY_REVENUE_PERMILLE / 10,
}
# A curve has no fixed "boosted" rate, so the savings artifact keeps its
# flat-rate proportion (0.8% over 0.6%) at every point on it.
ARTIFACT_SAVINGS_FACTOR = (ARTIFACT_SAVINGS_DAILY_MULT - 1.0) / (SAVINGS_DAILY_MULT - 1.0)

OFF = "off"
# bot_settings is a TEXT column, so both are stored as strings.
MODE_KEY = "rate_curve_mode"
STARTED_KEY = "rate_curve_started_at"


@dataclass(frozen=True)
class RateCurve:
    key: str
    label: str              # the menu's wording, player range included
    days: int               # start rate to end rate
    savings: tuple[float, float]    # % a day, start and end
    property: tuple[float, float]

    def pct(self, kind: str, elapsed_days: float) -> float:
        start, end = getattr(self, kind)
        return start + (end - start) * min(max(elapsed_days, 0.0) / self.days, 1.0)


RATE_CURVES: dict[str, RateCurve] = {
    "small": RateCurve("small", "Small (2-9 players)", 730, (1.0, 0.3), (2.5, 0.5)),
    "medium": RateCurve("medium", "Medium (10-99 players)", 365, (0.8, 0.2), (2.0, 0.4)),
    "large": RateCurve("large", "Large (100+ players)", 182, (0.6, 0.1), (1.5, 0.3)),
}

CURVE_MENU = "\n".join(
    f"**{c.label}** — savings {c.savings[0]:g}% → {c.savings[1]:g}%, "
    f"property {c.property[0]:g}% → {c.property[1]:g}% a day, over {c.days} days"
    for c in RATE_CURVES.values()
)
CURVE_QUESTION = (
    "Should savings interest and property revenue start high and fall over time? "
    "This applies to every server. The clock starts at day 0 and keeps running through any change, "
    "unless you name a position: a day or a percent of the way in, like `!settings interest medium 75%`.\n\n"
    f"{CURVE_MENU}\n"
    f"**Flat rates** — savings {FLAT_PCT['savings']:g}% and property {FLAT_PCT['property']:g}% a day, for good."
)

# (started_at, mode), hashable so the integration table can be cached.
Curve = tuple[float, str]


def active_curve() -> Curve | None:
    mode = state.bot_settings.get(MODE_KEY)
    started_at = state.bot_settings.get(STARTED_KEY)
    if mode not in RATE_CURVES or not started_at:
        return None
    return float(started_at), mode


def parse_position(text: str, curve: RateCurve) -> float | None:
    """Days into `curve` from `274` or `75%`, or None if it isn't on the curve."""
    text = text.strip()
    try:
        days = float(text[:-1]) / 100 * curve.days if text.endswith("%") else float(text)
    except ValueError:
        return None
    return days if 0 <= days <= curve.days else None


def rate_at(curve: Curve, kind: str, ts: float) -> float:
    """% a day on `curve` at `ts`. Before the curve started it's flat."""
    started_at, mode = curve
    if ts < started_at:
        return FLAT_PCT[kind]
    return RATE_CURVES[mode].pct(kind, (ts - started_at) / DAY_SECS)


@lru_cache(maxsize=16)
def _savings_table(curve: Curve, boosted: bool) -> tuple[list[float], float, float]:
    """(cumulative log-growth per whole day from the start, flat log rate, end
    log rate). The leaderboard reads every deposit, so the integral of the
    falling rate is a lookup, not a walk over up to two years of days."""
    factor = ARTIFACT_SAVINGS_FACTOR if boosted else 1.0

    def log_rate(ts: float) -> float:
        return math.log1p(rate_at(curve, "savings", ts) * factor / 100)

    started_at, mode = curve
    horizon = RATE_CURVES[mode].days
    cumulative = [0.0]
    for day in range(horizon):
        cumulative.append(cumulative[-1] + log_rate(started_at + (day + 0.5) * DAY_SECS))
    return cumulative, math.log1p(FLAT_PCT["savings"] * factor / 100), log_rate(started_at + horizon * DAY_SECS)


def _log_growth_to(curve: Curve, boosted: bool, ts: float) -> float:
    cumulative, flat, end = _savings_table(curve, boosted)
    days = (ts - curve[0]) / DAY_SECS
    if days <= 0:
        return days * flat
    horizon = len(cumulative) - 1
    if days >= horizon:
        return cumulative[-1] + (days - horizon) * end
    whole = int(days)
    return cumulative[whole] + (days - whole) * (cumulative[whole + 1] - cumulative[whole])


def curve_growth(curve: Curve, start: float, end: float, boosted: bool = False) -> float:
    """Savings growth factor from `start` to `end` on `curve`."""
    if end <= start:
        return 1.0
    return math.exp(_log_growth_to(curve, boosted, end) - _log_growth_to(curve, boosted, start))


def property_mult(curve: Curve | None, ts: float) -> float:
    """What to multiply a deed's flat revenue by at `ts`."""
    if curve is None:
        return 1.0
    return rate_at(curve, "property", ts) / FLAT_PCT["property"]


def _pct_now(kind: str, now: float | None) -> float:
    curve = active_curve()
    if curve is None:
        return FLAT_PCT[kind]
    return rate_at(curve, kind, time.time() if now is None else now)


def savings_pct_text(boosted: bool = False, now: float | None = None) -> str:
    """Today's savings rate, for display. Matches SAVINGS_DAILY_PCT's format."""
    return f"{_pct_now('savings', now) * (ARTIFACT_SAVINGS_FACTOR if boosted else 1.0):.2f}%"


def property_pct_text(now: float | None = None) -> str:
    """Today's property rate, for display. Matches PROPERTY_DAILY_REVENUE_PCT's format."""
    return f"{_pct_now('property', now):.2f}".rstrip("0").rstrip(".") + "%"


def curve_status() -> str:
    """One line for the settings overview."""
    curve = active_curve()
    if curve is None:
        return "flat rates"
    rates = RATE_CURVES[curve[1]]
    day = min((time.time() - curve[0]) / DAY_SECS, rates.days)
    return f"{rates.label}, day {day:.0f} of {rates.days}: savings {savings_pct_text()}, property {property_pct_text()} a day"
