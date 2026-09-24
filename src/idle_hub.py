"""The `!idle` panel: one message — the character sheet and the map on
top, a page dropdown, the page's actions as buttons, and the last reply in
a field.

Same rules as the admin panel (src/admin_hub.py): the panel never plays a
turn itself. Every press becomes the typed subcommand — `idle shop find`,
`idle travel Velvragh`, `idle align chaotic good`, `idle admin push <id>
-2h` — run through `forwarding.forward` after `refusal_for`, with the reply
captured into the panel instead of posted. The subcommands that open a
Confirm prompt or tell a story in the channel (`bless`, `prestige`, `leave`,
`duel`, `admin remove`, `admin reset`) run `public`: a captured `ctx.send`
would swallow the prompt nobody could then click.

The default page, Common, is the sheet with the everyday buttons under it;
the other pages repeat them where they belong (Travel is on the road and
in a town with no market; the ladder on the realm page). An item is made
once, by its factory below, so a repeat can't drift from the original.

The sheet is the display, not an action: it draws `IdleCog._sheet` and
`_map_file` for the panel's target — the invoker, or a player picked with
"Look at a player" — the same helpers a typed `!idle status` sends.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import discord
from discord import ButtonStyle

from src import idlerpg as rpg
from src.forwarding import CapturingContext, forward, refusal_for
from src.helpers import emb, C_BLUE, C_RED, format_duration
from src.idle_map import MAP_FILENAME
from src.panel import Panel, PanelItem
from src.permissions import permitted_for
from src.settings_views import Field

PAGES = (
    ("common", "⚔️ Common"),
    ("town", "🏘️ Town"),
    ("road", "🧭 Road"),
    ("character", "🎭 Character"),
    ("realm", "🌍 Realm"),
    ("rules", "📖 Rules"),
    ("admin", "🛠️ Admin"),
)
_PAGE_LABELS = dict(PAGES)
MAP_PAGES = frozenset({"common", "road"})   # the pages that carry the map
PITCH = (
    "A game you win by doing nothing. Your character levels up while you're online and idle; "
    "items, battles and quests happen on their own.\n\n"
    "**Join** below to start · the **Rules** page says how it works · **Ladder** for who's ahead"
)


@dataclass(frozen=True, kw_only=True)
class IdleItem(PanelItem):
    """`command` is the IdleCog attribute of the subcommand to forward
    (`cmd_shop`); empty means the press only redraws the panel. A form's
    `call(hub, values)` returns `(args, kwargs)` for the callback, raising
    `ValueError` with a message to refuse; a plain item forwards `args` /
    `kwargs`. `public` items post their reply (and their Confirm prompt) in
    the channel. `look` picks whose sheet the panel shows; `goto` switches
    the page."""
    command: str = ""
    args: tuple = ()
    kwargs: dict | None = None
    call: Callable | None = None
    public: bool = False
    look: bool = False
    goto: str = ""


_USER = Field("user", "Who?", kind="users")


def _picked_user(values: dict, key: str = "user") -> str:
    ids = values.get(key) or []
    if not ids:
        raise ValueError("Pick a player first.")
    return str(ids[0])   # the subcommands take a name; `_match_player` reads a bare id


def _class_field() -> Field:
    from src.cogs.idle_cog import CLASS_MAX  # the cog imports this module; late, to break the cycle
    return Field("class_name", "Your class — invent one", placeholder="Drunken Bard, Tax Wizard…", max_length=CLASS_MAX)


# ── the items, one factory each; None when the invoker couldn't use it now ──

def _refresh(hub) -> IdleItem:
    return IdleItem(key="refresh", short="🔄 Refresh", label="🔄 Refresh — the sheet and the map as they are now")


def _own(hub) -> "IdleItem | None":
    if hub.target == hub.ctx.author.id:
        return None
    return IdleItem(key="own", short="↩️ My sheet", label="↩️ Back to your own sheet", look=True)


def _join(hub) -> "IdleItem | None":
    char = hub.char
    if char is not None and char["claimed"]:
        return None
    return IdleItem(
        key="join", command="cmd_join", title="Join the Idle RPG", fields=(_class_field(),), style=ButtonStyle.success,
        short="⚔️ Join" if char is None else "⚔️ Claim",
        label="⚔️ Join — start a character" if char is None else "⚔️ Claim — the character playing in your name",
        description="the class is yours to invent",
        call=lambda hub, v: ((), {"class_name": v["class_name"]}),
    )


def _items(hub) -> "IdleItem | None":
    if hub.char is None:
        return None
    return IdleItem(key="items", short="🎒 Items", label="🎒 Items — your items, bag and potions", command="cmd_items")


def _store(hub) -> "IdleItem | None":
    if hub.char is None or rpg.market_in_reach(hub.char) is None:
        return None
    return IdleItem(key="store", short="🛒 Store", label="🛒 Store — the town's market", goto="town", style=ButtonStyle.primary)


def _shop(hub) -> list[IdleItem]:
    char = hub.char
    if char is None or rpg.market_in_reach(char) is None:
        return []
    shorts = {"sell": "🛒 Sell", "find": "🔍 Find", "potion": "🧪 Potions", "sharpen": "🔪 Sharpen",
              "rush": "⏩ Rush", "duel": "🤺 Extra duel", "class": "🎭 New class"}
    items = []
    for key, text in hub.cog._shop_lines(char):
        label, _sep, price = text.partition(" · ")
        base = dict(key=f"shop-{key}", short=shorts[key], label=f"🛒 {label}", description=price, command="cmd_shop")
        if key == "potion":
            room, each = rpg.potion_room(char), rpg.shop_prices(char)["potion"]
            if not room:
                continue   # the belt is full; the sheet shows it
            items.append(IdleItem(**base, title="Potions", call=lambda hub, v: (("potion",), {"arg": v["n"][0]}),
                                  fields=(Field("n", "How many?", kind="choices",
                                                options=tuple((f"{n} · {each * n:,} gold", str(n)) for n in range(1, room + 1))),)))
        elif key == "sharpen":
            owned = tuple((f"{slot.title()} (level {char['items'][slot]['level']}) · {rpg.sharpen_price(char['items'][slot]):,} gold", slot)
                          for slot in rpg.ITEM_SLOTS if slot in char["items"])
            if not owned:
                continue
            items.append(IdleItem(**base, title="Sharpen", call=lambda hub, v: (("sharpen",), {"arg": v["slot"][0]}),
                                  fields=(Field("slot", "Which item?", kind="choices", options=owned),)))
        elif key == "class":
            items.append(IdleItem(**base, title="A new class", call=lambda hub, v: (("class",), {"arg": v["class_name"]}),
                                  fields=(_class_field(),)))
        else:
            items.append(IdleItem(**base, args=(key,)))
    return items


def _gamble(hub) -> "IdleItem | None":
    char = hub.char
    if char is None or rpg.market_in_reach(char) is None or char["gold"] <= 0:
        return None
    return IdleItem(
        key="gamble", short="🎲 Gamble", label="🎲 The tables — bet gold at even money", command="cmd_gamble", title="The Tables",
        description=f"the house wins {rpg.GAMBLE_LOSE_BELOW} in 100 · you have {char['gold']:,} gold",
        fields=(Field("stake", "Stake", placeholder="200, 1.5k, half, all", max_length=12),),
        call=lambda hub, v: ((v["stake"],), {}),
    )


def _hunt(hub) -> "IdleItem | None":
    char = hub.char
    if char is None or rpg.market_in_reach(char) is None or rpg.hunting(char) or rpg.hunt_wait(char, int(time.time())):
        return None
    return IdleItem(key="hunt", short="🏹 Hunt", label="🏹 Hunt — take one off the town's board", command="cmd_hunt",
                    description="kill N of a kind for a slice off your clock")


def _auto(hub) -> "IdleItem | None":
    char = hub.char
    if char is None:
        return None
    on = bool(char["auto_trade"])
    return IdleItem(
        key="auto", short=f"🔁 Auto-trade {'off' if on else 'on'}", label=f"🔁 Auto-trade — turn {'off' if on else 'on'}",
        command="cmd_shop", args=("auto",), kwargs={"arg": "off" if on else "on"},
        description="in a town's centre your character shops on its own" if on else "your character keeps its gold in town",
    )


def _travel(hub) -> "IdleItem | None":
    char = hub.char
    if char is None:
        return None
    quest = hub.cog._quest(hub.ctx.guild.id)
    if quest.get("kind") == "journey" and hub.ctx.author.id in quest["members"]:
        return None
    rpg.ensure_position(char, hub.cog.rng)   # what `!idle travel` does first
    places = tuple(
        ((f"{place} ({'market' if place in rpg.TOWNS else rpg.biome_at(*rpg.LANDMARKS[place])}) — "
          f"{rpg.travel_steps(char, place)} squares, about {format_duration(rpg.travel_eta_secs(char, place))}")[:100], place)
        for place in sorted(rpg.LANDMARKS, key=lambda p: rpg.travel_steps(char, p))
    )
    return IdleItem(
        key="travel", short="🧭 Travel", label="🧭 Travel — walk to a town or a wild place", command="cmd_travel", title="Travel",
        description="towns have markets; the wilds have the monsters",
        fields=(Field("place", "Where to?", kind="choices", options=places),),
        call=lambda hub, v: ((), {"where": v["place"][0]}),
    )


def _stop(hub) -> "IdleItem | None":
    char = hub.char
    if char is None or not char.get("travel_to"):
        return None
    return IdleItem(key="stop", short="🛑 Stop", label=f"🛑 Stop — give up on {char['travel_to']} and wander again",
                    command="cmd_travel", kwargs={"where": "stop"})


def _lore(hub) -> IdleItem:
    return IdleItem(
        key="lore", short="📜 Lore", label="📜 Lore — read about a place", command="cmd_lore", title="Lore",
        fields=(Field("place", "Which place?", kind="choices", options=tuple((p, p) for p in rpg.LANDMARKS)),),
        call=lambda hub, v: ((), {"where": v["place"][0]}),
    )


def _align(hub) -> "IdleItem | None":
    char = hub.char
    if char is None:
        return None
    return IdleItem(
        key="align", short="⚖️ Alignment", label="⚖️ Alignment — a law and a moral, once a day", command="cmd_align", title="Alignment",
        description=f"now {rpg.alignment_label(char)}",
        fields=(Field("alignment", "Alignment", kind="choices",
                      options=tuple((rpg.alignment_label({"law": law, "moral": moral}).title(), f"{law} {moral}")
                                    for law, moral in rpg.ALIGNMENTS)),),
        call=lambda hub, v: ((), {"alignment": v["alignment"][0]}),
    )


def _titles(hub) -> "IdleItem | None":
    if hub.char is None:
        return None
    return IdleItem(key="titles", short="🎖️ Titles", label="🎖️ Titles — what you've earned and what's left", command="cmd_title")


def _wear(hub) -> "IdleItem | None":
    char = hub.char
    earned = (char.get("titles") or []) if char is not None else []
    if not earned:
        return None
    options = tuple((rpg.TITLE_NAMES[key], key) for key in earned) + (("None — go by your own name", "none"),)
    return IdleItem(key="wear", short="🎖️ Wear a title", label="🎖️ Wear a title", command="cmd_title", title="Wear a title",
                    fields=(Field("title", "Title", kind="choices", options=options),),
                    call=lambda hub, v: ((), {"which": v["title"][0]}))


def _duel(hub) -> "IdleItem | None":
    if hub.char is None:
        return None
    return IdleItem(
        key="duel", short="🤺 Duel", label="🤺 Duel — challenge a player, once a day", command="cmd_duel", title="Duel", public=True,
        description="fought in the channel; add gold to wager it",
        fields=(_USER, Field("wager", "Gold to wager (optional)", required=False, placeholder="200", max_length=12)),
        call=lambda hub, v: ((_picked_user(v),) + ((v["wager"],) if v.get("wager") else ()), {}),
    )


def _prestige(hub) -> "IdleItem | None":
    char = hub.char
    if char is None or char["level"] < rpg.PRESTIGE_LEVEL:
        return None
    return IdleItem(key="prestige", short="🌟 Prestige", label="🌟 Prestige — start over at level 0 for a ★", command="cmd_prestige",
                    description="confirm in the channel", public=True)


def _leave(hub) -> "IdleItem | None":
    if hub.char is None:
        return None
    return IdleItem(key="leave", short="🪦 Retire", label="🪦 Retire — delete your character for good", command="cmd_leave",
                    description="confirm in the channel", public=True, style=ButtonStyle.danger)


def _world(hub) -> IdleItem:
    return IdleItem(key="world", short="🌍 Realm", label="🌍 The realm today — events, blessings and your boost", command="cmd_world")


def _quest(hub) -> IdleItem:
    return IdleItem(key="quest", short="📜 Quest", label="📜 Quest — the running quest, or what the next one needs", command="cmd_quest")


def _top(hub) -> IdleItem:
    return IdleItem(key="top", short="🏆 Ladder", label="🏆 Ladder — this server's top adventurers", command="cmd_top")


def _bless(hub) -> "IdleItem | None":
    if hub.char is None:
        return None
    return IdleItem(key="bless", short="🕊️ Bless", label="🕊️ Bless the realm — an hour's speed for everyone here",
                    description=f"{rpg.BLESS_COST:,} of your gold · confirm in the channel", command="cmd_bless", public=True)


def _look(hub) -> IdleItem:
    return IdleItem(key="look", short="🔎 Look at a player", label="🔎 Look at a player — their sheet and the map",
                    title="Look at a player", fields=(_USER,), look=True)


def _their_items(hub) -> IdleItem:
    return IdleItem(key="their-items", short="🎒 Their items", label="🎒 A player's items", title="A player's items",
                    command="cmd_items", fields=(_USER,), call=lambda hub, v: ((), {"member": _picked_user(v)}))


def _rules(hub) -> list[IdleItem]:
    from src.cogs.idle_cog import _RULES_TOPICS
    return [IdleItem(key=f"rules-{topic}", short=topic.title(), label=f"📖 {topic.title()}", command="cmd_rules", args=(topic,))
            for topic in _RULES_TOPICS]


def _admin(hub) -> list[IdleItem]:
    return [
        IdleItem(key="hog", short="⚡ Hand of God", label="⚡ Hand of God — strike a character now", command="cmd_admin_hog",
                 title="Hand of God", fields=(_USER,), call=lambda hub, v: ((), {"who": _picked_user(v)})),
        IdleItem(key="gold", short="💰 Gold", label="💰 Gold — give some, or take it with a leading −", command="cmd_admin_gold",
                 title="Gold", fields=(_USER, Field("amount", "Amount", placeholder="500, or -500", max_length=12)),
                 call=lambda hub, v: ((_picked_user(v), v["amount"]), {})),
        IdleItem(key="push", short="⏱️ Push", label="⏱️ Push — move a character's clock", command="cmd_admin_push", title="Push a clock",
                 description="−2h brings the next level sooner, +1d later",
                 fields=(_USER, Field("amount", "Time", placeholder="-2h, +1d", max_length=12)),
                 call=lambda hub, v: ((_picked_user(v), v["amount"]), {})),
        IdleItem(key="move", short="📍 Move", label="📍 Move — set a character down somewhere", command="cmd_admin_move",
                 title="Move a character",
                 fields=(_USER, Field("where", "Place, or x y", placeholder="Velvragh, or 250 250", max_length=40)),
                 call=lambda hub, v: ((_picked_user(v),), {"where": v["where"]})),
        IdleItem(key="remove", short="🗑️ Remove", label="🗑️ Remove — delete a player's character", command="cmd_admin_remove",
                 title="Remove a character", description="confirm in the channel", fields=(_USER,), public=True,
                 style=ButtonStyle.danger, call=lambda hub, v: ((), {"who": _picked_user(v)})),
        IdleItem(key="reset", short="💥 Reset", label="💥 Reset — wipe this server's game", command="cmd_admin_reset",
                 description="confirm in the channel", public=True, style=ButtonStyle.danger),
    ]


def _some(*made) -> list[IdleItem]:
    """The items that apply now, in order; a factory's None is skipped."""
    out = []
    for item in made:
        if isinstance(item, list):
            out.extend(item)
        elif item is not None:
            out.append(item)
    return out


