"""Real-estate catalog and revenue math (!assets).

Every property is a UNIQUE, bot-wide deed: at most one owner across all
servers, tracked in state.property_owners ({property_id: {owner_id,
acquired_at, list_price, listed_at, upgraded, secured, custom_name, and the
theft damage fields of src/property_theft.py}}, source of truth:
property_owners table). The catalog below holds the 36 shipped properties
(10k to 2m) and a release pool of 42 more (marked "pool"), which reach the
market one at a time at the 5am reset as the economy develops — see
src/property_supply.py. An unreleased pool deed doesn't exist to players:
every reader goes through is_released / released_properties.

Economics:
  - Revenue is 1.1% of purchase price per day, derived (never hand-typed):
    daily = cost * 11 // 1000. One invariant for the whole catalog.
  - Revenue is counted in gameplay days (5am CT rollover), not real time:
    every daily claim (!daily or the dailies channel) banks one full day of
    portfolio revenue, however long since the last claim. Each skipped day
    adds a full day's revenue to the missed-day bank, capped at
    PROPERTY_ACCRUAL_CAP_BASE plus artifact bonuses — or one day's portfolio
    revenue when that's larger, so a single skipped day always banks in
    full. The next claim pays today's rent plus the bank; today's rent is
    never capped, so the two together can exceed the cap.
  - The bot-wide interest curve, when set, replaces the flat 1.1% with a
    falling rate (src/rate_curves.py). That scales what owners are paid, per
    gameplay day; book value and upgrades are untouched.
  - A user owns at most max_owned() properties: PROPERTY_BASE_MAX_OWNED,
    raised with economy net worth by the release pass and never lowered.
  - Owners can list a property for sale at any price; listings are global,
    so a deed listed in one server can be bought from any other.

Artifact hooks (see src/artifacts.py):
    property_accrual_cap_bonus       — flat increase to the accrual cap
    property_revenue_pct_per_property — % revenue boost per property owned
                                        (capped by property_revenue_pct_cap)
    property_upgrade_discount_pct    — % off the upgrade price charged in
                                        !assets upgrade; property_value still
                                        folds in the upgrade's full cost
"""
import time

from src import state

# ── Tuning ───────────────────────────────────────────────────────────────────

