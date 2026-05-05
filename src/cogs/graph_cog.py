import logging

import discord
from discord.ext import commands, tasks

from src.helpers import emb, C_GOLD
from src.permissions import requires_perm
from src.economy import snapshot_all
from src.graph_series import (
    find_spec, parse_tokens, render_combined, render_ai_uptime_strip,
    parse_admin_tokens, build_admin_series, SeriesSpec,
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

    # Build each series. Pass kwargs based on what the spec accepts.
    serieses = []
    for spec in parsed.specs:
        kwargs = {}
        args = []
        if spec.accepts_member:
            args.append(parsed.member)
        if spec.accepts_guild:
            args.append(parsed.guild_id)
        data = await spec.build(*args, **kwargs)
        serieses.append(data)

    # If there's no data at all, bail with a friendly note.
    if all(not s.x_points for s in serieses):
        await ctx.send(embed=emb("📊 No Data", "No history recorded yet — data is captured every 30 minutes.", C_GOLD))
        return
    if all(len(s.x_points) < 2 for s in serieses):
        await ctx.send(embed=emb("📊 Not Enough Data", "Need at least 2 data points to draw a graph.", C_GOLD))
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
    elif len(parsed.specs) == 1 and parsed.specs[0].name == "crime":
        title = f"{parsed.member.display_name}'s Crime — Last 2 Weeks"
    elif len(parsed.specs) == 1 and parsed.specs[0].name == "gambling":
        title = f"{parsed.member.display_name}'s Gambling P/L — Last 2 Weeks"
    elif len(parsed.specs) == 1 and parsed.specs[0].name == "levels":
        title = f"{parsed.member.display_name}'s Level-ups — Last 2 Weeks"
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

    @tasks.loop(minutes=30)
    async def _snapshot_loop(self):
        """Refresh the snapshot-style graph data (balances, bot stats, per-cog
        command usage) every 30 minutes. Each tick UPSERTs the (date, bucket)
        row for the CURRENT 6h CT bucket — ticks within the same bucket
        overwrite the same row, while bucket boundaries (00, 06, 12, 18 CT)
        roll to a fresh row, freezing the previous bucket's value as a
        permanent data point on the chart (4 points/day).

        First tick fires ~immediately on boot, doubling as a recovery path
        for any downtime that crossed a tick.

        Atomic-write series (crime / gambling / level-ups) are NOT touched
        here — they persist per-event and read directly from disk.
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
            "`!graph crime [@user]` — Coins gained/lost via !steal and !mug\n"
            "`!graph gambling [@user]` — Net P/L from games and gambling\n"
            "`!graph levels [@user]` — Level-ups per day in this server\n"
            "`!graph commands` — Command usage by category over the last 2 weeks\n"
            "`!graph server` — Daily message and command counts over the last 2 weeks\n"
            "`!graph memory` — Bot memory usage (MB) over the last 2 weeks\n"
            "`!graph ai` — Daily AI response count and uptime over the last 2 weeks\n"
            "`!graph admin <wallet|savings> [N|@users…]` — per-user breakout (bot admin)\n"
            "\n**Combine compatible graphs** by listing multiple names:\n"
            "`!graph balance crime [@user]` — overlay coins-group graphs\n"
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

    @cmd_graph.command(name="crime")
    @requires_perm
    async def cmd_graph_crime(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("crime"))

    @cmd_graph.command(name="gambling", aliases=["gamble", "games", "game"])
    @requires_perm
    async def cmd_graph_gambling(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("gambling"))

    @cmd_graph.command(name="levels", aliases=["level", "lvl"])
    @requires_perm
    async def cmd_graph_levels(self, ctx: commands.Context, *tokens: str):
        await _build_and_render(ctx, tokens, find_spec("levels"))

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

    # ── Admin: per-user breakouts ────────────────────────────────────────
    # bot_admin tier — see src/command_perms.json. Renders one line per user
    # to inspect economy distribution. Tokens are either an integer N (top
    # N by current value) or one or more @user mentions, never both.

    @cmd_graph.group(name="admin", invoke_without_command=True)
    @requires_perm
    async def cmd_graph_admin(self, ctx: commands.Context):
        await ctx.send(embed=emb(
            "📊 Graph — Admin",
            "**Subcommands:**\n"
            "`!graph admin wallet [N|@users…]` — per-user wallet, top N (default 10) "
            "or specific users\n"
            "`!graph admin savings [N|@users…]` — per-user savings, same shape",
            C_GOLD,
        ))

    @cmd_graph_admin.command(name="wallet")
    @requires_perm
    async def cmd_graph_admin_wallet(self, ctx: commands.Context, *tokens: str):
        await _admin_handler(ctx, tokens, field="wallet")

    @cmd_graph_admin.command(name="savings")
    @requires_perm
    async def cmd_graph_admin_savings(self, ctx: commands.Context, *tokens: str):
        await _admin_handler(ctx, tokens, field="savings")


async def _admin_handler(ctx, tokens: tuple[str, ...], *, field: str):
    """Shared body for `!graph admin wallet` and `!graph admin savings`."""
    parsed = await parse_admin_tokens(ctx, tokens)
    if parsed.error:
        await ctx.send(embed=emb("📊 Invalid Arguments", parsed.error, C_GOLD))
        return

    data = await build_admin_series(field, top_n=parsed.top_n, members=parsed.members or None)

    if not data.x_points or not data.segments:
        await ctx.send(embed=emb(
            "📊 No Data",
            "No balance history yet — data is captured every 30 minutes.",
            C_GOLD,
        ))
        return
    if len(data.x_points) < 2:
        await ctx.send(embed=emb(
            "📊 Not Enough Data",
            "Need at least 2 data points to draw a graph.",
            C_GOLD,
        ))
        return

    if parsed.members:
        names = ", ".join(m.display_name for m in parsed.members)
        title = f"{names} — {field.capitalize()} — Last 2 Weeks"
    else:
        title = f"Top {parsed.top_n} {field.capitalize()}s — Last 2 Weeks"

    buf = await render_combined(
        [data], "coins", "🪙 Coins", title,
    )
    await ctx.send(file=discord.File(buf, filename=f"admin_{field}.png"))


async def setup(bot):
    await bot.add_cog(GraphCog(bot))