def _common_items(hub) -> list[IdleItem]:
    return _some(_own(hub), _refresh(hub), _join(hub), _items(hub), _store(hub), _gamble(hub), _travel(hub),
                 _hunt(hub), _quest(hub), _top(hub), _prestige(hub))


def _town_items(hub) -> list[IdleItem]:
    # No market in reach: the road there is the one thing to do.
    walk = _travel(hub) if hub.char is not None and rpg.market_in_reach(hub.char) is None else None
    return _some(_shop(hub), _gamble(hub), _hunt(hub), walk, _auto(hub))


def _road_items(hub) -> list[IdleItem]:
    return _some(_travel(hub), _stop(hub), _lore(hub))


def _character_items(hub) -> list[IdleItem]:
    return _some(_items(hub), _align(hub), _titles(hub), _wear(hub), _duel(hub), _prestige(hub), _leave(hub))


def _realm_items(hub) -> list[IdleItem]:
    return _some(_world(hub), _quest(hub), _top(hub), _bless(hub), _look(hub), _their_items(hub))


_BUILDERS = {
    "common": _common_items, "town": _town_items, "road": _road_items, "character": _character_items,
    "realm": _realm_items, "rules": _rules, "admin": _admin,
}
_NEED_CHARACTER = ("town", "road", "character")


