"""Chess shop catalog: piece sets and board themes unlocked with chess Elo.

Costs are paid from the user's spendable chess Elo (total_elo_defeated minus
elo_spent — see chess_elo_balance in src/games/bot_chess_rewards.py), not
coins. Unlocks are permanent, global (no guild dimension, like the economy),
and live in state.chess_unlocks / the chess_unlocks table.

`key` is the renderer name (render_board_png's piece_set= / theme=); `id` is
the stable identifier stored in chess_unlocks. Cost 0 marks the default
everyone already has — it is never written to the table. Nothing equips an
unlocked item yet; the shop only sells the unlocks.
"""
from src import state
from src.games.chess_render import (
    BOARD_THEMES, DEFAULT_BOARD_THEME, DEFAULT_PIECE_SET, PIECE_SET_KEYS,
)


PIECE_SET_ITEMS = [
    {"id": "pieces:cburnett", "key": "cburnett", "name": "Classic", "cost": 0},
    {"id": "pieces:fantasy", "key": "fantasy", "name": "Fantasy", "cost": 5_000},
    {"id": "pieces:celtic", "key": "celtic", "name": "Celtic", "cost": 5_000},
    {"id": "pieces:rhosgfx", "key": "rhosgfx", "name": "Wood", "cost": 10_000},
    {"id": "pieces:spatial", "key": "spatial", "name": "Spatial", "cost": 10_000},
    {"id": "pieces:merida", "key": "merida", "name": "Merida", "cost": 15_000},
    {"id": "pieces:kiwen-suwi", "key": "kiwen-suwi", "name": "Kiwen-Suwi", "cost": 20_000},
    {"id": "pieces:totoy", "key": "totoy", "name": "Geometric", "cost": 20_000},
    {"id": "pieces:pixel", "key": "pixel", "name": "Pixel", "cost": 20_000},
]

BOARD_ITEMS = [
    {"id": "board:default", "key": "default", "name": "Default", "cost": 0},
    {"id": "board:brown", "key": "brown", "name": "Brown", "cost": 10_000},
    {"id": "board:blue", "key": "blue", "name": "Blue", "cost": 10_000},
    {"id": "board:green", "key": "green", "name": "Green", "cost": 10_000},
    {"id": "board:coffee", "key": "coffee", "name": "Coffee", "cost": 20_000},
    {"id": "board:purple", "key": "purple", "name": "Purple", "cost": 20_000},
    {"id": "board:ice", "key": "ice", "name": "Ice", "cost": 20_000},
    {"id": "board:charcoal", "key": "charcoal", "name": "Charcoal", "cost": 20_000},
]

# One flat list so the shop can number items continuously across both
# sections and `!chess shop buy <n>` is unambiguous.
CHESS_SHOP_ITEMS = PIECE_SET_ITEMS + BOARD_ITEMS

# Prestige gate: every 20k item additionally requires having beaten an
# 1100+ Elo bot (the Maia boundary) at least once — max_elo_defeated, not
# spendable balance. Derived from cost so future 20k items inherit the gate.
PRESTIGE_COST = 20_000
PRESTIGE_MIN_MAX_ELO = 1_100
for _it in CHESS_SHOP_ITEMS:
    _it["req_max_elo"] = PRESTIGE_MIN_MAX_ELO if _it["cost"] >= PRESTIGE_COST else 0


def elo_requirement_met(uid: int, item: dict) -> bool:
    """Whether the user clears the item's prestige gate (best single bot
    Elo defeated, lifetime — spending Elo can never lose it)."""
    req = item.get("req_max_elo", 0)
    if req <= 0:
        return True
    from src.games.bot_chess_rewards import chess_ranks
    max_elo, _ = chess_ranks(uid)
    return max_elo >= req

# Every catalog key must exist in the renderer, so an unlock can never point
# at something render_board_png doesn't know (module-load assert, same
# pattern as PROPERTY_UPGRADES in src/properties.py).
assert all(it["key"] in PIECE_SET_KEYS for it in PIECE_SET_ITEMS)
assert all(it["key"] in BOARD_THEMES for it in BOARD_ITEMS)


def has_chess_unlock(uid: int, item_id: str) -> bool:
    """Whether the user owns this chess-shop item. Cost-0 defaults are owned
    by everyone without a table row."""
    for it in CHESS_SHOP_ITEMS:
        if it["id"] == item_id and it["cost"] == 0:
            return True
    return item_id in state.chess_unlocks.get(int(uid), set())


def equipped_cosmetics(uid: int) -> tuple[str, str]:
    """(piece_set, board_theme) renderer keys for this user — defaults for
    anyone with no equipped row (including the bot itself, so resolving
    cosmetics for the bot's uid in bot games is harmless)."""
    row = state.chess_equipped.get(int(uid))
    if not row:
        return DEFAULT_PIECE_SET, DEFAULT_BOARD_THEME
    return (
        row.get("pieces") or DEFAULT_PIECE_SET,
        row.get("board") or DEFAULT_BOARD_THEME,
    )


def find_chess_item(token: str) -> dict | None:
    """Look up a catalog item by list number (1-based across the combined
    list), display name, or renderer key. Case-insensitive."""
    token = token.strip().lower()
    if token.isdigit():
        idx = int(token)
        if 1 <= idx <= len(CHESS_SHOP_ITEMS):
            return CHESS_SHOP_ITEMS[idx - 1]
        return None
    for it in CHESS_SHOP_ITEMS:
        if token in (it["name"].lower(), it["key"].lower()):
            return it
    return None
