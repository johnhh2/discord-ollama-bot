"""The Minecraft block shop's catalog: what the bot buys and sells on the
Bedrock server, priced in 🟫 blocks.

Blocks are the shop's own currency. They can't be bought, paid or won —
the only way in is selling to the shop, which is what keeps the coin
economy and the server's survival economy apart. Two rules decide what's
listed, both about abuse:

* **Sell side — nothing a farm makes.** Iron and gold farms, sheep farms,
  cobblestone / stone / basalt generators and guardian farms turn AFK time
  into blocks, so their products are buy-only or absent. The minerals the
  operator listed (coal, copper, lapis, quartz, diamond, netherite and their
  ores) sell at their listed prices and are not for sale at all.
* **Buy side — nothing that turns back into a resource.** No wood, no
  ingot blocks, no hay / bone / kelp / slime blocks, no gilded blackstone:
  a bought block must stay a block. Copper *blocks* uncraft to ingots, so
  only the cut and chiseled forms are sold.

Prices follow verzion's Minecraft economy price guide's method — a raw
material's value, and a crafted block at its ingredients' cost per block
made — with the shop paying half of what it charges (the ratio the listed
minerals use). Sell never exceeds half of buy anywhere, so buying, crafting
and selling back can't mint blocks. Only full blocks are listed; slabs,
stairs and walls would price at ½ / 1½ / 1 of these.

The price the operator can't check against the guide is the one to edit:
every entry is a plain row below, in tenths of a block (`_price`), and
ids are Bedrock's flattened item names (`give`/`clear` reject a wrong one
and the command reports the console's reply). Every helper the cog needs
(`find_item`, `fmt_blocks`, `items_in`) reads this table.
"""
from __future__ import annotations

from dataclasses import dataclass

BLOCK = "🟫"  # :brown_square:

# The most a single buy or sell moves: a full 36-slot inventory of stacks.
MAX_TRADE_COUNT = 36 * 64


@dataclass(frozen=True)
class ShopItem:
    id: str                  # Bedrock item id without the `minecraft:` prefix
    name: str
    category: str
    buy: int | None          # tenths of a block the shop charges; None = not for sale
    sell: int | None         # tenths of a block the shop pays; None = not accepted
    aliases: tuple[str, ...] = ()


def _price(blocks: float) -> int:
    """A price in blocks → tenths. Prices are written with at most one
    decimal so the purse stays an integer."""
    tenths = round(blocks * 10)
    assert abs(tenths - blocks * 10) < 1e-6, f"price {blocks} isn't a whole tenth"
    return tenths


def fmt_blocks(tenths: int) -> str:
    """`45 🟫`, `2.5 🟫`, `1,260 🟫`."""
    whole, frac = divmod(abs(int(tenths)), 10)
    sign = "-" if tenths < 0 else ""
    number = f"{whole:,}" + (f".{frac}" if frac else "")
    return f"{sign}{number} {BLOCK}"


CATEGORIES: tuple[tuple[str, str], ...] = (
    ("minerals", "⛏️ Minerals & ores"),
    ("stone", "🪨 Stone"),
    ("deepslate", "⬛ Deepslate & tuff"),
    ("earth", "🏜️ Sand, gravel, bricks & mud"),
    ("nether", "🔥 Nether"),
    ("end", "🌌 End"),
    ("quartz", "⬜ Quartz"),
    ("copper", "🟧 Copper"),
    ("prismarine", "🌊 Prismarine"),
    ("glass", "🪟 Glass"),
    ("wool", "🧶 Wool"),
    ("terracotta", "🏺 Terracotta"),
    ("concrete", "🧱 Concrete"),
)
CATEGORY_LABELS = dict(CATEGORIES)

_COLORS = ("white", "light_gray", "gray", "black", "brown", "red", "orange", "yellow",
           "lime", "green", "cyan", "light_blue", "blue", "purple", "magenta", "pink")


def _mineral(item_id: str, name: str, pays: float, *aliases: str) -> ShopItem:
    return ShopItem(item_id, name, "minerals", None, _price(pays), aliases)