def items_for(page: str, hub: "IdleHub") -> list[IdleItem]:
    builder = _BUILDERS.get(page)
    return builder(hub) if builder is not None else []


def pages_for(hub: "IdleHub") -> list[tuple[str, str]]:
    """Common, the realm and the rules for everyone; the town, the road
    and the character once there is one; admin for the `idle admin` tier."""
    has_char = hub.char is not None
    return [(key, label) for key, label in PAGES
            if not (key in _NEED_CHARACTER and not has_char)
            and not (key == "admin" and not permitted_for(hub.ctx, "idle admin"))]


class IdleHub(Panel):
    page_placeholder = "Page…"
    buttons = True
    closed_title = "⚔️ Idle RPG — closed"
    closed_hint = "Run `!idle` to open it again."

    def __init__(self, cog, ctx, page: str = "common"):
        self.cog = cog
        self.target = ctx.author.id   # whose sheet the Common page shows
        self.last = None              # the latest captured reply
        super().__init__(ctx, page)

    @property
    def char(self) -> "dict | None":
        return self.cog._chars(self.ctx.guild.id).get(self.ctx.author.id)

    def pages(self):
        return pages_for(self)

    def items(self):
        return items_for(self.page, self)

    def _head(self) -> str:
        """One line of where the invoker stands, for the pages it matters."""
        char = self.char
        if char is None or char.get("x") is None:
            return ""
        if self.page == "town":
            market = rpg.market_in_reach(char)
            if market is not None:
                return f"The market of **{market}** is open. You have **{char['gold']:,}** gold."
            town, away = rpg.nearest_town(char)
            return (f"No market within {rpg.MARKET_RADIUS} squares — the nearest is **{town}**, {away} squares away. "
                    "Travel walks you there.")
        if self.page == "road":
            going = f"walking to **{char['travel_to']}**" if char.get("travel_to") else "wandering"
            return f"You're at [{char['x']}, {char['y']}], {going}."
        return ""

    def embed(self) -> discord.Embed:
        guild, uid = self.ctx.guild, self.ctx.author.id
        if self.page == "common":
            char = self.cog._chars(guild.id).get(self.target)
            if char is not None:
                embed = self.cog._sheet(guild, self.target, char, int(time.time()))
            elif self.target == uid:
                embed = emb("⚔️ Idle RPG", PITCH, C_BLUE)
            else:
                embed = emb("❌ No Character", f"{self.cog._namer(guild)(self.target)} has no character here.", C_RED)
        else:
            lines = [item.label + (f"\n-# {item.description}" if item.description else "") for item in self.items()]
            head = self._head()
            body = (head + "\n\n" if head else "") + ("\n".join(lines) or "Nothing to do here right now.")
            embed = emb(f"⚔️ Idle RPG — {_PAGE_LABELS[self.page]}", body, C_BLUE)
        if self.page in MAP_PAGES:
            embed.set_image(url=f"attachment://{MAP_FILENAME}")
        if self.last is not None:
            text = self.last.description if isinstance(self.last, discord.Embed) else str(self.last)
            title = self.last.title if isinstance(self.last, discord.Embed) else "Last action"
            embed.add_field(name=title or "Last action", value=(text or "—")[:1024], inline=False)
        embed.set_footer(text="Pick a page, then press a button. Typed commands keep working: !idle status, !travel, !store…")
        return embed

    async def attachments(self) -> list[discord.File]:
        if self.page not in MAP_PAGES:
            return []
        # The road page is about the invoker; the sheet about whoever it shows.
        # The viewer's own route is drawn only on a picture of them (see _map_file).
        shown = self.target if self.page == "common" else self.ctx.author.id
        return [await self.cog._map_file(self.ctx.guild, highlight=(shown,), viewer=self.ctx.author.id)]

    async def on_pick(self, interaction: discord.Interaction, item: IdleItem, values: dict | None) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        if item.look:
            ids = (values or {}).get("user") or []
            self.target = int(ids[0]) if ids else self.ctx.author.id
            self.page = "common"
            await self.refresh(interaction)
            return
        if item.goto:
            self.page = item.goto
            await self.refresh(interaction)
            return
        if not item.command:
            await self.refresh(interaction)
            return
        command = getattr(self.cog, item.command)
        # No level lock: the subcommand `shop`'s name would meet the coin shop's.
        refusal = refusal_for(self.ctx, command, level=False)
        args, kwargs = item.args, dict(item.kwargs or {})
        if refusal is None and item.call is not None:
            try:
                args, kwargs = item.call(self, values or {})
            except ValueError as e:
                refusal = str(e)
        if refusal is not None:
            await interaction.followup.send(refusal, ephemeral=True)
            await self.refresh(interaction)
            return
        if item.public:
            await forward(self.ctx, command, *args, cog=self.cog, **kwargs)
            self.last = emb(item.label, "Posted in the channel.", C_BLUE)
        else:
            captured = CapturingContext(self.ctx)
            await forward(captured, command, *args, cog=self.cog, **kwargs)
            self.last = captured.last or emb(item.label, "Done.", C_BLUE)
            if isinstance(self.last, str):
                self.last = emb(item.label, self.last, C_RED)
        await self.refresh(interaction)
