"""Real-estate catalog and revenue math (!assets).

Every property is a UNIQUE, bot-wide deed: at most one owner across all
servers, tracked in state.property_owners ({property_id: {owner_id,
acquired_at, list_price, listed_at}}, source of truth: property_owners
table). The catalog below is the full board — 36 properties from 10k to 2m.

Economics:
  - Revenue is 1.1% of purchase price per day, derived (never hand-typed):
    daily = cost * 11 // 1000. One invariant for the whole catalog.
  - Revenue accrues per property from max(last payout, acquisition) and is
    banked automatically with the owner's daily claim (!daily or the dailies
    channel). Accrual is capped at PROPERTY_ACCRUAL_CAP_BASE (plus artifact
    bonuses) — or one full day's portfolio revenue when that's larger, so
    claiming daily always pays out in full.
  - A user owns at most PROPERTY_MAX_OWNED properties.
  - Owners can list a property for sale at any price; listings are global,
    so a deed listed in one server can be bought from any other.

Artifact hooks (see src/artifacts.py):
    property_accrual_cap_bonus       — flat increase to the accrual cap
    property_revenue_pct_per_property — % revenue boost per property owned
"""
import time

from src import state

# ── Tuning ───────────────────────────────────────────────────────────────────

PROPERTY_MAX_OWNED = 5
# Unredeemed revenue accrues up to this many coins (or one day's full
# portfolio revenue, if greater). Artifacts add to it.
PROPERTY_ACCRUAL_CAP_BASE = 10_000
# Marketplace fee on player-to-player sales, paid by the seller out of the
# sale price into the buyer's guild's house pot (burned in DMs).
PROPERTY_SALE_FEE_PCT = 5
# Revenue rate: every property pays out this ‰ (per mille) of its purchase
# price per day — 11‰ = 1.1%/day, ≈4× purchase price per year.
PROPERTY_DAILY_REVENUE_PERMILLE = 11
# User-facing rate string — keeps help copy in lockstep with the math.
PROPERTY_DAILY_REVENUE_PCT = f"{PROPERTY_DAILY_REVENUE_PERMILLE / 10:g}%"

# ── Catalog ──────────────────────────────────────────────────────────────────
# id      — stable key stored in the DB; never rename once shipped
# name    — display name (unique; !assets buy/sell match on it)
# emoji   — display emoji
# tier    — display grouping, 1..5
# level   — display level (per-guild, see level_unlocks) required to buy
# cost    — bank price in coins; also drives daily revenue (2×/year)

