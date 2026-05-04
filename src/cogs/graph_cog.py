import logging

import discord
from discord.ext import commands, tasks

from src.helpers import emb, C_GOLD
from src.permissions import requires_perm
from src.economy import snapshot_all
from src.graph_series import (
    find_spec, parse_tokens, render_combined, render_ai_uptime_strip,
    SeriesSpec,
)


# Title prefix used for combined renders.
def _combined_title(specs, group: str) -> str:
    if len(specs) == 1:
        return f"{specs[0].name.capitalize()} — Last 2 Weeks"
    names = " + ".join(s.name for s in specs)
    return f"{names} — Last 2 Weeks"


async def _build_and_render(ctx, tokens: tuple[str, ...], entry_spec: SeriesSpec):
    """Shared subcommand body. Resolves tokens (entry_spec is always
    prepended), validates grouping, builds each series, renders, sends.
    """
    # Always include the entry alias so e.g. `!graph balance economy` works
    # whether the user typed `balance` first or `economy` first. We dedupe in
    # parse_tokens.
    full_tokens = (entry_spec.name,) + tuple(tokens)

    parsed = await parse_tokens(ctx, full_tokens)
    if parsed.error:
        await ctx.send(embed=emb("📊 Invalid Combination", parsed.error, C_GOLD))
        return

    # Build each series.
    serieses = []
    for spec in parsed.specs:
        if spec.accepts_member:
            data = await spec.build(parsed.member)
        else:
            data = await spec.build()
        serieses.append(data)

    # If there's no data at all, bail with a friendly note.
    if all(not s.x_dates for s in serieses):
        await ctx.send(embed=emb("📊 No Data", "No history recorded yet — data is captured every 6 hours.", C_GOLD))
        return
    if all(len(s.x_dates) < 2 for s in serieses):
        await ctx.send(embed=emb("📊 Not Enough Data", "Need at least 2 days of history to draw a graph.", C_GOLD))
        return

    group = parsed.specs[0].group  # all the same after validation
    y_unit = parsed.specs[0].y_unit_label

    # Special case: solo `ai` keeps its uptime strip layout.
    if len(parsed.specs) == 1 and parsed.specs[0].name == "ai":
        buf = render_ai_uptime_strip(serieses[0])
        await ctx.send(file=discord.File(buf, filename="ai_activity.png"))
        return

    title = _combined_title(parsed.specs, group)
    if len(parsed.specs) == 1 and parsed.specs[0].name == "balance":
        title = f"{parsed.member.display_name}'s Balance — Last 2 Weeks"
    elif len(parsed.specs) == 1 and parsed.specs[0].name == "economy":
        title = "Total Economy Balance — Last 2 Weeks"
    elif len(parsed.specs) == 1 and parsed.specs[0].name == "commands":
        title = "Command Usage by Category — Last 2 Weeks"
    elif len(parsed.specs) == 1 and parsed.specs[0].name == "server":
        title = "Server Activity — Last 2 Weeks"
    elif len(parsed.specs) == 1 and parsed.specs[0].name == "memory":
        title = "Bot Memory Usage — Last 2 Weeks"

    buf = await render_combined(serieses, group, y_unit, title)
    filename = "_".join(s.name for s in parsed.specs) + ".png"
    await ctx.send(file=discord.File(buf, filename=filename))


class GraphCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._snapshot_loop.start()

    def cog_unload(self):
        self._snapshot_loop.cancel()

    @tasks.loop(hours=6)
    async def _snapshot_loop(self):
        """Capture graph data (balances, bot stats, per-cog command usage) every
        6 hours. Each snapshot upserts the row keyed by `_ct_today()`, so multiple
        ticks within the same gameplay-day refresh the same row with running totals.

        The first tick fires ~immediately on boot, which doubles as a recovery
        path for any time the bot was offline at the previous expected tick.
        Final-of-day capture is handled by `do_daily_reset` at 5am CT (the loop
        cadence isn't aligned to the gameplay-day boundary).
        """
        try:
            await snapshot_all()
        except Exception:
            logging.exception("[graph] snapshot loop failed")

    @_snapshot_loop.before_loop
    async def _wait_for_ready(self):
        await self.bot.wait_until_ready()

    @commands.group(name="graph", invoke_without_command=True)
    @requires_perm
    async def cmd_graph(self, ctx: commands.Context):
        await ctx.send(embed=emb(
            "📊 Graph",
            "**Subcommands:**\n"
            "`!graph balance [@user]` — Wallet balance over the last 2 weeks\n"
            "`!graph economy` — Total economy (wallet + savings) over the last 2 weeks\n"
            "`!graph commands` — Command usage by category over the last 2 weeks\n"
            "`!graph server` — Daily message and command counts over the last 2 weeks\n"
            "`!graph memory` — Bot memory usage (MB) over the last 2 weeks\n"
            "`!graph ai` — Daily AI response count and uptime over the last 2 weeks\n"
            "\n**Combine compatible graphs** by listing multiple names:\n"
            "`!graph balance economy [@user]` — overlay coins-group graphs\n"
            "`!graph commands server ai` — grouped stacked bars for counts-group graphs\n"
            "Coins, counts, and MB graphs cannot be mixed (different y-axes).",
            C_GOLD,
        ))

    @cmd_graph.command(name="balance", aliases=["bal"])
    @requires_perm
    async def cmd_graph_balance(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("balance"))

    @cmd_graph.command(name="economy", aliases=["total", "eco", "totalbalance"])
    @requires_perm
    async def cmd_graph_economy(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("economy"))

    @cmd_graph.command(name="commands", aliases=["cmd", "cmds"])
    @requires_perm
    async def cmd_graph_commands(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("commands"))

    @cmd_graph.command(name="server", aliases=["srv"])
    @requires_perm
    async def cmd_graph_server(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("server"))

    @cmd_graph.command(name="memory", aliases=["mem", "ram"])
    @requires_perm
    async def cmd_graph_memory(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("memory"))

    @cmd_graph.command(name="ai")
    @requires_perm
    async def cmd_graph_ai(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("ai"))


async def setup(bot):
    await bot.add_cog(GraphCog(bot))
