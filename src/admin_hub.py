"""The `!admin` panel: the moderation, effects, permission, economy and
counter commands an admin otherwise types, as pickers and forms.

Same rules as the settings panel (src/settings_hub.py): the panel never
acts itself. Every pick becomes the typed command — `ban <member> reason`,
`effects <@id> add tax 2h`, `setperm <member> bot_admin`, `counter add afk
…` — resolved through `bot.get_command` and run through `forwarding.forward`
after `refusal_for` has applied the permission and feature gates, so a
server admin who opens the panel sees only what their tier allows and
cannot reach a bot-admin command through it. Replies are captured into the
panel except for the commands whose reply *is* the action (`say`, `event`).

Commands that take a `Member` converter get the member object, not a
mention: a callback called directly never runs discord.py's converters.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import discord

from src import state
from src.forwarding import CapturingContext, forward, refusal_for
from src.helpers import emb, C_GOLD, C_RED
from src.panel import Panel, PanelItem
from src.permissions import permitted_for
from src.settings_views import Field, MAX_OPTIONS

PAGES = (
    ("overview", "📋 Overview"),
    ("moderation", "🔨 Moderation"),
    ("effects", "✨ Effects"),
    ("permissions", "🔑 Permissions & locks"),
    ("economy", "🪙 Economy"),
    ("counters", "🔢 Counters"),
)
_PAGE_LABELS = dict(PAGES)


@dataclass(frozen=True, kw_only=True)
class AdminItem(PanelItem):
    """`command` is a qualified command name. A form's `call(ctx, values)`
    returns `(args, kwargs)` for the callback (raising `ValueError` with a
    message to refuse); a plain item forwards `args` / `kwargs`. `public`
    items keep their reply in the channel."""
    command: str
    args: tuple = ()
    kwargs: dict | None = None
    call: Callable | None = None
    public: bool = False


def _member(ctx, values: dict, key: str = "user"):
    ids = values.get(key) or []
    if not ids:
        raise ValueError("Pick a user first.")
    member = ctx.guild.get_member(int(ids[0]))
    if member is None:
        raise ValueError("That user isn't in this server.")
    return member


def _mention(values: dict, key: str = "user") -> str:
    ids = values.get(key) or []
    if not ids:
        raise ValueError("Pick a user first.")
    return f"<@{ids[0]}>"


_USER = Field("user", "Who?", kind="users")


def _moderation(ctx) -> list[AdminItem]:
    reason = Field("reason", "Reason (optional)", kind="paragraph", required=False, max_length=200)
    return [
        AdminItem(key="ban", label="🔨 Ban a user from the bot (this server)", description="the bot ignores them here",
                  command="ban", title="Ban from the bot", fields=(_USER, reason),
                  call=lambda ctx, v: ((_member(ctx, v),), {"reason": v.get("reason") or None})),
        AdminItem(key="unban", label="🔓 Unban a user (this server)", command="unban", title="Unban",
                  fields=(_USER,), call=lambda ctx, v: ((_member(ctx, v),), {})),
        AdminItem(key="globalban", label="🌐 Global ban — every server", description="bot admins only",
                  command="globalban", title="Global ban", fields=(_USER, reason),
                  call=lambda ctx, v: ((_member(ctx, v),), {"reason": v.get("reason") or None})),
        AdminItem(key="globalunban", label="🌐 Global unban", command="globalunban", title="Global unban",
                  fields=(_USER,), call=lambda ctx, v: ((_member(ctx, v),), {})),
        AdminItem(key="clear", label="🧹 Delete the last N messages here", command="clear", title="Delete messages",
                  fields=(Field("n", "How many?", placeholder="10", max_length=4),), call=lambda ctx, v: ((v["n"],), {})),
        AdminItem(key="say", label="🔊 Say something as the bot", description="posted in this channel", command="say",
                  title="Say", fields=(Field("text", "Text", kind="paragraph", max_length=2000),),
                  call=lambda ctx, v: ((), {"text": v["text"]}), public=True),
        AdminItem(key="audit", label="🔍 Audit — the last failed command attempts", command="audit"),
    ]


def _effects(ctx) -> list[AdminItem]:
    from src.cogs.effects_cog import _EFFECTS  # a cog module; imported late to keep the cogs loading first
    settable = tuple((f"{spec['emoji']} {name} — {spec['desc']}"[:100], name)
                     for name, spec in _EFFECTS.items() if spec.get("admin_settable"))
    every = tuple((f"{spec['emoji']} {name}", name) for name, spec in _EFFECTS.items())
    return [
        AdminItem(key="effects-view", label="✨ See a user's active effects", command="effects", title="Effects on",
                  fields=(_USER,), call=lambda ctx, v: ((_mention(v),), {})),
        AdminItem(key="effects-add", label="✨ Give a user an effect", description="tax or insurance; no duration = permanent",
                  command="effects", title="Give an effect",
                  fields=(_USER, Field("effect", "Effect", kind="choices", options=settable),
                          Field("duration", "Duration (optional)", required=False, placeholder="2h, 3d…", max_length=10)),
                  call=lambda ctx, v: ((_mention(v), "add", v["effect"][0]) + ((v["duration"],) if v.get("duration") else ()), {})),
        AdminItem(key="effects-remove", label="✨ Remove an effect from a user", command="effects", title="Remove an effect",
                  fields=(_USER, Field("effect", "Effect", kind="choices", options=every)),
                  call=lambda ctx, v: ((_mention(v), "remove", v["effect"][0]), {})),
    ]


def _permissions(ctx) -> list[AdminItem]:
    tiers = (("Server admin — settings and moderation here", "server_admin"),
             ("Bot admin — bot-admin commands here", "bot_admin"),
             ("Clear — remove the override", "clear"))
    return [
        AdminItem(key="setperm", label="🔑 Grant or clear a permission override", description="bot admins only",
                  command="setperm", title="Permission override",
                  fields=(_USER, Field("tier", "Tier", kind="choices", options=tiers)),
                  call=lambda ctx, v: ((_member(ctx, v), v["tier"][0]), {})),
        AdminItem(key="unlock-channel", label="🔓 Unlock a shop-locked channel", command="adminunlock", title="Unlock a channel",
                  fields=(Field("channel", "Channel", kind="channels"),),
                  call=lambda ctx, v: ((f"<#{v['channel'][0]}>",), {}) if v.get("channel") else (_ for _ in ()).throw(ValueError("Pick a channel first."))),
        AdminItem(key="unlock-role", label="🔓 Unlock a shop-locked role", command="adminunlock", title="Unlock a role",
                  fields=(Field("role", "Role", kind="roles"),),
                  call=lambda ctx, v: ((f"<@&{v['role'][0]}>",), {}) if v.get("role") else (_ for _ in ()).throw(ValueError("Pick a role first."))),
        AdminItem(key="counter-addperm", label="🔢 Let a user write counters", command="counter addperm", title="Counter writer",
                  fields=(_USER,), call=lambda ctx, v: ((), {"member": _member(ctx, v)})),
        AdminItem(key="counter-removeperm", label="🔢 Take counter writing away", command="counter removeperm", title="Counter writer",
                  fields=(_USER,), call=lambda ctx, v: ((), {"member": _member(ctx, v)})),
        AdminItem(key="counter-perms", label="🔢 Who may write counters", command="counter perms"),
    ]


def _economy(ctx) -> list[AdminItem]:
    amount = Field("amount", "Amount", placeholder="5k", max_length=12)
    return [
        AdminItem(key="admingive", label="🪙 Give or take coins", description="bot admins only; a negative amount takes",
                  command="admingive", title="Give coins", fields=(_USER, amount),
                  call=lambda ctx, v: ((_member(ctx, v), v["amount"]), {})),
        AdminItem(key="admingivexp", label="✨ Give XP", command="admingivexp", title="Give XP", fields=(_USER, amount),
                  call=lambda ctx, v: ((_member(ctx, v), v["amount"]), {})),
        AdminItem(key="adminjailbreak", label="🚔 Free a user from jail", command="adminjailbreak", title="Free from jail",
                  fields=(_USER,), call=lambda ctx, v: ((_member(ctx, v),), {})),
        AdminItem(key="godmode", label="🕊️ Toggle godmode — free costs", description="bot admins only", command="godmode",
                  title="Godmode", fields=(Field("user", "Who? (empty = you)", kind="users", required=False),),
                  call=lambda ctx, v: (((_member(ctx, v),) if v.get("user") else ()), {})),
        AdminItem(key="event", label="🪙 Start a coin event here", description="posts the event in this channel",
                  command="event", title="Coin event",
                  fields=(amount, Field("hours", "Hours (optional)", required=False, placeholder="1", max_length=5)),
                  call=lambda ctx, v: ((v["amount"],) + ((v["hours"],) if v.get("hours") else ()), {}), public=True),
    ]


def _counters(ctx) -> list[AdminItem]:
    existing = state.counters.get(ctx.guild.id, {}) if ctx.guild else {}
    items = [
        AdminItem(key="counter-add", label="🔢 Add a counter", description="a custom tally: !count <name> @user 3",
                  command="counter add", title="Add a counter",
                  fields=(Field("name", "Name", placeholder="afk", max_length=32),
                          Field("description", "Description", max_length=100)),
                  call=lambda ctx, v: ((v["name"],), {"description": v["description"]})),
        AdminItem(key="counter-list", label="🔢 List the counters", command="counter list"),
    ]
    if existing:
        items.append(AdminItem(key="counter-remove", label="🔢 Remove a counter", command="counter remove", title="Remove a counter",
                               fields=(Field("name", "Counter", kind="choices",
                                             options=tuple((n, n) for n in list(existing)[:MAX_OPTIONS])),),
                               call=lambda ctx, v: ((v["name"][0],), {})))
    return items


_BUILDERS = {
    "moderation": _moderation, "effects": _effects, "permissions": _permissions,
    "economy": _economy, "counters": _counters,
}


def items_for(page: str, ctx) -> list[AdminItem]:
    """The page's items the invoker's tier allows — a server admin never
    sees the bot-admin rows."""
    builder = _BUILDERS.get(page)
    if builder is None:
        return []
    return [item for item in builder(ctx) if permitted_for(ctx, item.command)]


def pages_for(ctx) -> list[tuple[str, str]]:
    return [(key, label) for key, label in PAGES if key == "overview" or items_for(key, ctx)]


class AdminHub(Panel):
    item_placeholder = "Do…"
    closed_title = "🛠️ Admin — closed"
    closed_hint = "Run `!admin` to open it again."

    def __init__(self, cog, ctx, page: str = "overview"):
        self.cog = cog
        self.last = None  # the latest captured reply
        super().__init__(ctx, page)

    def pages(self):
        return pages_for(self.ctx)

    def items(self):
        return items_for(self.page, self.ctx)

    def embed(self) -> discord.Embed:
        if self.page == "overview":
            lines = [f"{label} — {len(items_for(key, self.ctx))} actions" for key, label in self.pages() if key != "overview"]
            embed = emb("🛠️ Admin", "\n".join(lines) or "Nothing here for your tier.", C_GOLD)
        else:
            lines = [f"{item.label}" + (f"\n-# {item.description}" if item.description else "") for item in self.items()]
            embed = emb(f"🛠️ Admin — {_PAGE_LABELS[self.page]}", "\n".join(lines) or "Nothing here for your tier.", C_GOLD)
        if self.last is not None:
            text = self.last.description if isinstance(self.last, discord.Embed) else str(self.last)
            title = self.last.title if isinstance(self.last, discord.Embed) else "Result"
            embed.add_field(name=title or "Result", value=(text or "—")[:1024], inline=False)
        embed.set_footer(text="Pick a section, then an action. Typed commands keep working.")
        return embed

    async def on_pick(self, interaction: discord.Interaction, item: AdminItem, values: dict | None) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        bot = getattr(self.cog, "bot", None)
        command = bot.get_command(item.command) if bot is not None else None
        refusal = None
        if command is None:
            refusal = "That command isn't available right now."
        else:
            refusal = refusal_for(self.ctx, command, level=False)
        args, kwargs = item.args, dict(item.kwargs or {})
        if refusal is None and item.call is not None:
            try:
                args, kwargs = item.call(self.ctx, values or {})
            except ValueError as e:
                refusal = str(e)
        if refusal is not None:
            await interaction.followup.send(refusal, ephemeral=True)
            await self.refresh(interaction)
            return
        if item.public:
            await forward(self.ctx, command, *args, cog=self.cog, **kwargs)
            self.last = emb(item.label, "Done — posted in the channel.", C_GOLD)
        else:
            captured = CapturingContext(self.ctx)
            await forward(captured, command, *args, cog=self.cog, **kwargs)
            self.last = captured.last or emb(item.label, "Done.", C_GOLD)
            if isinstance(self.last, str):
                self.last = emb(item.label, self.last, C_RED)
        await self.refresh(interaction)