PROPERTY_BASE_MAX_OWNED = 5
# The missed-day bank holds up to this many coins (or one day's full
# portfolio revenue, if greater). Artifacts add to it.
PROPERTY_ACCRUAL_CAP_BASE = 10_000
# Marketplace fee on player-to-player sales, paid by the seller out of the
# sale price into the buyer's guild's house pot (burned in DMs).
PROPERTY_SALE_FEE_PCT = 5
# Listing a property at or below this % of its value triggers a bank offer:
# the bank buys it back instantly at this % of value (upgrade included), no
# market fee. Declining the offer proceeds with the normal listing.
PROPERTY_BANK_BUYBACK_PCT = 75
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
# cost    — bank price in coins; also drives daily revenue (see daily_revenue)

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
    # ── Release pool ──
    # Unreleased until the 5am release pass admits them (src/property_supply.py);
    # "pool" marks them. Order of release is PROPERTY_RELEASE_ORDER, not this list.
    # Tier 1 pool
    {"id": "shoe_shine_stand",   "name": "Shoe Shine Stand",  "emoji": "👞", "tier": 1, "level": 5, "cost": 11_000, "pool": True},
    {"id": "pretzel_stand",      "name": "Pretzel Stand",     "emoji": "🥨", "tier": 1, "level": 5, "cost": 13_000, "pool": True},
    {"id": "ice_cream_truck",    "name": "Ice Cream Truck",   "emoji": "🍦", "tier": 1, "level": 5, "cost": 15_000, "pool": True},
    {"id": "key_kiosk",          "name": "Key Kiosk",         "emoji": "🔑", "tier": 1, "level": 5, "cost": 17_000, "pool": True},
    {"id": "thrift_shop",        "name": "Thrift Shop",       "emoji": "👕", "tier": 1, "level": 5, "cost": 19_000, "pool": True},
    {"id": "bike_repair",        "name": "Bike Repair",       "emoji": "🚲", "tier": 1, "level": 5, "cost": 21_000, "pool": True},
    {"id": "donut_shop",         "name": "Donut Shop",        "emoji": "🍩", "tier": 1, "level": 5, "cost": 23_000, "pool": True},
    {"id": "pet_groomer",        "name": "Pet Groomer",       "emoji": "🐩", "tier": 1, "level": 5, "cost": 25_000, "pool": True},
    {"id": "taco_stand",         "name": "Taco Stand",        "emoji": "🌮", "tier": 1, "level": 5, "cost": 27_000, "pool": True},
    {"id": "print_shop",         "name": "Print Shop",        "emoji": "🖨️", "tier": 1, "level": 5, "cost": 29_000, "pool": True},
    {"id": "record_store",       "name": "Record Store",      "emoji": "💿", "tier": 1, "level": 5, "cost": 31_000, "pool": True},
    {"id": "bakery",             "name": "Bakery",            "emoji": "🥐", "tier": 1, "level": 5, "cost": 34_000, "pool": True},
    # Tier 2 pool
    {"id": "diner",              "name": "Diner",             "emoji": "🍳", "tier": 2, "level": 10, "cost": 43_000, "pool": True},
    {"id": "sushi_bar",          "name": "Sushi Bar",         "emoji": "🍣", "tier": 2, "level": 10, "cost": 49_000, "pool": True},
    {"id": "bookstore",          "name": "Bookstore",         "emoji": "📚", "tier": 2, "level": 10, "cost": 56_000, "pool": True},
    {"id": "comic_shop",         "name": "Comic Shop",        "emoji": "🗯️", "tier": 2, "level": 10, "cost": 64_000, "pool": True},
    {"id": "pool_hall",          "name": "Pool Hall",         "emoji": "🎱", "tier": 2, "level": 10, "cost": 72_000, "pool": True},
    {"id": "mini_golf",          "name": "Mini Golf",         "emoji": "⛳", "tier": 2, "level": 10, "cost": 80_000, "pool": True},
    {"id": "brewery",            "name": "Brewery",           "emoji": "🍻", "tier": 2, "level": 10, "cost": 87_000, "pool": True},
    {"id": "hardware_store",     "name": "Hardware Store",    "emoji": "🔨", "tier": 2, "level": 10, "cost": 93_000, "pool": True},
    {"id": "karaoke_bar",        "name": "Karaoke Bar",       "emoji": "🎤", "tier": 2, "level": 10, "cost": 98_000, "pool": True},
    {"id": "ice_rink",           "name": "Ice Rink",          "emoji": "⛸️", "tier": 2, "level": 10, "cost": 104_000, "pool": True},
    # Tier 3 pool
    {"id": "art_gallery",        "name": "Art Gallery",       "emoji": "🖼️", "tier": 3, "level": 15, "cost": 130_000, "pool": True},
    {"id": "water_park",         "name": "Water Park",        "emoji": "🌊", "tier": 3, "level": 15, "cost": 150_000, "pool": True},
    {"id": "golf_course",        "name": "Golf Course",       "emoji": "🏌️", "tier": 3, "level": 15, "cost": 175_000, "pool": True},
    {"id": "boutique_hotel",     "name": "Boutique Hotel",    "emoji": "🏨", "tier": 3, "level": 15, "cost": 205_000, "pool": True},
    {"id": "concert_hall",       "name": "Concert Hall",      "emoji": "🎻", "tier": 3, "level": 15, "cost": 240_000, "pool": True},
    {"id": "racetrack",          "name": "Racetrack",         "emoji": "🏁", "tier": 3, "level": 15, "cost": 280_000, "pool": True},
    {"id": "ranch",              "name": "Ranch",             "emoji": "🐎", "tier": 3, "level": 15, "cost": 320_000, "pool": True},
    {"id": "tv_station",         "name": "TV Station",        "emoji": "📺", "tier": 3, "level": 15, "cost": 350_000, "pool": True},
    {"id": "zoo",                "name": "Zoo",               "emoji": "🦁", "tier": 3, "level": 15, "cost": 380_000, "pool": True},
    # Tier 4 pool
    {"id": "stadium",            "name": "Stadium",           "emoji": "🏟️", "tier": 4, "level": 20, "cost": 450_000, "pool": True},
    {"id": "cruise_ship",        "name": "Cruise Ship",       "emoji": "🛳️", "tier": 4, "level": 20, "cost": 550_000, "pool": True},
    {"id": "theme_park",         "name": "Theme Park",        "emoji": "🎢", "tier": 4, "level": 20, "cost": 700_000, "pool": True},
    {"id": "oil_rig",            "name": "Oil Rig",           "emoji": "🛢️", "tier": 4, "level": 20, "cost": 900_000, "pool": True},
    {"id": "film_studio",        "name": "Film Studio",       "emoji": "🎥", "tier": 4, "level": 20, "cost": 1_100_000, "pool": True},
    {"id": "airline",            "name": "Airline",           "emoji": "✈️", "tier": 4, "level": 20, "cost": 1_200_000, "pool": True},
    # Tier 5 pool
    {"id": "bank_tower",         "name": "Bank Tower",        "emoji": "🏦", "tier": 5, "level": 25, "cost": 1_400_000, "pool": True},
    {"id": "sports_franchise",   "name": "Sports Franchise",  "emoji": "🏆", "tier": 5, "level": 25, "cost": 1_800_000, "pool": True},
    {"id": "satellite_network",  "name": "Satellite Network", "emoji": "🛰️", "tier": 5, "level": 25, "cost": 2_200_000, "pool": True},
    {"id": "megacity",           "name": "Megacity",          "emoji": "🌆", "tier": 5, "level": 25, "cost": 2_500_000, "pool": True},
    {"id": "moon_base",          "name": "Moon Base",         "emoji": "🌕", "tier": 5, "level": 25, "cost": 3_000_000, "pool": True},
]

