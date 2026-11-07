"""The `!graph` panel: one dropdown of every graph, the per-user breakouts
included for bot admins.

A pick with parameters opens a modal: whose graph, which same-axis graphs
to draw alongside it, or a breakout's top-N / users. A pick without any
draws at once. Either way it forwards to the `!graph <name>` subcommand
itself (`forwarding`), so the rendering, the group rules and the permission
tiers are the typed command's. The rendered image posts publicly, as a
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
    ("wallet", "👥 Wallets per user", "one line per user — top N, or the users you pick"),
    ("savings", "👥 Savings per user", "one line per user"),
    ("assets", "👥 Property value per user", "one line per user"),
    ("total", "👥 Total per user", "wallet + savings + property, one line per user"),
)
_LABELS = {name: label for name, label, _ in GRAPHS}


@dataclass(frozen=True, kw_only=True)
class GraphItem(PanelItem):
    command: str  # the qualified `graph …` command


def _graph_fields(name: str) -> tuple:
    """A graph's modal: the same-axis graphs it can be drawn with, and a
    user when it or any of those is per-user. Empty for a graph with
    neither, which then draws on the pick."""
    spec = find_spec(name)
    if spec is None:
        return ()
    partners = [s for s in (find_spec(n) for n, _, _ in GRAPHS)
                if s is not None and s.group == spec.group and s.name != spec.name]
    fields = []
    if partners:
        fields.append(Field("with", "Draw it with (optional)", kind="choices", required=False,
                            options=tuple((_LABELS[s.name], s.name) for s in partners),
                            max_values=len(partners), placeholder="Just this one"))
    if spec.accepts_member or any(s.accepts_member for s in partners):
        fields.append(Field("user", "Whose? (empty = you)", kind="users", required=False,
                            description=None if spec.accepts_member else "For the per-user graphs you add"))
    return tuple(fields)


def items_for(ctx) -> list[GraphItem]:
    in_guild = ctx.guild is not None
    items = []
    for name, label, description in GRAPHS:
        spec = find_spec(name)
        if spec is not None and spec.accepts_guild and not in_guild:
            continue  # per-server graphs can't draw in a DM
        items.append(GraphItem(key=name, label=label, description=description, command=f"graph {name}",
                               fields=_graph_fields(name), title=f"{label} graph"[:45]))
    if is_admin(ctx):
        items += [GraphItem(key=f"admin:{field}", label=label, description=description, command=f"graph admin {field}",
                            title=label[:45],
                            fields=(Field("n", "Top N (default 10)", required=False, placeholder="10", max_length=3),
                                    Field("users", "…or specific users", kind="users", required=False, max_values=10)))
                  for field, label, description in BREAKOUTS]
    return items


class GraphHub(Panel):
    item_placeholder = "Pick a graph…"
    page_select = False
    closed_title = "📊 Graph — closed"
    closed_hint = "Run `!graph` to open it again."

    def __init__(self, cog, ctx):
        self.cog = cog
        super().__init__(ctx, "graphs")

    def pages(self):
        return [("graphs", "📊 Graphs")]

    def items(self):
        return items_for(self.ctx)

    def embed(self) -> discord.Embed:
        lines = [f"{item.label} — {item.description}" for item in self.items()]
        note = "Graphs with options ask for them: whose, and what to draw alongside. Empty means you, on its own."
        embed = emb("📊 Graph", "\n".join(lines) + f"\n\n*{note}*", C_GOLD)
        embed.set_footer(text="Typed: !graph <name> [@user], !graph balance crime to combine.")
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
        args: list[str] = list(values.get("with") or [])
        if values.get("n"):
            args.append(values["n"])
        args += [f"<@{uid}>" for uid in (values.get("user") or []) + (values.get("users") or [])]
        await forward(self.ctx, command, *args, cog=self.cog)
        await self.refresh(interaction)
