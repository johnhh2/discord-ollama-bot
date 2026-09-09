"""Artifact catalog and effect helpers.

Artifacts are permanent, nameless per-user purchases — they're listed by
their effect only (see !artifacts). Ownership lives in state.user_artifacts
({uid: {artifact_id: quantity}}, source of truth: user_artifacts table);
first-purchase times in state.user_artifact_acquired_at ({uid: {artifact_id:
unix ts}}, the table's acquired_at column, absent for pre-0067 rows).

Each catalog entry:
    id      — stable key stored in the DB; never rename once shipped
    level   — display level (per-guild, see level_unlocks) required to buy
    cost    — price in coins
    effect  — the user-facing description (doubles as the artifact's "name")
    max     — how many copies one user may own
Effect payload keys (all optional) are read by the systems they modify:
    slots_blanks_removed      — ⬛ symbols removed from the buyer's slots reel
    unlocks_chessthreats      — grants access to !chessthreats
    bail_discount_pct         — % off any bail the owner pays
    extra_scratchoffs         — extra daily scratchoff tickets
    steal_boost_pct           — relative % increase to !steal success chance
    crime_catch_reduction_pct — relative % cut to being jailed after a crime
                                (steal/mug; bank heists excluded)
    scratchoffs_at_25_streak  — extra daily scratchoffs while the owner's
                                live daily streak (src/streaks.py) is 25+ days
    property_accrual_cap_bonus        — flat increase to the unredeemed
                                property-revenue cap (src/properties.py)
    savings_rate_boost        — the owner's savings accrue at
                                ARTIFACT_SAVINGS_DAILY_MULT instead of
                                SAVINGS_DAILY_MULT from the purchase instant
                                on (src/economy.py savings_growth)
    property_upgrade_discount_pct — % off any property upgrade the owner
                                buys; the upgrade's full catalog cost still
                                folds into the deed's value
    property_revenue_pct_per_property — % property-revenue boost per
                                property the owner holds
    property_revenue_pct_cap  — ceiling on the per-property boost above
"""
from src.config import (
    ARTIFACT_SLOTS_BLANK_COST, ARTIFACT_CHESSTHREATS_COST,
    ARTIFACT_BAIL_DISCOUNT_COST, ARTIFACT_EXTRA_SCRATCH_COST,
    ARTIFACT_STEAL_BOOST_COST, ARTIFACT_CRIME_CATCH_COST,
    ARTIFACT_STREAK_SCRATCH_COST,
    ARTIFACT_PROPERTY_CAP_COST, ARTIFACT_SAVINGS_BOOST_COST,
    ARTIFACT_UPGRADE_DISCOUNT_COST, ARTIFACT_PROPERTY_BOOST_COST,
    SAVINGS_DAILY_MULT, ARTIFACT_SAVINGS_DAILY_MULT,
    SLOT_REEL, SCRATCHOFF_MAX_DAILY,
)
from src import state

# Live daily streak (days) at which the streak artifact's extra ticket unlocks.
STREAK_SCRATCHOFF_MIN_STREAK = 25


def _daily_pct(mult: float) -> str:
    """1.008 -> '0.80%' — keeps the catalog copy in lockstep with the math."""
    return f"{(mult - 1.0) * 100:.2f}%"