PROPERTIES_BY_ID: dict[str, dict] = {p["id"]: p for p in PROPERTIES}

# ── Upgrades ─────────────────────────────────────────────────────────────────
# Two upgrade types per property. Revenue: exactly one predefined upgrade per
# property, (name, cost, revenue_boost_pct), the row's `upgraded` flag.
# Costs were rolled once at 75–125% of the property's cost and boosts at
# 35–75%, then hardcoded (seed "discord-ollama-bot-upgrades-0050") — don't
# re-roll shipped values; owners have paid for them. The upgrade's cost folds
# into the property's VALUE (records, snapshots, bank buyback) and its boost
# into that property's daily revenue.

PROPERTY_UPGRADES: dict[str, tuple[str, int, int]] = {
    "lemonade_stand": ("Fresh-Squeeze Press", 8_500, 52),
    "hot_dog_cart": ("Chili Dog Station", 13_000, 52),
    "newspaper_kiosk": ("Magazine Rack", 17_000, 56),
    "vending_route": ("Cold-Drink Machines", 19_000, 35),
    "laundromat": ("Dry-Cleaning Service", 22_000, 64),
    "car_wash": ("Wax & Detail Bay", 21_000, 48),
    "barber_shop": ("Hot Towel Service", 21_000, 67),
    "coffee_cart": ("Espresso Machine", 24_000, 36),
    "food_truck": ("Fusion Menu", 28_000, 46),
    "nail_salon": ("Pedicure Spa Chairs", 22_000, 49),
    "bait_shop": ("Live Bait Tanks", 33_000, 57),
    "flower_shop": ("Wedding Arrangements", 27_000, 47),
    "corner_store": ("Lottery Counter", 47_000, 41),
    "pizza_parlor": ("Wood-Fired Oven", 57_000, 70),
    "dive_bar": ("Karaoke Night", 64_000, 50),
    "arcade": ("Prize Counter", 73_000, 40),
    "tattoo_parlor": ("Piercing Booth", 78_000, 44),
    "bowling_alley": ("Cosmic Bowling", 62_000, 64),
    "gas_station": ("Convenience Mart", 67_000, 57),
    "pawn_shop": ("Jewelry Case", 72_000, 44),
    "motel": ("Heated Pool", 93_000, 43),
    "auto_shop": ("Performance Garage", 115_000, 43),
    "movie_theater": ("IMAX Screen", 145_000, 35),
    "nightclub": ("VIP Lounge", 140_000, 41),
    "gym": ("Personal Training Studio", 147_000, 44),
    "apartment_block": ("Rooftop Terrace", 190_000, 52),
    "recording_studio": ("Mastering Suite", 189_000, 39),
    "marina": ("Yacht Charters", 255_000, 37),
    "vineyard": ("Tasting Room", 309_000, 74),
    "ski_lodge": ("Heli-Ski Pad", 396_000, 51),
    "shopping_mall": ("Food Court", 708_000, 45),
    "server_farm": ("GPU Cluster", 968_000, 56),
    "private_island": ("Beach Resort", 1_100_000, 60),
    "casino_resort": ("High-Roller Suite", 1_534_000, 68),
    "skyscraper": ("Observation Deck", 1_360_000, 60),
    "space_port": ("Orbital Hotel", 1_840_000, 55),
    # Release pool — rolled the same way (seed "discord-ollama-bot-property-pool-0084").
    "shoe_shine_stand": ("Leather Care Kit", 13_000, 67),
    "pretzel_stand": ("Cheese Dip Bar", 12_000, 36),
    "ice_cream_truck": ("Soft-Serve Machine", 17_000, 69),
    "key_kiosk": ("Smart Lock Counter", 19_000, 71),
    "thrift_shop": ("Vintage Rack", 16_000, 42),
    "bike_repair": ("E-Bike Rentals", 21_000, 47),
    "donut_shop": ("Drive-Thru Window", 20_000, 73),
    "pet_groomer": ("Doggy Daycare", 25_000, 65),
    "taco_stand": ("Late-Night Menu", 30_000, 39),
    "print_shop": ("Large-Format Printer", 34_000, 40),
    "record_store": ("Listening Booths", 25_000, 47),
    "bakery": ("Wedding Cakes", 36_000, 71),
    "diner": ("24-Hour Service", 50_000, 66),
    "sushi_bar": ("Omakase Counter", 45_000, 71),
    "bookstore": ("Reading Café", 44_000, 40),
    "comic_shop": ("Tabletop Game Room", 79_000, 49),
    "pool_hall": ("League Nights", 77_000, 71),
    "mini_golf": ("Glow Course", 67_000, 48),
    "brewery": ("Taproom", 80_000, 56),
    "hardware_store": ("Tool Rental Desk", 100_000, 44),
    "karaoke_bar": ("Private Rooms", 113_000, 66),
    "ice_rink": ("Hockey League", 120_000, 68),
    "art_gallery": ("Auction House", 109_000, 56),
    "water_park": ("Wave Pool", 180_000, 48),
    "golf_course": ("Country Club", 157_000, 57),
    "boutique_hotel": ("Rooftop Bar", 191_000, 45),
    "concert_hall": ("Season Tickets", 206_000, 59),
    "racetrack": ("Grandstand Boxes", 343_000, 37),
    "ranch": ("Guest Lodge", 272_000, 66),
    "tv_station": ("Prime-Time Slot", 274_000, 35),
    "zoo": ("Safari Tram", 289_000, 61),
    "stadium": ("Luxury Suites", 456_000, 45),
    "cruise_ship": ("Casino Deck", 664_000, 35),
    "theme_park": ("Record Coaster", 531_000, 52),
    "oil_rig": ("Second Platform", 732_000, 55),
    "film_studio": ("Franchise Deal", 1_272_000, 74),
    "airline": ("First-Class Cabins", 1_328_000, 56),
    "bank_tower": ("Private Vaults", 1_509_000, 57),
    "sports_franchise": ("Championship Run", 1_698_000, 51),
    "satellite_network": ("Global Coverage", 2_748_000, 36),
    "megacity": ("Transit Line", 2_703_000, 55),
    "moon_base": ("Helium-3 Mine", 3_138_000, 75),
}

