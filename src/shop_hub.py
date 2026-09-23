"""The `!shop` panel: one message, a page dropdown, an item dropdown, and
nothing typed after the command.

Same idea as src/settings_hub.py. The panel never charges: every pick is
turned into the typed form of the matching command — `shop mock <@id>`,
`shop nickname <@id> Name`, `shop insurance standard 3`, `artifacts buy 3`,
`assets buy lemonade_stand` — and forwarded through `ShopCog.forward`, so
the subcommand's own lottery-channel guard, per-item disable check,
insurance protection, confirm prompt and race-safe claim-before-charge all
run exactly as they would for a typed command. Unlike the settings panel the
replies are *not* captured: a mock activation, a ragebait opener, a confirm
prompt and a record announcement are public events and post to the channel
as they do today. The panel just rebuilds itself afterwards.

The gates a typed command meets in `process_commands` — permission tier,
feature switch, level lock — are `bot.check`s, which a forward skips, so
`ShopCog.forward` applies them itself and the panel answers a refusal
privately.

Two kinds of pick: a *buy* applies at once (the command's confirm prompt
follows); a *form* opens a modal — a user picker for the target, a dropdown
of the bot's own roles or channels (the only ones the shop may touch), a
text box for a name, colour, topic, price or bounty condition — whose
submit applies. `items_for` is the one catalog; a new shop item gets an
entry there and nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import discord
from discord import ui

from src import state
from src.artifacts import ARTIFACTS, owned_qty
from src.config import (
    SHOP_NICKNAME_SELF_COST, SHOP_NICKNAME_REMOVE_COST, SHOP_NICKNAME_OTHER_COST,
    SHOP_ROLE_CREATE_COST, SHOP_ROLE_ASSIGN_COST, SHOP_ROLE_REMOVE_COST, SHOP_ROLE_DELETE_COST, SHOP_ROLE_MOVE_COST,
    SHOP_ROLECOLOR_COST, SHOP_ROLECHANNEL_COST, SHOP_LOCK_COST, SHOP_RENAME_COST, SHOP_CHANNEL_COST,
    SHOP_CHANNEL_DELETE_COST, SHOP_TAX_COST, SHOP_INSURANCE_TIERS, SHOP_INSURANCE_MAX_DAYS,
    SHOP_MOCK_COST, SHOP_RAGEBAIT_COST, SHOP_MUTE_COST, SHOP_CURSE_COST, SHOP_UNOREVERSE_COST,
    SHOP_MOCK_MESSAGES, SHOP_RAGEBAIT_MESSAGES, SHOP_CURSE_MESSAGES, SHOP_MUTE_MINUTES, SHOP_TAX_PER_MESSAGE,
    SHOP_XP_COST_PER_XP, BOUNTY_MIN_AMOUNT,
)
from src.economy import get_insurance_tier, get_insurance_expiry
from src.features import feature_enabled
from src.guild_config import get_guild_cfg
from src.helpers import emb, C_PURPLE, C_GREY
from src.level_unlocks import is_locked_for, user_global_display_level
from src.properties import (
    PROPERTIES, PROPERTY_MAX_OWNED, PROPERTY_UPGRADES, PROPERTY_BANK_BUYBACK_PCT, PROPERTY_DAILY_REVENUE_PCT,
    owned_properties, property_daily_revenue, property_value,
)
from src.settings_views import Field, FormModal, MAX_OPTIONS, _OwnedView

HUB_TIMEOUT = 300.0
DESCRIPTION_MAX = 100  # a select option's description

PAGES = (
    ("overview", "📋 Overview"),
    ("nicknames", "🎭 Nicknames"),
    ("roles", "👑 Roles"),
    ("channels", "📢 Channels"),
    ("fun", "🎉 Fun & Social"),
    ("insurance", "🛡️ Insurance"),
    ("leveling", "✨ Leveling"),
    ("artifacts", "🏺 Artifacts"),
    ("assets_low", "🏘️ Assets — tiers 1–2"),
    ("assets_high", "🏘️ Assets — tiers 3–5"),
    ("portfolio", "🏘️ My properties"),
    ("bounties", "🎯 Bounties"),
)
_PAGE_LABELS = dict(PAGES)

# Shop items whose section is a page, by the shop_items key each needs.
_PAGE_ITEM_KEYS = {
    "nicknames": ("nickname",),
    "roles": ("createrole", "assignrole", "unassignrole", "deleterole", "roleup", "roledown", "rolecolor", "renamerole", "lockrole"),
    "channels": ("channel", "renamechannel", "lockchannel", "rolechannel"),
}


@dataclass(frozen=True)
class ShopItem:
    """One pick in the item dropdown. A buy forwards `args` / `kwargs`; a
    form opens `fields` and forwards `call(values)` → `(args, kwargs)`.
    `method` is a ShopCog attribute, or `bot:<qualified name>` for another
    cog's command. `lock` is the level the item unlocks at when the invoker
    is below it — listed, marked, and refused on a pick."""
    key: str
    label: str
    description: str
    method: str
    args: tuple = ()
    kwargs: dict | None = None
    fields: tuple = ()
    call: Callable[[dict], tuple[tuple, dict]] | None = None
    title: str = ""
    invoked_with: str | None = None
    lock: int | None = None

    @property
    def is_form(self) -> bool:
        return bool(self.fields)


def _coins(n: int) -> str:
    return f"{n:,} 🪙"


def _mention(values: dict, key: str) -> str | None:
    ids = values.get(key) or []
    return f"<@{ids[0]}>" if ids else None


def _role_mention(values: dict, key: str) -> str:
    picked = values.get(key) or []
    return f"<@&{picked[0]}>" if picked else ""


def _channel_mention(values: dict, key: str) -> str:
    picked = values.get(key) or []
    return f"<#{picked[0]}>" if picked else ""


def _bot_roles(guild) -> list:
    return [r for r in getattr(guild, "roles", []) if r.id in state.bot_roles]


def _bot_channels(guild) -> list:
    ids = get_guild_cfg(guild.id).get("bot_channels", [])
    return [c for c in (guild.get_channel(cid) for cid in ids) if c is not None]


def _pick_field(key: str, label: str, things: list, kind: str, *, required: bool = True) -> Field:
    """A dropdown of the bot's own roles / channels; Discord's picker past
    the 25-option cap (the command still refuses anything not the bot's)."""
    if 0 < len(things) <= MAX_OPTIONS:
        return Field(key, label, kind="choices", required=required,
                     options=tuple((getattr(t, "name", str(t.id))[:100], str(t.id)) for t in things))
    return Field(key, label, kind=kind, required=required)


def _picked_id(values: dict, key: str) -> str:
    """The id a `_pick_field` submitted, whichever component it was."""
    picked = values.get(key) or []
    return str(picked[0]) if picked else ""


def _locked(ctx, name: str) -> int | None:
    return is_locked_for(name, ctx.author.id, ctx.guild.id) if ctx.guild else None


# ── pages ────────────────────────────────────────────────────────────────────

def _nicknames(ctx, shop_items: dict) -> list[ShopItem]:
    return [
        ShopItem("nickname", f"🎭 Change a nickname — {_coins(SHOP_NICKNAME_SELF_COST)} yours / {_coins(SHOP_NICKNAME_OTHER_COST)} someone else's",
                 "pick a user for theirs, or leave it empty for your own", "shop_nickname", title="Change a nickname",
                 fields=(Field("user", "Whose? (empty = you)", kind="users", required=False),
                         Field("name", "New nickname", max_length=32)),
                 call=lambda v: (tuple(x for x in (_mention(v, "user"), v["name"]) if x), {}),
                 lock=_locked(ctx, "nickname")),
        ShopItem("removenickname", f"🎭 Remove your nickname — {_coins(SHOP_NICKNAME_REMOVE_COST)}",
                 "back to your username", "shop_removenickname", lock=_locked(ctx, "removenickname")),
    ]


def _roles(ctx, shop_items: dict) -> list[ShopItem]:
    roles = _bot_roles(ctx.guild)
    role = _pick_field("role", "Which role?", roles, "roles")
    have = "pick one of the bot's roles" if roles else "no bot-created roles here yet"
    items = []

    def add(key, enabled_key, label, description, fields, call, *, method=None, invoked_with=None):
        if enabled_key and not shop_items.get(enabled_key, True):
            return
        items.append(ShopItem(key, label, description, method or f"shop_{key}", title=label.split(" — ")[0][2:],
                              fields=fields, call=call, invoked_with=invoked_with, lock=_locked(ctx, key)))

    add("rolecreate", "createrole", f"👑 Create a role — {_coins(SHOP_ROLE_CREATE_COST)}", "a new role, given to whoever you pick",
        (Field("user", "Who gets it?", kind="users"), Field("name", "Role name", max_length=100)),
        lambda v: ((_mention(v, "user"), v["name"]), {}), method="shop_createrole")
    add("roleassign", "assignrole", f"👑 Assign a role — {_coins(SHOP_ROLE_ASSIGN_COST)}", have,
        (Field("user", "Who gets it?", kind="users"), role),
        lambda v: ((_mention(v, "user"), _role_mention_from(v)), {}), method="shop_assignrole")
    add("roleunassign", "unassignrole", f"👑 Remove a role — {_coins(SHOP_ROLE_REMOVE_COST)}", "from yourself, or from someone you pick",
        (Field("user", "From whom? (empty = you)", kind="users", required=False), role),
        lambda v: (tuple(x for x in (_mention(v, "user"), _role_mention_from(v)) if x), {}), method="shop_unassignrole")
    add("roledelete", "deleterole", f"👑 Delete a role — {_coins(SHOP_ROLE_DELETE_COST)}", have, (role,),
        lambda v: ((_role_mention_from(v),), {}), method="shop_deleterole")
    add("roleup", "roleup", f"👑 Move a role up — {_coins(SHOP_ROLE_MOVE_COST)}", have, (role,),
        lambda v: ((_role_mention_from(v),), {}), method="shop_roleup", invoked_with="roleup")
    add("roledown", "roledown", f"👑 Move a role down — {_coins(SHOP_ROLE_MOVE_COST)}", have, (role,),
        lambda v: ((_role_mention_from(v),), {}), method="shop_roleup", invoked_with="roledown")
    add("rolecolor", "rolecolor", f"👑 Recolour a role — {_coins(SHOP_ROLECOLOR_COST)}", "a hex code like #FF0000, or a colour name",
        (role, Field("color", "Colour", placeholder="#FF0000", max_length=32)),
        lambda v: ((_role_mention_from(v), v["color"]), {}), method="shop_rolecolor")
    add("rolerename", "renamerole", f"👑 Rename a role — {_coins(SHOP_RENAME_COST)}", have,
        (role, Field("name", "New name", max_length=100)),
        lambda v: ((_role_mention_from(v), "|", v["name"]), {}), method="shop_renamerole")
    add("rolelock", "lockrole", f"👑 Lock a role — {_coins(SHOP_LOCK_COST)}", "only you can change it afterwards", (role,),
        lambda v: ((_role_mention_from(v),), {}), method="shop_lockrole")
    add("roleunlock", None, "👑 Unlock a role", "lock owner only", (role,),
        lambda v: ((_role_mention_from(v),), {}), method="shop_unlockrole")
    return items


def _role_mention_from(values: dict) -> str:
    picked = _picked_id(values, "role")
    return f"<@&{picked}>" if picked else ""


def _channels(ctx, shop_items: dict) -> list[ShopItem]:
    channels = _bot_channels(ctx.guild)
    channel = _pick_field("channel", "Which channel?", channels, "channels")
    have = "pick one of the bot's channels" if channels else "no bot-created channels here yet"
    roles = _bot_roles(ctx.guild)
    items = []

    def add(key, enabled_key, label, description, fields, call, *, method):
        if enabled_key and not shop_items.get(enabled_key, True):
            return
        items.append(ShopItem(key, label, description, method, title=label.split(" — ")[0][2:],
                              fields=fields, call=call, lock=_locked(ctx, key)))

    def ch(v):
        picked = _picked_id(v, "channel")
        return f"<#{picked}>" if picked else ""

    add("channelcreate", "channel", f"📢 Create a channel — {_coins(SHOP_CHANNEL_COST)}", "a new text channel",
        (Field("name", "Channel name", max_length=100),), lambda v: ((v["name"],), {}), method="shop_createchannel")
    add("channeldelete", "channel", f"📢 Delete a channel — {_coins(SHOP_CHANNEL_DELETE_COST)}", have, (channel,),
        lambda v: ((ch(v),), {}), method="shop_deletechannel")
    add("channelrename", "renamechannel", f"📢 Rename a channel — {_coins(SHOP_RENAME_COST)}", have,
        (channel, Field("name", "New name", max_length=100)), lambda v: ((ch(v), v["name"]), {}), method="shop_renamechannel")
    add("channellock", "lockchannel", f"📢 Lock a channel — {_coins(SHOP_LOCK_COST)}", "only you can change it afterwards", (channel,),
        lambda v: ((ch(v),), {}), method="shop_lockchannel")
    add("channelunlock", None, "📢 Unlock a channel", "lock owner only", (channel,),
        lambda v: ((ch(v),), {}), method="shop_unlockchannel")
    add("rolechannel", "rolechannel", f"📢 Restrict a channel to a role — {_coins(SHOP_ROLECHANNEL_COST)}", have,
        (_pick_field("role", "Which role?", roles, "roles"), channel),
        lambda v: ((_role_mention_from(v), ch(v)), {}), method="shop_rolechannel")
    return items


def _fun(ctx, shop_items: dict) -> list[ShopItem]:
    user = Field("user", "Who?", kind="users")

    def target(v):
        return ((_mention(v, "user"),), {})
    items = [
        ShopItem("mock", f"🎭 Mock someone — {_coins(SHOP_MOCK_COST)}", f"their next {SHOP_MOCK_MESSAGES} messages, mocked",
                 "shop_mock", title="Mock someone", fields=(user,), call=target),
        ShopItem("tax", f"💰 Tax someone — {_coins(SHOP_TAX_COST)}", f"they owe you {SHOP_TAX_PER_MESSAGE:,} 🪙 per message for 24h",
                 "shop_tax", title="Tax someone", fields=(user,), call=target),
        ShopItem("curse", f"🔮 Curse someone — {_coins(SHOP_CURSE_COST)}", f"their next {SHOP_CURSE_MESSAGES} messages, cursed",
                 "shop_curse", title="Curse someone", fields=(user,), call=target),
        ShopItem("mute", f"🔇 Mute someone — {_coins(SHOP_MUTE_COST)}", f"a server mute for {SHOP_MUTE_MINUTES} minutes",
                 "shop_mute", title="Mute someone", fields=(user,), call=target),
        ShopItem("unoreverse", f"🔄 Uno reverse — {_coins(SHOP_UNOREVERSE_COST)}", "redirect an active mock, ragebait or curse onto someone else",
                 "shop_unoreverse", title="Uno reverse", fields=(user,), call=target, lock=_locked(ctx, "unoreverse")),
    ]
    if shop_items.get("ragebait", True) and feature_enabled(ctx.guild.id, "ai"):
        items.append(ShopItem("ragebait", f"🎣 Ragebait someone — {_coins(SHOP_RAGEBAIT_COST)}", f"for {SHOP_RAGEBAIT_MESSAGES + 1} messages, on a topic if you like",
                              "shop_ragebait", title="Ragebait someone",
                              fields=(user, Field("topic", "Topic (optional)", required=False, max_length=200)),
                              call=lambda v: (tuple(x for x in (_mention(v, "user"), v.get("topic")) if x), {})))
    for word, emoji in get_guild_cfg(ctx.guild.id).get("tax_aliases", {}).items():
        items.append(ShopItem(f"tax:{word}", f"{emoji} {word.capitalize()} tax — {_coins(SHOP_TAX_COST)}",
                              f"a tax announced as the {word} tax", "shop_tax", title=f"{word.capitalize()} tax",
                              fields=(user,), call=target, invoked_with=word))
    return items[:MAX_OPTIONS]


def _insurance(ctx, shop_items: dict) -> list[ShopItem]:
    uid = ctx.author.id
    tier = get_insurance_tier(uid)
    exp = get_insurance_expiry(uid)
    status = f"{tier} until <t:{exp}:R>" if exp else "no coverage"
    tiers = tuple((f"{name.title()} — {info['cost']:,} 🪙/day, refunds {info['refund_pct']}%", name)
                  for name, info in SHOP_INSURANCE_TIERS.items())
    tier_field = Field("tier", "Tier", kind="choices", options=tiers, defaults=(tier,) if tier else ())
    items = [
        ShopItem("insurance", "🛡️ Prepay insurance — from " + _coins(min(i["cost"] for i in SHOP_INSURANCE_TIERS.values())) + "/day",
                 f"now: {status}", "shop_insurance", title="Prepay insurance",
                 fields=(tier_field, Field("days", f"Days (1–{SHOP_INSURANCE_MAX_DAYS})", default="1", max_length=3)),
                 call=lambda v: ((v["tier"][0], v["days"]), {})),
        ShopItem("insurance-sub", "🛡️ Subscribe — charged daily at 5am CT",
                 "subscribed at " + state.insurance_subs[uid] if uid in state.insurance_subs else "not subscribed",
                 "shop_insurance", title="Subscribe to insurance", fields=(tier_field,),
                 call=lambda v: ((v["tier"][0], "sub"), {})),
    ]
    if uid in state.insurance_subs:
        items.append(ShopItem("insurance-unsub", "🛡️ Unsubscribe", "coverage already paid for stays", "shop_insurance", args=("unsub",)))
    return items


def _leveling(ctx, shop_items: dict) -> list[ShopItem]:
    return [
        ShopItem("buyxp", f"✨ Buy your next level — {SHOP_XP_COST_PER_XP} 🪙/XP", "quoted and confirmed before you pay",
                 "shop_buyxp"),
        ShopItem("buyxp-to", "✨ Buy every level up to…", "quoted and confirmed before you pay", "shop_buyxp",
                 title="Buy levels", fields=(Field("level", "Target level", max_length=4),),
                 call=lambda v: (("lvl", v["level"]), {})),
    ]


def _artifacts(ctx, shop_items: dict) -> list[ShopItem]:
    uid = ctx.author.id
    lvl = user_global_display_level(uid)
    items = []
    for i, art in enumerate(ARTIFACTS, start=1):
        req = art.get("level", 1)
        if owned_qty(uid, art["id"]) >= art["max"]:
            status = "✅ owned"
        elif lvl < req:
            status = f"🔒 global level {req}"
        else:
            status = "permanent, yours forever"
        items.append(ShopItem(f"artifact:{i}", f"🏺 {i}. {art['effect'][:70]} — {_coins(art['cost'])}", status,
                              "shop_artifacts", args=("buy", str(i))))
    return items[:MAX_OPTIONS]


def _assets(ctx, tiers: tuple[int, ...]) -> list[ShopItem]:
    uid = ctx.author.id
    lvl = user_global_display_level(uid)
    items = []
    for p in PROPERTIES:
        if p["tier"] not in tiers:
            continue
        row = state.property_owners.get(p["id"])
        rev = property_daily_revenue(p["id"], row)
        if row is None:
            price, status = p["cost"], f"tier {p['tier']} · {rev:,} 🪙/day" + (f" · 🔒 global level {p['level']}" if lvl < p["level"] else "")
        elif row["owner_id"] == uid:
            price, status = None, f"✅ yours · {rev:,} 🪙/day"
        elif row.get("list_price"):
            price, status = row["list_price"], f"🏷️ listed by a player · {rev:,} 🪙/day"
        else:
            price, status = None, "🔒 owned by someone else"
        label = f"{p['emoji']} {p['name']}" + (f" — {_coins(price)}" if price else "")
        items.append(ShopItem(f"asset:{p['id']}", label, status, "bot:assets buy", kwargs={"name": p["id"]}))
    return items[:MAX_OPTIONS]


def _portfolio(ctx) -> list[ShopItem]:
    uid = ctx.author.id
    items = []
    for p in owned_properties(uid):
        pid = p["id"]
        row = state.property_owners.get(pid, {})
        name = f"{p['emoji']} {row.get('custom_name') or p['name']}"
        value = property_value(pid, row)
        items.append(ShopItem(f"sell:{pid}", f"🏷️ Sell {name}", f"value {value:,} 🪙 · at ≤{PROPERTY_BANK_BUYBACK_PCT}% the bank offers an instant buyback",
                              "bot:assets sell", title=f"Sell {p['name']}"[:45],
                              fields=(Field("price", "Asking price", placeholder="e.g. 50k", max_length=12),),
                              call=lambda v, pid=pid: ((pid, v["price"]), {})))
        if not row.get("upgraded"):
            up_name, up_cost, up_boost = PROPERTY_UPGRADES[pid]
            items.append(ShopItem(f"upgrade:{pid}", f"⭐ Build the {up_name} on {name} — {_coins(up_cost)}",
                                  f"+{up_boost}% revenue", "bot:assets upgrade", kwargs={"name": pid}))
        if row.get("list_price"):
            items.append(ShopItem(f"unlist:{pid}", f"🏷️ Unlist {name}", f"listed at {row['list_price']:,} 🪙",
                                  "bot:assets unlist", kwargs={"name": pid}))
        items.append(ShopItem(f"rename:{pid}", f"✏️ Rename {name}", "1–48 characters", "bot:assets rename",
                              title=f"Rename {p['name']}"[:45], fields=(Field("name", "New name", max_length=48),),
                              call=lambda v, pid=pid: ((pid, *v["name"].split()), {})))
    return items[:MAX_OPTIONS]


def _bounties(ctx) -> list[ShopItem]:
    return [
        ShopItem("bounty", f"🎯 Post a bounty — {_coins(BOUNTY_MIN_AMOUNT)} minimum", "escrowed from you until someone claims it",
                 "shop_bounty", title="Post a bounty",
                 fields=(Field("coins", "Reward", placeholder="5k", max_length=12),
                         Field("duration", "Duration (optional)", required=False, placeholder="7d", max_length=8),
                         Field("condition", "What has to be done?", kind="paragraph", max_length=400)),
                 call=lambda v: (tuple(x for x in (v["coins"], v.get("duration")) if x) + tuple(v["condition"].split()), {})),
        ShopItem("bounties", "🎯 Open bounties", "this server's open bounties and their rewards", "bot:bounties"),
    ]


def pages_for(ctx) -> list[tuple[str, str]]:
    """The pages this invoker can see: a section hides when its feature is
    off, every one of its shop items is disabled, or (bounties) no bounty
    channel is set."""
    gid = ctx.guild.id
    cfg = get_guild_cfg(gid)
    shop_items = cfg.get("shop_items", {})
    out = []
    for key, label in PAGES:
        needs = _PAGE_ITEM_KEYS.get(key)
        if needs and not any(shop_items.get(k, True) for k in needs):
            continue
        if key == "leveling" and not shop_items.get("buyxp", True):
            continue
        if key == "artifacts" and not feature_enabled(gid, "artifacts"):
            continue
        if key in ("assets_low", "assets_high", "portfolio") and not feature_enabled(gid, "assets"):
            continue
        if key == "portfolio" and not owned_properties(ctx.author.id):
            continue
        if key == "bounties" and not cfg.get("bounty_channel"):
            continue
        out.append((key, label))
    return out


def items_for(page: str, ctx) -> list[ShopItem]:
    """What the item dropdown offers on `page`, with prices and status."""
    shop_items = get_guild_cfg(ctx.guild.id).get("shop_items", {})
    if page == "nicknames":
        return _nicknames(ctx, shop_items)
    if page == "roles":
        return _roles(ctx, shop_items)
    if page == "channels":
        return _channels(ctx, shop_items)
    if page == "fun":
        return _fun(ctx, shop_items)
    if page == "insurance":
        return _insurance(ctx, shop_items)
    if page == "leveling":
        return _leveling(ctx, shop_items)
    if page == "artifacts":
        return _artifacts(ctx, shop_items)
    if page == "assets_low":
        return _assets(ctx, (1, 2))
    if page == "assets_high":
        return _assets(ctx, (3, 4, 5))
    if page == "portfolio":
        return _portfolio(ctx)
    if page == "bounties":
        return _bounties(ctx)
    return []


_PAGE_NOTES = {
    "assets_low": f"Every property is unique — one owner across all servers. Own up to {PROPERTY_MAX_OWNED}; "
                  f"revenue is {PROPERTY_DAILY_REVENUE_PCT} of price per day, banked with your daily claim.",
    "assets_high": f"Every property is unique — one owner across all servers. Own up to {PROPERTY_MAX_OWNED}; "
                   f"revenue is {PROPERTY_DAILY_REVENUE_PCT} of price per day, banked with your daily claim.",
    "artifacts": "Permanent per-user upgrades, owned bot-wide. Level gates are your global level.",
    "insurance": "Crime still goes through; the insurer refunds your wallet. Every tier also blocks mock, ragebait, curses, mutes, nickname changes, role assignments and tax.",
}


# ── the view ─────────────────────────────────────────────────────────────────

class _PageSelect(ui.Select):
    def __init__(self, pages: list[tuple[str, str]], current: str):
        super().__init__(
            placeholder="Section…", min_values=1, max_values=1, row=0,
            options=[discord.SelectOption(label=label, value=key, default=key == current) for key, label in pages],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.view.show(interaction, self.values[0])


class _ItemSelect(ui.Select):
    def __init__(self, items: list[ShopItem]):
        self.items = {item.key: item for item in items[:MAX_OPTIONS]}
        super().__init__(
            placeholder="Buy or do…", min_values=1, max_values=1, row=1,
            options=[discord.SelectOption(label=item.label[:100], value=item.key,
                                          description=(f"🔒 level {item.lock} · " if item.lock else "") + item.description[:DESCRIPTION_MAX] or None)
                     for item in self.items.values()],
        )

    async def callback(self, interaction: discord.Interaction):
        hub: ShopHub = self.view  # type: ignore[assignment]
        item = self.items[self.values[0]]
        if item.lock:
            await interaction.response.send_message(f"🔒 That unlocks at **level {item.lock}** in this server.", ephemeral=True)
            return
        if item.is_form:
            async def _submitted(submit: discord.Interaction, values: dict):
                args, kwargs = item.call(values)
                await hub.apply(submit, item, args, kwargs)
            # A modal is the only reply a pick can open, so no defer first.
            await interaction.response.send_modal(FormModal(item.title or item.label, item.fields, on_submit=_submitted))
            return
        await hub.apply(interaction, item, item.args, item.kwargs or {})


class _CloseButton(ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, True, "closed")


class ShopHub(_OwnedView):
    """Invoker-only. `overview` renders the read-only overview embed for the
    first page; the others list their items and take picks."""

    def __init__(self, cog, ctx, page: str, overview, *, timeout: float = HUB_TIMEOUT):
        super().__init__(ctx.author.id, timeout)
        self.cog, self.ctx, self.page, self.overview = cog, ctx, page, overview
        self._build()

    def items(self) -> list[ShopItem]:
        return items_for(self.page, self.ctx)

    def _build(self) -> None:
        self.clear_items()
        pages = pages_for(self.ctx)
        if self.page not in dict(pages):
            self.page = "overview"
        self.add_item(_PageSelect(pages, self.page))
        items = self.items()
        if items:
            self.add_item(_ItemSelect(items))
        self.add_item(_CloseButton())

    def embed(self) -> discord.Embed:
        if self.page == "overview":
            embed = self.overview(self.ctx) or emb("🛒 Shop", "No shop items are currently available.", C_PURPLE)
        else:
            lines = [f"{item.label}" + (f" 🔒 **Lvl {item.lock}**" if item.lock else "") + f"\n-# {item.description}"
                     for item in self.items()]
            body = "\n".join(lines) or "Nothing here for you right now."
            if self.page in _PAGE_NOTES:
                body += "\n\n*" + _PAGE_NOTES[self.page] + "*"
            embed = emb(f"🛒 Shop — {_PAGE_LABELS[self.page]}", body[:4000], C_PURPLE)
        embed.set_footer(text="Pick a section, then an item. Every purchase still asks you to confirm.")
        return embed

    async def show(self, interaction: discord.Interaction, page: str) -> None:
        self.page = page
        await self.refresh(interaction)

    async def apply(self, interaction: discord.Interaction, item: ShopItem, args: tuple, kwargs: dict) -> None:
        """Run the item's command in its typed form; its replies (confirm
        prompt, result) post to the channel as usual."""
        if not interaction.response.is_done():
            await interaction.response.defer()
        refusal = await self.cog.forward(self.ctx, item.method, *args, invoked_with=item.invoked_with, **kwargs)
        if refusal:
            await interaction.followup.send(refusal, ephemeral=True)
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        try:
            await interaction.edit_original_response(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass  # the panel may be gone (closed, timed out); the purchase stands


async def open_shop_hub(ctx, cog, *, page: str = "overview", overview, ephemeral: bool = False) -> None:
    """Post the panel and wait it out. The message is deleted when it closes."""
    hub = ShopHub(cog, ctx, page, overview)
    kwargs = {"ephemeral": True} if ephemeral else {}
    msg = await ctx.send(embed=hub.embed(), view=hub, **kwargs)
    await hub.wait()
    try:
        await msg.delete()
    except discord.HTTPException:
        try:
            await msg.edit(embed=emb("🛒 Shop — closed", "Run `!shop` to open it again.", C_GREY), view=None)
        except discord.HTTPException:
            pass  # cosmetic — every purchase already posted its own result
