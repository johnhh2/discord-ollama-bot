"""Artifact catalog and effect helpers.

Artifacts are permanent, nameless per-user purchases — they're listed by
their effect only (see !artifacts). Ownership lives in state.user_artifacts
({uid: {artifact_id: quantity}}, source of truth: user_artifacts table).

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
    scratchoffs_per_50_streak — extra daily scratchoffs per 50 days of the
                                owner's live command streak (src/streaks.py)
    property_accrual_cap_bonus        — flat increase to the unredeemed
                                property-revenue cap (src/properties.py)
    property_revenue_pct_per_property — % property-revenue boost per
                                property the owner holds
"""
from src.config import (
    ARTIFACT_SLOTS_BLANK_COST, ARTIFACT_CHESSTHREATS_COST,
    ARTIFACT_BAIL_DISCOUNT_COST, ARTIFACT_EXTRA_SCRATCH_COST,
    ARTIFACT_STEAL_BOOST_COST, ARTIFACT_CRIME_CATCH_COST,
    ARTIFACT_STREAK_SCRATCH_COST,
    ARTIFACT_PROPERTY_CAP_COST, ARTIFACT_PROPERTY_BOOST_COST,
    SLOT_REEL, SCRATCHOFF_MAX_DAILY,
)
from src import state

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
        "effect": "Grants +1 daily scratchoff for every 50 days in your daily command streak",
        "max": 1,
        "scratchoffs_per_50_streak": 1,
    },
    {
        "id": "property_cap_deed",
        "level": 40,
        "cost": ARTIFACT_PROPERTY_CAP_COST,
        "effect": "Your unredeemed property revenue cap is 5,000 🪙 higher",
        "max": 1,
        "property_accrual_cap_bonus": 5_000,
    },
    {
        "id": "property_mogul",
        "level": 50,
        "cost": ARTIFACT_PROPERTY_BOOST_COST,
        "effect": "Your property revenue is 5% higher for every property you own",
        "max": 1,
        "property_revenue_pct_per_property": 5,
    },
]


def artifacts_at_level(level: int) -> list[dict]:
    """Catalog entries that become purchasable exactly at *level* (display
    level). Used by the level-up announcement."""
    return [a for a in ARTIFACTS if a.get("level", 1) == level]


def owned_qty(uid: int, artifact_id: str) -> int:
    return state.user_artifacts.get(uid, {}).get(artifact_id, 0)


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
    per_50 = _owned_total(uid, "scratchoffs_per_50_streak")
    if per_50:
        # Local imports: src.streaks/src.economy pull in the persistence
        # layer, which this module must not require at import time.
        from src.streaks import get_command_streak_entry, effective_streak
        from src.economy import _ct_today
        streak = effective_streak(get_command_streak_entry(str(uid)), _ct_today())
        cap += per_50 * (streak // 50)
    return cap


def steal_success_chance(uid: int, base: float) -> float:
    """!steal escape chance for this thief after artifact boosts."""
    return min(1.0, base * (1 + _owned_total(uid, "steal_boost_pct") / 100))


def crime_catch_chance(uid: int, base: float) -> float:
    """Chance of being jailed after a failed steal / a mug, after artifact
    reductions. Bank heists deliberately don't call this."""
    pct = min(_owned_total(uid, "crime_catch_reduction_pct"), 100)
    return base * (1 - pct / 100)


def property_accrual_cap_bonus(uid: int) -> int:
    """Flat coins added to the unredeemed property-revenue cap."""
    return _owned_total(uid, "property_accrual_cap_bonus")


def property_revenue_boosted(uid: int, base: int, owned_count: int) -> int:
    """Property revenue after artifact boosts. The per-property bonus scales
    with holdings: +10%/property × 5 properties = +50% at the ownership cap."""
    pct = _owned_total(uid, "property_revenue_pct_per_property") * owned_count
    return int(base * (1 + pct / 100))