assert set(PROPERTY_UPGRADES) == set(PROPERTIES_BY_ID), \
    "every property needs exactly one entry in PROPERTY_UPGRADES"

# The second upgrade type, security: price only, the same effect on every
# deed (SECURITY_SUCCESS_MULT on a property theft's odds). Rolled once at
# 75–125% of the property's cost, divided by 4, then hardcoded (seed
# "discord-ollama-bot-security-0087") — don't re-roll shipped values. Like
# revenue, its cost folds into the property's VALUE. Tiers 1 and 2 have no
# security upgrade. SECURITY_UPGRADES_LIVE takes it off sale.
PROPERTY_SECURITY_UPGRADES: dict[str, int] = {
    "movie_theater": 33_000,
    "nightclub": 36_500,
    "gym": 45_000,
    "apartment_block": 51_000,
    "recording_studio": 56_000,
    "marina": 65_000,
    "vineyard": 77_000,
    "ski_lodge": 105_000,
    "shopping_mall": 122_000,
    "server_farm": 248_000,
    "private_island": 189_000,
    "casino_resort": 404_000,
    "skyscraper": 492_000,
    "space_port": 536_000,
    "art_gallery": 33_000,
    "water_park": 36_000,
    "golf_course": 51_000,
    "boutique_hotel": 52_000,
    "concert_hall": 46_500,
    "racetrack": 87_000,
    "ranch": 72_000,
    "tv_station": 84_000,
    "zoo": 116_000,
    "stadium": 118_000,
    "cruise_ship": 111_000,
    "theme_park": 161_000,
    "oil_rig": 198_000,
    "film_studio": 219_000,
    "airline": 287_000,
    "bank_tower": 411_000,
    "sports_franchise": 417_000,
    "satellite_network": 466_000,
    "megacity": 698_000,
    "moon_base": 843_000,
}
SECURITY_SUCCESS_MULT = 0.75
SECURITY_UPGRADES_LIVE = True