ARTIFACTS: list[dict] = [
    {
        "id": "slots_blank_remover",
        "level": 5,
        "cost": ARTIFACT_SLOTS_BLANK_COST,
        "effect": "Removes 1× ⬛ blank symbol from your slots reel, making wins more likely",
        "max": 1,
        "slots_blanks_removed": 1,
    },
    {
        "id": "chessthreats_unlock",
        "level": 10,
        "cost": ARTIFACT_CHESSTHREATS_COST,
        "effect": "Unlocks `!chessthreats` — reveals every hanging piece in your current chess game",
        "max": 1,
        "unlocks_chessthreats": 1,
    },
    {
        "id": "bail_discount",
        "level": 15,
        "cost": ARTIFACT_BAIL_DISCOUNT_COST,
        "effect": "Any bail you pay costs 50% less",
        "max": 1,
        "bail_discount_pct": 50,
    },
    {
        "id": "extra_scratchoff",
        "level": 20,
        "cost": ARTIFACT_EXTRA_SCRATCH_COST,
        "effect": "Grants a 4th daily scratchoff ticket",
        "max": 1,
        "extra_scratchoffs": 1,
    },
    {
        "id": "steal_boost",
        "level": 25,
        "cost": ARTIFACT_STEAL_BOOST_COST,
        "effect": "Your `!steal` success chance is 25% higher",
        "max": 1,
        "steal_boost_pct": 25,
    },
    {
        "id": "crime_catch_reducer",
        "level": 30,
        "cost": ARTIFACT_CRIME_CATCH_COST,
        "effect": "You're 20% less likely to be caught in any crime (bank heists excluded)",
        "max": 1,
        "crime_catch_reduction_pct": 20,
    },
    {
        "id": "streak_scratchoffs",
        "level": 35,
        "cost": ARTIFACT_STREAK_SCRATCH_COST,
        "effect": "Grants +1 daily scratchoff while your daily streak is 25+ days",
        "max": 1,
        "scratchoffs_at_25_streak": 1,
    },
    {
        "id": "property_cap_deed",
        "level": 40,
        "cost": ARTIFACT_PROPERTY_CAP_COST,
        "effect": "Your missed-day property revenue bank holds 10,000 🪙 more",
        "max": 1,
        "property_accrual_cap_bonus": 10_000,
    },
    {
        "id": "savings_rate_boost",
        "level": 40,
        "cost": ARTIFACT_SAVINGS_BOOST_COST,
        "effect": (
            f"Your savings earn {_daily_pct(ARTIFACT_SAVINGS_DAILY_MULT)} "
            f"compound interest per day instead of {_daily_pct(SAVINGS_DAILY_MULT)}"
        ),
        "max": 1,
        "savings_rate_boost": 1,
    },
    {
        "id": "property_upgrade_discount",
        "level": 45,
        "cost": ARTIFACT_UPGRADE_DISCOUNT_COST,
        "effect": "Property upgrades cost you 20% less",
        "max": 1,
        "property_upgrade_discount_pct": 20,
    },
    {
        "id": "property_mogul",
        "level": 50,
        "cost": ARTIFACT_PROPERTY_BOOST_COST,
        "effect": "Your property revenue increases by 5% for each property you own (up to 25%)",
        "max": 1,
        "property_revenue_pct_per_property": 5,
        "property_revenue_pct_cap": 25,
    },
]


def artifacts_at_level(level: int) -> list[dict]:
    """Catalog entries that become purchasable exactly at *level* (display
    level). Used by the level-up announcement."""
    return [a for a in ARTIFACTS if a.get("level", 1) == level]


def owned_qty(uid: int, artifact_id: str) -> int:
    return state.user_artifacts.get(uid, {}).get(artifact_id, 0)


def artifact_acquired_at(uid: int, artifact_id: str) -> float | None:
    """Unix time the user first bought this artifact, or None when it isn't
    owned or the row predates migration 0067 (no timestamp recorded)."""
    if owned_qty(uid, artifact_id) <= 0:
        return None
    return state.user_artifact_acquired_at.get(uid, {}).get(artifact_id)


def owned_artifact_count(uid: int) -> int:
    """Total artifacts the user owns, summing quantities across the catalog.

    Quantity-aware rather than a plain len() of the ownership dict so a
    future stackable artifact counts once per copy. Feeds the
    "most artifacts owned" record.
    """
    return sum(int(q or 0) for q in state.user_artifacts.get(uid, {}).values())


def _owned_total(uid: int, key: str) -> int:
    """Sum a payload key across every artifact the user owns."""
    owned = state.user_artifacts.get(uid, {})
    return sum(art.get(key, 0) * owned.get(art["id"], 0) for art in ARTIFACTS)