PROPERTIES: list[dict] = [
    # Tier 1
    {"id": "lemonade_stand",   "name": "Lemonade Stand",   "emoji": "🍋", "tier": 1, "level": 5,  "cost": 10_000},
    {"id": "hot_dog_cart",     "name": "Hot Dog Cart",     "emoji": "🌭", "tier": 1, "level": 5,  "cost": 12_000},
    {"id": "newspaper_kiosk",  "name": "Newspaper Kiosk",  "emoji": "📰", "tier": 1, "level": 5,  "cost": 14_000},
    {"id": "vending_route",    "name": "Vending Route",    "emoji": "🥤", "tier": 1, "level": 5,  "cost": 16_000},
    {"id": "laundromat",       "name": "Laundromat",       "emoji": "🧺", "tier": 1, "level": 5,  "cost": 18_000},
    {"id": "car_wash",         "name": "Car Wash",         "emoji": "🚿", "tier": 1, "level": 5,  "cost": 20_000},
    {"id": "barber_shop",      "name": "Barber Shop",      "emoji": "💈", "tier": 1, "level": 5,  "cost": 22_000},
    {"id": "coffee_cart",      "name": "Coffee Cart",      "emoji": "☕", "tier": 1, "level": 5,  "cost": 24_000},
    {"id": "food_truck",       "name": "Food Truck",       "emoji": "🚚", "tier": 1, "level": 5,  "cost": 26_000},
    {"id": "nail_salon",       "name": "Nail Salon",       "emoji": "💅", "tier": 1, "level": 5,  "cost": 28_000},
    {"id": "bait_shop",        "name": "Bait Shop",        "emoji": "🎣", "tier": 1, "level": 5,  "cost": 30_000},
    {"id": "flower_shop",      "name": "Flower Shop",      "emoji": "🌷", "tier": 1, "level": 5,  "cost": 32_000},
    # Tier 2
    {"id": "corner_store",     "name": "Corner Store",     "emoji": "🏪", "tier": 2, "level": 10, "cost": 40_000},
    {"id": "pizza_parlor",     "name": "Pizza Parlor",     "emoji": "🍕", "tier": 2, "level": 10, "cost": 46_000},
    {"id": "dive_bar",         "name": "Dive Bar",         "emoji": "🍺", "tier": 2, "level": 10, "cost": 52_000},
    {"id": "arcade",           "name": "Arcade",           "emoji": "🕹️", "tier": 2, "level": 10, "cost": 60_000},
    {"id": "tattoo_parlor",    "name": "Tattoo Parlor",    "emoji": "🖋️", "tier": 2, "level": 10, "cost": 68_000},
    {"id": "bowling_alley",    "name": "Bowling Alley",    "emoji": "🎳", "tier": 2, "level": 10, "cost": 76_000},
    {"id": "gas_station",      "name": "Gas Station",      "emoji": "⛽", "tier": 2, "level": 10, "cost": 84_000},
    {"id": "pawn_shop",        "name": "Pawn Shop",        "emoji": "💰", "tier": 2, "level": 10, "cost": 90_000},
    {"id": "motel",            "name": "Motel",            "emoji": "🛏️", "tier": 2, "level": 10, "cost": 96_000},
    {"id": "auto_shop",        "name": "Auto Shop",        "emoji": "🔧", "tier": 2, "level": 10, "cost": 100_000},
    # Tier 3
    {"id": "movie_theater",    "name": "Movie Theater",    "emoji": "🎬", "tier": 3, "level": 15, "cost": 120_000},
    {"id": "nightclub",        "name": "Nightclub",        "emoji": "🪩", "tier": 3, "level": 15, "cost": 140_000},
    {"id": "gym",              "name": "Gym",              "emoji": "🏋️", "tier": 3, "level": 15, "cost": 160_000},
    {"id": "apartment_block",  "name": "Apartment Block",  "emoji": "🏢", "tier": 3, "level": 15, "cost": 190_000},
    {"id": "recording_studio", "name": "Recording Studio", "emoji": "🎙️", "tier": 3, "level": 15, "cost": 220_000},
    {"id": "marina",           "name": "Marina",           "emoji": "⛵", "tier": 3, "level": 15, "cost": 260_000},
    {"id": "vineyard",         "name": "Vineyard",         "emoji": "🍇", "tier": 3, "level": 15, "cost": 300_000},
    # Tier 4
    {"id": "ski_lodge",        "name": "Ski Lodge",        "emoji": "🎿", "tier": 4, "level": 20, "cost": 400_000},
    {"id": "shopping_mall",    "name": "Shopping Mall",    "emoji": "🛍️", "tier": 4, "level": 20, "cost": 600_000},
    {"id": "server_farm",      "name": "Server Farm",      "emoji": "🖥️", "tier": 4, "level": 20, "cost": 800_000},
    {"id": "private_island",   "name": "Private Island",   "emoji": "🏝️", "tier": 4, "level": 20, "cost": 1_000_000},
    # Tier 5
    {"id": "casino_resort",    "name": "Casino Resort",    "emoji": "🎰", "tier": 5, "level": 25, "cost": 1_300_000},
    {"id": "skyscraper",       "name": "Skyscraper",       "emoji": "🏙️", "tier": 5, "level": 25, "cost": 1_600_000},
    {"id": "space_port",       "name": "Space Port",       "emoji": "🚀", "tier": 5, "level": 25, "cost": 2_000_000},
]

PROPERTIES_BY_ID: dict[str, dict] = {p["id"]: p for p in PROPERTIES}