SECURITY_MIN_TIER = 3

assert set(PROPERTY_SECURITY_UPGRADES) == {p["id"] for p in PROPERTIES if p["tier"] >= SECURITY_MIN_TIER}, \
    "every tier 3+ property, and no other, needs an entry in PROPERTY_SECURITY_UPGRADES"

# Upgrade types as players name them; "revenue" is PROPERTY_UPGRADES.
UPGRADE_TYPES = ("revenue", "security")


def live_upgrade_types() -> tuple[str, ...]:
    return UPGRADE_TYPES if SECURITY_UPGRADES_LIVE else ("revenue",)


def upgrade_types_for(pid: str) -> tuple[str, ...]:
    """The upgrade types on sale for one deed: security only from tier 3."""
    return tuple(t for t in live_upgrade_types() if t != "security" or pid in PROPERTY_SECURITY_UPGRADES)

# ── Release order ────────────────────────────────────────────────────────────
# The order each tier's pool reaches the market: a seeded shuffle (seed
# "discord-ollama-bot-property-release-0084"), rolled once and hardcoded so
# players never see it change. New pool entries append to their tier's tuple
# (and get a threshold in property_supply) — never reorder a shipped tuple.
PROPERTY_RELEASE_ORDER: dict[int, tuple[str, ...]] = {
    1: ("ice_cream_truck", "print_shop", "pretzel_stand", "bakery", "bike_repair", "thrift_shop",
        "record_store", "pet_groomer", "donut_shop", "shoe_shine_stand", "taco_stand", "key_kiosk"),
    2: ("karaoke_bar", "hardware_store", "sushi_bar", "ice_rink", "mini_golf", "comic_shop",
        "pool_hall", "bookstore", "brewery", "diner"),
    3: ("zoo", "golf_course", "art_gallery", "water_park", "tv_station", "concert_hall",
        "ranch", "racetrack", "boutique_hotel"),
    4: ("film_studio", "stadium", "airline", "theme_park", "cruise_ship", "oil_rig"),
    5: ("sports_franchise", "moon_base", "bank_tower", "megacity", "satellite_network"),
}

assert {pid for ids in PROPERTY_RELEASE_ORDER.values() for pid in ids} \
    == {p["id"] for p in PROPERTIES if p.get("pool")}, \
    "every pool property needs exactly one place in PROPERTY_RELEASE_ORDER"
assert all(PROPERTIES_BY_ID[pid]["tier"] == t
           for t, ids in PROPERTY_RELEASE_ORDER.items() for pid in ids), \
    "a pool property is released in its own tier's order"


def is_released(pid: str) -> bool:
    """On the market or owned: shipped deeds always, pool deeds once the
    release pass admitted them. An owned row counts too, so a deed can't
    vanish from its owner if the released set failed to load."""
    p = PROPERTIES_BY_ID.get(pid)
    if p is None:
        return False
    return not p.get("pool") or pid in state.released_properties or pid in state.property_owners


def released_properties() -> list[dict]:
    """Every deed players can see, in catalog order."""
    return [p for p in PROPERTIES if is_released(p["id"])]


def max_owned() -> int:
    """Today's per-player deed limit: the high-water mark the release pass
    keeps in economy_meta, never below the base."""
    return max(PROPERTY_BASE_MAX_OWNED, int(state.economy.get("property_cap") or 0))