def _block(item_id: str, name: str, category: str, buy: float, *aliases: str, farmable: bool = False) -> ShopItem:
    """A decorative block: sold at `buy`, bought back at half — unless a
    farm can make it, in which case the shop only sells it."""
    tenths = _price(buy)
    return ShopItem(item_id, name, category, tenths, None if farmable else tenths // 2, aliases)


def _colored(template: str, name_template: str, category: str, buy: float, *, farmable: bool = False,
             ids: dict[str, str] | None = None) -> list[ShopItem]:
    out = []
    for color in _COLORS:
        item_id = (ids or {}).get(color) or template.format(color)
        pretty = color.replace("_", " ").title()
        out.append(_block(item_id, name_template.format(pretty), category, buy, farmable=farmable))
    return out


ITEMS: tuple[ShopItem, ...] = tuple([
    # ── minerals & ores: the operator's list, sell only ─────────────────
    _mineral("coal", "Coal", 2.5),
    _mineral("coal_block", "Block of Coal", 22.5, "coal block"),
    _mineral("coal_ore", "Coal Ore", 5),
    _mineral("deepslate_coal_ore", "Deepslate Coal Ore", 6),
    _mineral("copper_block", "Copper Block", 13.5, "block of copper"),
    _mineral("exposed_copper", "Exposed Copper", 13.5),
    _mineral("weathered_copper", "Weathered Copper", 13.5),
    _mineral("oxidized_copper", "Oxidized Copper", 13.5),
    _mineral("waxed_copper", "Waxed Copper Block", 13.5, "waxed copper"),
    _mineral("waxed_exposed_copper", "Waxed Exposed Copper", 13.5),
    _mineral("waxed_weathered_copper", "Waxed Weathered Copper", 13.5),
    _mineral("waxed_oxidized_copper", "Waxed Oxidized Copper", 13.5),
    _mineral("raw_copper", "Raw Copper", 1.5),
    _mineral("copper_ingot", "Copper Ingot", 1.5),
    _mineral("raw_copper_block", "Block of Raw Copper", 13.5, "raw copper block"),
    _mineral("copper_ore", "Copper Ore", 5),
    _mineral("deepslate_copper_ore", "Deepslate Copper Ore", 6),
    _mineral("lapis_lazuli", "Lapis Lazuli", 2.5, "lapis"),
    _mineral("lapis_block", "Lapis Lazuli Block", 22.5, "block of lapis lazuli", "lapis lazuli block", "block of lapis"),
    _mineral("lapis_ore", "Lapis Lazuli Ore", 17.5, "lapis ore"),
    _mineral("deepslate_lapis_ore", "Deepslate Lapis Lazuli Ore", 18.5, "deepslate lapis ore"),
    _mineral("quartz", "Nether Quartz", 1.5),
    _mineral("quartz_ore", "Nether Quartz Ore", 3.5),
    _mineral("diamond", "Diamond", 140),
    _mineral("diamond_block", "Block of Diamond", 1260, "diamond block"),
    _mineral("diamond_ore", "Diamond Ore", 500),
    _mineral("deepslate_diamond_ore", "Deepslate Diamond Ore", 550),
    _mineral("ancient_debris", "Ancient Debris", 75),
    _mineral("netherite_scrap", "Netherite Scrap", 75),
    _mineral("netherite_ingot", "Netherite Ingot", 420),
    _mineral("netherite_block", "Block of Netherite", 3780, "netherite block"),
    # ── stone ───────────────────────────────────────────────────────────
    _block("cobblestone", "Cobblestone", "stone", 1, farmable=True),
    _block("stone", "Stone", "stone", 2, farmable=True),
    _block("smooth_stone", "Smooth Stone", "stone", 3, farmable=True),
    _block("stone_bricks", "Stone Bricks", "stone", 2, "stone brick", farmable=True),
    _block("mossy_stone_bricks", "Mossy Stone Bricks", "stone", 3, farmable=True),
    _block("cracked_stone_bricks", "Cracked Stone Bricks", "stone", 3, farmable=True),
    _block("chiseled_stone_bricks", "Chiseled Stone Bricks", "stone", 2, farmable=True),
    _block("mossy_cobblestone", "Mossy Cobblestone", "stone", 2, farmable=True),
    _block("granite", "Granite", "stone", 1),
    _block("polished_granite", "Polished Granite", "stone", 1),
    _block("diorite", "Diorite", "stone", 1),
    _block("polished_diorite", "Polished Diorite", "stone", 1),
    _block("andesite", "Andesite", "stone", 1),
    _block("polished_andesite", "Polished Andesite", "stone", 1),
    _block("calcite", "Calcite", "stone", 2),
    _block("dripstone_block", "Dripstone Block", "stone", 2),
    _block("amethyst_block", "Block of Amethyst", "stone", 12, "amethyst block"),
    # ── deepslate & tuff ────────────────────────────────────────────────
    _block("cobbled_deepslate", "Cobbled Deepslate", "deepslate", 1),
    _block("deepslate", "Deepslate", "deepslate", 3),
    _block("polished_deepslate", "Polished Deepslate", "deepslate", 1),
    _block("deepslate_bricks", "Deepslate Bricks", "deepslate", 1),
    _block("cracked_deepslate_bricks", "Cracked Deepslate Bricks", "deepslate", 2),
    _block("deepslate_tiles", "Deepslate Tiles", "deepslate", 1),
    _block("cracked_deepslate_tiles", "Cracked Deepslate Tiles", "deepslate", 2),
    _block("chiseled_deepslate", "Chiseled Deepslate", "deepslate", 1),
    _block("tuff", "Tuff", "deepslate", 1),
    _block("polished_tuff", "Polished Tuff", "deepslate", 1),
    _block("tuff_bricks", "Tuff Bricks", "deepslate", 1),
    _block("chiseled_tuff", "Chiseled Tuff", "deepslate", 1),
    # ── sand, gravel, bricks & mud ──────────────────────────────────────
    _block("sand", "Sand", "earth", 1),
    _block("red_sand", "Red Sand", "earth", 2),
    _block("gravel", "Gravel", "earth", 1),
    _block("sandstone", "Sandstone", "earth", 4),
    _block("cut_sandstone", "Cut Sandstone", "earth", 4),
    _block("chiseled_sandstone", "Chiseled Sandstone", "earth", 4),
    _block("smooth_sandstone", "Smooth Sandstone", "earth", 5),
    _block("red_sandstone", "Red Sandstone", "earth", 8),
    _block("cut_red_sandstone", "Cut Red Sandstone", "earth", 8),
    _block("chiseled_red_sandstone", "Chiseled Red Sandstone", "earth", 8),
    _block("smooth_red_sandstone", "Smooth Red Sandstone", "earth", 9),
    _block("brick_block", "Bricks", "earth", 8, "brick", "bricks block"),
    _block("mud", "Mud", "earth", 1),
    _block("packed_mud", "Packed Mud", "earth", 2),
    _block("mud_bricks", "Mud Bricks", "earth", 2),
    # ── nether ──────────────────────────────────────────────────────────
    _block("netherrack", "Netherrack", "nether", 1, farmable=True),
    _block("nether_brick", "Nether Bricks", "nether", 4, "nether brick block"),
    _block("red_nether_brick", "Red Nether Bricks", "nether", 8),
    _block("chiseled_nether_bricks", "Chiseled Nether Bricks", "nether", 4),
    _block("cracked_nether_bricks", "Cracked Nether Bricks", "nether", 5),
    _block("blackstone", "Blackstone", "nether", 2),
    _block("polished_blackstone", "Polished Blackstone", "nether", 2),
    _block("polished_blackstone_bricks", "Polished Blackstone Bricks", "nether", 2),
    _block("cracked_polished_blackstone_bricks", "Cracked Polished Blackstone Bricks", "nether", 3),
    _block("chiseled_polished_blackstone", "Chiseled Polished Blackstone", "nether", 2),
    _block("basalt", "Basalt", "nether", 1, farmable=True),
    _block("polished_basalt", "Polished Basalt", "nether", 1, farmable=True),
    _block("smooth_basalt", "Smooth Basalt", "nether", 1, farmable=True),
    # ── end ─────────────────────────────────────────────────────────────
    _block("end_stone", "End Stone", "end", 3),
    _block("end_bricks", "End Stone Bricks", "end", 3, "end stone brick"),
    _block("purpur_block", "Purpur Block", "end", 6, "purpur"),
    _block("purpur_pillar", "Purpur Pillar", "end", 6),
    # ── quartz (the operator's block price; the rest priced alike) ──────
    _block("quartz_block", "Block of Quartz", "quartz", 12, "quartz block"),
    _block("chiseled_quartz_block", "Chiseled Quartz Block", "quartz", 12),
    _block("quartz_pillar", "Quartz Pillar", "quartz", 12),
    _block("quartz_bricks", "Quartz Bricks", "quartz", 12),
    _block("smooth_quartz", "Smooth Quartz Block", "quartz", 13, "smooth quartz"),
    # ── copper: the cut forms only (a copper block uncrafts to ingots) ──
    _block("cut_copper", "Cut Copper", "copper", 27),
    _block("exposed_cut_copper", "Exposed Cut Copper", "copper", 27),
    _block("weathered_cut_copper", "Weathered Cut Copper", "copper", 27),
    _block("oxidized_cut_copper", "Oxidized Cut Copper", "copper", 27),
    _block("waxed_cut_copper", "Waxed Cut Copper", "copper", 27),
    _block("waxed_exposed_cut_copper", "Waxed Exposed Cut Copper", "copper", 27),
    _block("waxed_weathered_cut_copper", "Waxed Weathered Cut Copper", "copper", 27),
    _block("waxed_oxidized_cut_copper", "Waxed Oxidized Cut Copper", "copper", 27),
    _block("chiseled_copper", "Chiseled Copper", "copper", 27),
    # ── prismarine: guardian farms, so sold only ────────────────────────
    _block("prismarine", "Prismarine", "prismarine", 16, farmable=True),
    _block("prismarine_bricks", "Prismarine Bricks", "prismarine", 36, farmable=True),
    _block("dark_prismarine", "Dark Prismarine", "prismarine", 36, farmable=True),
    _block("sea_lantern", "Sea Lantern", "prismarine", 50, farmable=True),
    # ── glass ───────────────────────────────────────────────────────────
    _block("glass", "Glass", "glass", 3),
    _block("tinted_glass", "Tinted Glass", "glass", 5),
    *_colored("{}_stained_glass", "{} Stained Glass", "glass", 3),
    # ── wool: sheep farms, so sold only ─────────────────────────────────
    *_colored("{}_wool", "{} Wool", "wool", 4, farmable=True),
    # ── terracotta ──────────────────────────────────────────────────────
    _block("hardened_clay", "Terracotta", "terracotta", 9),
    *_colored("{}_terracotta", "{} Terracotta", "terracotta", 10),
    # Bedrock never renamed the light-gray glazed one.
    *_colored("{}_glazed_terracotta", "{} Glazed Terracotta", "terracotta", 11,
              ids={"light_gray": "silver_glazed_terracotta"}),
    # ── concrete ────────────────────────────────────────────────────────
    *_colored("{}_concrete_powder", "{} Concrete Powder", "concrete", 2),
    *_colored("{}_concrete", "{} Concrete", "concrete", 2),
])

BY_ID: dict[str, ShopItem] = {item.id: item for item in ITEMS}
assert len(BY_ID) == len(ITEMS), "duplicate item id in the catalog"
assert all(item.category in CATEGORY_LABELS for item in ITEMS)
assert all(item.sell is None or item.buy is None or item.sell * 2 <= item.buy for item in ITEMS), \
    "an item sells back for more than half its price"


def items_in(category: str) -> list[ShopItem]:
    return [item for item in ITEMS if item.category == category]


def _norm(text: str) -> str:
    text = text.strip().lower()
    if text.startswith("minecraft:"):
        text = text[len("minecraft:"):]
    return " ".join(text.replace("_", " ").replace("-", " ").split())


def _names(item: ShopItem) -> set[str]:
    names = {_norm(item.id), _norm(item.name), *(_norm(a) for a in item.aliases)}
    # "block of coal" ↔ "coal block", either way round.
    for name in list(names):
        if name.startswith("block of "):
            names.add(name[len("block of "):] + " block")
        elif name.endswith(" block"):
            names.add("block of " + name[:-len(" block")])
    return names


_LOOKUP: dict[str, ShopItem] = {}
for _item in ITEMS:
    for _name in _names(_item):
        _LOOKUP.setdefault(_name, _item)


def find_item(query: str) -> ShopItem | None:
    """The item a player typed: an id, a name or an alias, spaces and
    underscores alike, `minecraft:` optional; else the one item whose name
    starts with what they typed. None when nothing (or several) fit."""
    q = _norm(query)
    if not q:
        return None
    hit = _LOOKUP.get(q)
    if hit is not None:
        return hit
    starts = {item for name, item in _LOOKUP.items() if name.startswith(q)}
    if len(starts) == 1:
        return starts.pop()
    return None


def match_category(query: str) -> str | None:
    q = _norm(query)
    for key, label in CATEGORIES:
        if q == key or q == _norm(label.split(" ", 1)[1]):
            return key
    for key, label in CATEGORIES:
        if key.startswith(q) or _norm(label.split(" ", 1)[1]).startswith(q):
            return key
    return None