def find_property(token: str) -> dict | None:
    """Resolve user input to a catalog entry: exact id, or case-insensitive
    name match (spaces/underscores interchangeable)."""
    t = token.strip().lower().replace("_", " ")
    for p in PROPERTIES:
        if p["id"].replace("_", " ") == t or p["name"].lower() == t:
            return p
    return None


# ── Revenue math ─────────────────────────────────────────────────────────────


def daily_revenue(cost: int) -> int:
    """Daily revenue for a property: 1.1% of its purchase price per day."""
    return cost * PROPERTY_DAILY_REVENUE_PERMILLE // 1000


def owned_properties(uid: int) -> list[dict]:
    """Catalog entries this user owns, in catalog order."""
    return [
        p for p in PROPERTIES
        if (row := state.property_owners.get(p["id"])) and row["owner_id"] == int(uid)
    ]


def owned_property_count(uid: int) -> int:
    return len(owned_properties(uid))


def portfolio_value(uid: int) -> int:
    """Book value (sum of catalog costs) of everything the user owns."""
    return sum(p["cost"] for p in owned_properties(uid))


def portfolio_daily_revenue(uid: int, *, boosted: bool = True) -> int:
    """The user's total property revenue per day. With `boosted`, applies
    artifact percentage bonuses (the number the accrual actually pays)."""
    base = sum(daily_revenue(p["cost"]) for p in owned_properties(uid))
    if not boosted:
        return base
    from src.artifacts import property_revenue_boosted
    return property_revenue_boosted(uid, base, owned_property_count(uid))


def accrual_cap(uid: int) -> int:
    """Max unredeemed revenue: base 10k + artifact bonuses — or one full
    day's (boosted) portfolio revenue when that's greater, so a daily
    claimer always collects everything they earned."""
    from src.artifacts import property_accrual_cap_bonus
    return max(
        PROPERTY_ACCRUAL_CAP_BASE + property_accrual_cap_bonus(uid),
        portfolio_daily_revenue(uid),
    )


def pending_property_revenue(uid: int, now: float = None) -> int:
    """Accrued, uncollected revenue for `uid` — artifact-boosted and capped.

    Pure read: mutates nothing. Each property accrues from
    max(user's last payout, that property's acquisition), so a freshly
    bought deed never pays backdated rent.
    """
    props = owned_properties(uid)
    if not props:
        return 0
    if now is None:
        now = time.time()
    user = state.economy["users"].get(str(uid), {})
    paid_at = float(user.get("property_paid_at", 0.0) or 0.0)
    base = 0.0
    for p in props:
        row = state.property_owners[p["id"]]
        since = max(paid_at, float(row["acquired_at"]))
        days = max(0.0, (now - since) / 86400.0)
        base += daily_revenue(p["cost"]) * days
    from src.artifacts import property_revenue_boosted
    boosted = property_revenue_boosted(uid, int(base), len(props))
    return min(boosted, accrual_cap(uid))


async def bank_property_revenue(uid: int, now: float = None) -> int:
    """Bank the user's pending revenue: stamp property_paid_at and add the
    coins. Returns the amount banked (0 for non-owners / nothing pending).

    The read + stamp happen synchronously before the add_balance await, so a
    concurrent second claim sees a fresh timestamp and accrues ~0 (see
    CLAUDE.md on per-user command races). Callers are responsible for the
    daily-claim gate; this only guards the revenue itself.
    """
    if now is None:
        now = time.time()
    amount = pending_property_revenue(uid, now)
    if amount <= 0:
        return 0
    user = state.economy["users"][str(uid)]
    prior_paid_at = user.get("property_paid_at", 0.0)
    user["property_paid_at"] = now                       # claim, sync
    user["property_revenue_total"] = int(user.get("property_revenue_total", 0) or 0) + amount
    from src.economy import add_balance
    try:
        await add_balance(uid, amount)                   # persists via save_economy
    except Exception:
        user["property_paid_at"] = prior_paid_at         # roll back on failure
        user["property_revenue_total"] -= amount
        raise
    return amount