def find_property(token: str) -> dict | None:
    """Resolve user input to a released catalog entry: exact id,
    case-insensitive catalog-name match (spaces/underscores interchangeable),
    or an owner's custom business name (!assets rename). Unreleased pool
    deeds resolve to nothing, so a guessed name can't be bought early."""
    t = token.strip().lower().replace("_", " ")
    for p in released_properties():
        if p["id"].replace("_", " ") == t or p["name"].lower() == t:
            return p
    for pid, row in state.property_owners.items():
        custom = row.get("custom_name")
        if custom and custom.lower() == t and pid in PROPERTIES_BY_ID:
            return PROPERTIES_BY_ID[pid]
    return None



# ── Revenue math ─────────────────────────────────────────────────────────────


def daily_revenue(cost: int) -> int:
    """Daily revenue for a property: 1.1% of its purchase price per day."""
    return cost * PROPERTY_DAILY_REVENUE_PERMILLE // 1000


def property_daily_revenue(pid: str, row: dict = None, *, current: bool = False) -> int:
    """One deed's daily revenue including its upgrade boost (if bought),
    less theft damage until it's repaired. Artifact bonuses are NOT applied
    here — they're portfolio-wide and live in portfolio_daily_revenue /
    pending_property_revenue. With `current`, it's what the deed pays today
    on the interest curve."""
    base = daily_revenue(PROPERTIES_BY_ID[pid]["cost"])
    if row is None:
        row = state.property_owners.get(pid)
    if row and row.get("upgraded"):
        boost = PROPERTY_UPGRADES[pid][2]
        base = base * (100 + boost) // 100
    if row and row.get("damaged_at") is not None:
        from src.property_theft import DAMAGE_PCT
        base = base * (100 - DAMAGE_PCT) // 100
    if not current:
        return base
    from src.rate_curves import active_curve, property_mult
    return int(base * property_mult(active_curve(), time.time()))


def property_value(pid: str, row: dict = None) -> int:
    """One deed's book value: catalog cost, plus each upgrade's cost once
    bought — an upgraded property is worth what went into it."""
    value = PROPERTIES_BY_ID[pid]["cost"]
    if row is None:
        row = state.property_owners.get(pid)
    if row and row.get("upgraded"):
        value += PROPERTY_UPGRADES[pid][1]
    if row and row.get("secured"):
        value += PROPERTY_SECURITY_UPGRADES.get(pid, 0)
    return value


def bank_buyback_offer(pid: str, row: dict = None) -> int:
    """What the bank pays to buy this deed back (75% of its value)."""
    return property_value(pid, row) * PROPERTY_BANK_BUYBACK_PCT // 100


def owned_properties(uid: int) -> list[dict]:
    """Catalog entries this user owns, in catalog order."""
    return [
        p for p in PROPERTIES
        if (row := state.property_owners.get(p["id"])) and row["owner_id"] == int(uid)
    ]


def owned_property_count(uid: int) -> int:
    return len(owned_properties(uid))


def portfolio_value(uid: int) -> int:
    """Book value (catalog costs + bought upgrades) of everything the user
    owns."""
    return sum(property_value(p["id"]) for p in owned_properties(uid))


def total_owned_property_value() -> int:
    """Book value of every owned deed bot-wide (upgrades included) — the
    economy graph / !economy aggregate."""
    return sum(
        property_value(pid)
        for pid in state.property_owners if pid in PROPERTIES_BY_ID
    )


def portfolio_daily_revenue(uid: int, *, boosted: bool = True) -> int:
    """The user's total property revenue per day, upgrades included. With
    `boosted`, applies artifact percentage bonuses (the number the accrual
    actually pays)."""
    base = sum(property_daily_revenue(p["id"], current=True) for p in owned_properties(uid))
    if not boosted:
        return base
    from src.artifacts import property_revenue_boosted
    return property_revenue_boosted(uid, base, owned_property_count(uid))


def accrual_cap(uid: int) -> int:
    """Max the missed-day bank holds: base 10k + artifact bonuses — or one
    full day's (boosted) portfolio revenue when that's greater, so a single
    skipped day never loses coins to the cap. Only banked (skipped) days are
    capped; today's own rent never is."""
    from src.artifacts import property_accrual_cap_bonus
    return max(
        PROPERTY_ACCRUAL_CAP_BASE + property_accrual_cap_bonus(uid),
        portfolio_daily_revenue(uid),
    )


