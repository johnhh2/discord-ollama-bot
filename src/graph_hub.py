"""The `!graph` panel: every graph as a dropdown pick, the per-user
breakouts as a second page for bot admins.

A pick forwards to the `!graph <name>` subcommand itself (`forwarding`), so
the rendering, the group rules and the permission tiers are the typed
command's. Graphs that take a user open a user picker first; the breakouts
take a top-N or specific users. The rendered image posts publicly, as a
typed `!graph` does; the panel stays open for the next pick.
"""
from __future__ import annotations

from dataclasses import dataclass

import discord

from src.forwarding import forward, refusal_for
from src.graph_series import find_spec
from src.helpers import emb, C_GOLD
from src.panel import Panel, PanelItem
from src.permissions import is_admin
from src.settings_views import Field

PAGES = (("graphs", "📊 Graphs"), ("admin", "🔑 Per-user breakouts"))
_PAGE_LABELS = dict(PAGES)

# (graph name, label, description) — the names are graph_series.REGISTRY's.
GRAPHS = (
    ("balance", "💰 Balance", "wallet balance over the last 2 weeks"),
    ("economy", "🏦 Economy", "total economy — wallet + savings + property"),
    ("assets", "🏘️ Assets", "property portfolio value and lifetime revenue"),
    ("crime", "🔫 Crime", "coins gained and lost via !steal and !mug"),
    ("gambling", "🎰 Gambling", "net profit and loss from games and gambling"),
    ("levels", "📈 Levels", "level-ups per day in this server"),
    ("commands", "⌨️ Commands", "command usage by category"),
    ("server", "💬 Server", "daily message and command counts"),
    ("ai", "🤖 AI", "daily AI response count and uptime"),
    ("memory", "🧠 Memory", "bot memory usage in MB"),
    ("ping", "📡 Ping", "Discord gateway ping in ms"),
    ("minecraft", "⛏️ Minecraft", "server ping and daily players"),
)
BREAKOUTS = (
    ("wallet", "💰 Wallets", "one line per user — top N, or the users you pick"),
    ("savings", "🐷 Savings", "one line per user"),
    ("assets", "🏘️ Property value", "one line per user"),
    ("total", "🏦 Total", "wallet + savings + property, one line per user"),
)


@dataclass(frozen=True, kw_only=True)
class GraphItem(PanelItem):
    command: str  # the qualified `graph …` command


def items_for(page: str, ctx) -> list[GraphItem]:
    if page == "graphs":
        items = []
        for name, label, description in GRAPHS:
            spec = find_spec(name)
            fields = ()
            if spec is not None and spec.accepts_member:
                fields = (Field("user", "Whose? (empty = you)", kind="users", required=False),)
            items.append(GraphItem(key=name, label=label, description=description, command=f"graph {name}",
                                   fields=fields, title=f"{label} graph"[:45]))
        return items
    if page == "admin":
        return [GraphItem(key=f"admin:{field}", label=label, description=description, command=f"graph admin {field}",
                          title=f"{label} breakout"[:45],
                          fields=(Field("n", "Top N (default 10)", required=False, placeholder="10", max_length=3),
                                  Field("users", "…or specific users", kind="users", required=False, max_values=10)))
                for field, label, description in BREAKOUTS]
    return []


class GraphHub(Panel):
    item_placeholder = "Draw…"
    closed_title = "📊 Graph — closed"
    closed_hint = "Run `!graph` to open it again."

    def __init__(self, cog, ctx, page: str = "graphs"):
        self.cog = cog
        super().__init__(ctx, page)

    def pages(self):
        return [(k, label) for k, label in PAGES if k == "graphs" or is_admin(self.ctx)]

    def items(self):
        return items_for(self.page, self.ctx)

    def embed(self) -> discord.Embed:
        lines = [f"{item.label} — {item.description}" for item in self.items()]
        note = ("Graphs that take a user ask for one; empty means you." if self.page == "graphs"
                else "Leave both empty for the top 10.")
        embed = emb(f"📊 Graph — {_PAGE_LABELS[self.page]}", "\n".join(lines) + f"\n\n*{note}*", C_GOLD)
        embed.set_footer(text="Pick a graph to draw it here. Typed: !graph <name> [@user], !graph balance crime to combine.")
        return embed

    async def on_pick(self, interaction: discord.Interaction, item: GraphItem, values: dict | None) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        bot = getattr(self.cog, "bot", None)
        command = bot.get_command(item.command) if bot is not None else None
        refusal = "That graph isn't available right now." if command is None else refusal_for(self.ctx, command, level=False)
        if refusal:
            await interaction.followup.send(refusal, ephemeral=True)
            return
        values = values or {}
        args: list[str] = []
        if values.get("n"):
            args.append(values["n"])
        args += [f"<@{uid}>" for uid in (values.get("user") or []) + (values.get("users") or [])]
        await forward(self.ctx, command, *args, cog=self.cog)
        await self.refresh(interaction)