def slots_blanks_removed(uid: int) -> int:
    """Total ⬛ symbols the user's artifacts strip from their slots reel."""
    return _owned_total(uid, "slots_blanks_removed")


def get_slot_reel(uid: int) -> list[str]:
    """The slots reel for this user with artifact effects applied."""
    reel = list(SLOT_REEL)
    for _ in range(min(slots_blanks_removed(uid), reel.count("⬛"))):
        reel.remove("⬛")
    return reel


def has_chessthreats_unlock(uid: int) -> bool:
    return _owned_total(uid, "unlocks_chessthreats") > 0


def bail_cost(uid: int, base_cost: int) -> int:
    """Bail cost for this payer after artifact discounts."""
    pct = min(_owned_total(uid, "bail_discount_pct"), 100)
    return base_cost - base_cost * pct // 100


def scratchoff_daily_cap(uid: int) -> int:
    """Daily scratchoff ticket cap for this user (base + artifact extras)."""
    cap = SCRATCHOFF_MAX_DAILY + _owned_total(uid, "extra_scratchoffs")
    at_25 = _owned_total(uid, "scratchoffs_at_25_streak")
    if at_25:
        # Local imports: src.streaks/src.economy pull in the persistence
        # layer, which this module must not require at import time.
        from src.streaks import get_command_streak_entry, effective_streak
        from src.economy import _ct_today
        streak = effective_streak(get_command_streak_entry(str(uid)), _ct_today())
        if streak >= STREAK_SCRATCHOFF_MIN_STREAK:
            cap += at_25
    return cap


def steal_success_chance(uid: int, base: float) -> float:
    """!steal escape chance for this thief after artifact boosts."""
    return min(1.0, base * (1 + _owned_total(uid, "steal_boost_pct") / 100))


def crime_catch_chance(uid: int, base: float) -> float:
    """Chance of being jailed after a failed steal / a mug, after artifact
    reductions. Bank heists deliberately don't call this."""
    pct = min(_owned_total(uid, "crime_catch_reduction_pct"), 100)
    return base * (1 - pct / 100)


def savings_boost_since(uid: int) -> float | None:
    """Unix time from which the user's savings accrue at the boosted rate, or
    None for no boost. Deliberately None when the artifact row carries no
    acquisition time: without a boundary the boost could only be applied to
    the deposit's whole history, which is exactly the retroactive re-pricing
    the timestamp exists to prevent."""
    if _owned_total(uid, "savings_rate_boost") <= 0:
        return None
    since = [
        artifact_acquired_at(uid, art["id"])
        for art in ARTIFACTS if art.get("savings_rate_boost")
    ]
    known = [ts for ts in since if ts is not None]
    return min(known) if known else None


def property_upgrade_cost(uid: int, base_cost: int) -> int:
    """What this buyer pays for a property upgrade after artifact discounts.
    Only the charge shrinks — the full catalog cost still folds into the
    deed's value (src/properties.py property_value)."""
    pct = min(_owned_total(uid, "property_upgrade_discount_pct"), 100)
    return base_cost - base_cost * pct // 100


def property_accrual_cap_bonus(uid: int) -> int:
    """Flat coins added to the unredeemed property-revenue cap."""
    return _owned_total(uid, "property_accrual_cap_bonus")


def property_revenue_pct(uid: int, owned_count: int) -> int:
    """The artifact % boost on property revenue for a holder of
    `owned_count` deeds: +5% per property, capped at +25% (the cap lands at
    PROPERTY_MAX_OWNED, so a holder at the ownership limit sees the full
    boost and nothing past it)."""
    pct = _owned_total(uid, "property_revenue_pct_per_property") * owned_count
    cap = _owned_total(uid, "property_revenue_pct_cap")
    if cap:
        pct = min(pct, cap)
    return pct


def property_revenue_boosted(uid: int, base: int, owned_count: int) -> int:
    """Property revenue after artifact boosts (see property_revenue_pct)."""
    return int(base * (1 + property_revenue_pct(uid, owned_count) / 100))