def _rent_due(uid: int, now: float) -> tuple[int, int]:
    """(today's rent, skipped-day rent) owed to `uid`, in base coins — before
    artifact boosts and the bank cap. Pure read.

    Revenue is counted in gameplay days (5am CT rollover), never in elapsed
    seconds. A deed earns one day's revenue for every gameplay day from its
    first unpaid day through today. The first unpaid day is the day after
    the owner's last payout (property_paid_at) — or the deed's acquisition
    day, if that's later. Today's day is "today's rent", paid in full by the
    next claim; every earlier one is a skipped day that goes to the bank.
    Hence a daily claimer's bank is always 0, nothing ever pays backdated
    rent, and a deed bought after today's claim starts earning tomorrow.
    """
    props = owned_properties(uid)
    if not props:
        return 0, 0
    from src.economy import gameplay_day
    from src.rate_curves import DAY_SECS, active_curve, property_mult
    curve = active_curve()
    today = gameplay_day(now)
    user = state.economy["users"].get(str(uid), {})
    paid_at = float(user.get("property_paid_at", 0.0) or 0.0)
    last_paid_day = gameplay_day(paid_at) if paid_at > 0 else None
    today_rent = 0
    skipped_rent = 0
    for p in props:
        row = state.property_owners[p["id"]]
        first_unpaid = gameplay_day(float(row["acquired_at"]))
        if last_paid_day is not None:
            first_unpaid = max(first_unpaid, last_paid_day + 1)
        days = today - first_unpaid + 1
        if days <= 0:
            continue
        rev = property_daily_revenue(p["id"], row)
        if curve is None:
            today_rent += rev
            skipped_rent += rev * (days - 1)
            continue
        # On a curve each skipped day pays that day's rate, not today's.
        today_rent += int(rev * property_mult(curve, now))
        skipped_rent += sum(
            int(rev * property_mult(curve, now - back * DAY_SECS)) for back in range(1, days)
        )
    return today_rent, skipped_rent


def pending_property_revenue(uid: int, now: float = None) -> int:
    """The missed-day bank: revenue for gameplay days the owner skipped —
    artifact-boosted and capped at accrual_cap. Always 0 for a daily
    claimer; every skipped claim adds one full day of portfolio revenue.
    Pure read: mutates nothing.
    """
    if now is None:
        now = time.time()
    _, skipped = _rent_due(uid, now)
    if skipped <= 0:
        return 0
    from src.artifacts import property_revenue_boosted
    boosted = property_revenue_boosted(uid, skipped, owned_property_count(uid))
    return min(boosted, accrual_cap(uid))


def claimable_property_revenue(uid: int, now: float = None) -> int:
    """What the next daily claim banks right now: today's full rent (never
    capped) plus the missed-day bank (capped). 0 once today has been paid.
    Pure read: mutates nothing.
    """
    if now is None:
        now = time.time()
    today_rent, _ = _rent_due(uid, now)
    from src.artifacts import property_revenue_boosted
    today = property_revenue_boosted(uid, today_rent, owned_property_count(uid))
    return today + pending_property_revenue(uid, now)


async def bank_property_revenue(uid: int, now: float = None) -> int:
    """Settle property revenue with today's daily claim: stamp
    property_paid_at and add today's rent plus the missed-day bank. Returns
    the amount banked (0 when nothing is due).

    The stamp lands synchronously before the add_balance await, so a
    concurrent second claim computes today as already paid and banks 0 (see
    CLAUDE.md on per-user command races). It lands even when nothing is due
    — for a non-owner too — so a deed bought later today starts earning
    tomorrow instead of counting today as a skipped day. Callers own the
    daily-claim gate; this only guards the revenue itself.
    """
    if now is None:
        now = time.time()
    amount = claimable_property_revenue(uid, now)
    user = state.economy["users"].get(str(uid))
    if user is None:
        return 0
    prior_paid_at = user.get("property_paid_at", 0.0)
    user["property_paid_at"] = now                       # claim, sync
    if amount <= 0:
        return 0
    user["property_revenue_total"] = int(user.get("property_revenue_total", 0) or 0) + amount
    from src.economy import add_balance
    try:
        await add_balance(uid, amount)                   # persists via save_economy
    except Exception:
        user["property_paid_at"] = prior_paid_at         # roll back on failure
        user["property_revenue_total"] -= amount
        raise
    return amount
