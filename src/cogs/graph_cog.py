import io
import datetime

import discord
from discord.ext import commands

from src.helpers import emb, C_GOLD, get_memory_mb
from src.permissions import check_command_permission
from src.persistence import load_balance_history, load_bot_stats_history
from src.economy import _ensure_user, get_balance, _ct_now
from src import state


def _sorted_dates(history: dict, limit: int = 14) -> list[str]:
    """Return up to `limit` most recent date keys from history, sorted ascending."""
    dates = sorted(history.keys())
    return dates[-limit:]


def _render_graph(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    return buf


def _fmt_date(d: datetime.date) -> str:
    return f"{d.strftime('%b')} {d.day}"


class GraphCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.group(name="graph", invoke_without_command=True)
    async def cmd_graph(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return
        await ctx.send(embed=emb(
            "📊 Graph",
            "**Subcommands:**\n"
            "`!graph balance [@user]` — Wallet balance over the last 2 weeks\n"
            "`!graph totalbalance` — Total economy (wallet + savings) over the last 2 weeks\n"
            "`!graph server` — Daily message and command counts over the last 2 weeks\n"
            "`!graph memory` — Bot memory usage (MB) over the last 2 weeks\n"
            "`!graph ai` — Daily AI response count and uptime over the last 2 weeks",
            C_GOLD,
        ))

    @cmd_graph.command(name="balance", aliases=["bal"])
    async def cmd_graph_balance(self, ctx: commands.Context, target: discord.Member = None):
        if not await check_command_permission(ctx):
            return

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        member = target or ctx.author
        uid_str = str(member.id)
        _ensure_user(member.id)

        history = load_balance_history()
        dates = _sorted_dates(history)

        if not dates:
            await ctx.send(embed=emb("📊 No Data", "No balance history recorded yet — data is captured once per day at 5am CT.", C_GOLD))
            return

        # Build series — include today's live value as the rightmost point
        x_dates = []
        y_wallet = []
        for d in dates:
            snap = history[d].get(uid_str)
            if snap is not None:
                x_dates.append(datetime.date.fromisoformat(d))
                y_wallet.append(snap["wallet"])

        # Append today's live balance
        today = _ct_now().date()
        live_wallet = get_balance(member.id)
        if not x_dates or x_dates[-1] != today:
            x_dates.append(today)
            y_wallet.append(live_wallet)

        if len(x_dates) < 2:
            await ctx.send(embed=emb("📊 Not Enough Data", "Need at least 2 days of history to draw a graph.", C_GOLD))
            return

        fig, ax = plt.subplots(figsize=(9, 4))
        fig.patch.set_facecolor("#2f3136")
        ax.set_facecolor("#36393f")

        ax.plot(x_dates, y_wallet, color="#2ecc71", linewidth=2, marker="o", markersize=4, label="Wallet")
        ax.fill_between(x_dates, y_wallet, alpha=0.15, color="#2ecc71")

        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_date(mdates.num2date(v).date())))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        fig.autofmt_xdate(rotation=35, ha="right")

        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
        ax.tick_params(colors="#dcddde", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#4f545c")
        ax.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)

        ax.set_title(f"{member.display_name}'s Balance — Last 2 Weeks", color="#ffffff", fontsize=12, pad=10)
        ax.set_ylabel("🪙 Coins", color="#b9bbbe", fontsize=9)
        ax.legend(facecolor="#2f3136", edgecolor="#4f545c", labelcolor="#dcddde", fontsize=8)

        buf = _render_graph(fig)
        plt.close(fig)
        await ctx.send(file=discord.File(buf, filename="balance.png"))

    @cmd_graph.command(name="totalbalance", aliases=["total", "eco"])
    async def cmd_graph_totalbalance(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import time as _time

        history = load_balance_history()
        dates = _sorted_dates(history)

        if not dates:
            await ctx.send(embed=emb("📊 No Data", "No balance history recorded yet — data is captured once per day at 5am CT.", C_GOLD))
            return

        x_dates = []
        y_wallet = []
        y_savings = []
        for d in dates:
            snap = history[d]
            total_w = sum(u["wallet"] for u in snap.values())
            total_s = sum(u["savings"] for u in snap.values())
            x_dates.append(datetime.date.fromisoformat(d))
            y_wallet.append(total_w)
            y_savings.append(total_s)

        # Append today's live totals
        today = _ct_now().date()
        now = _time.time()
        live_wallet = sum(u.get("balance", 0) for u in state.economy["users"].values())
        live_savings = int(sum(
            e["amount"] * (1.01 ** ((now - e["deposited_at"]) / 86400.0))
            for u in state.economy["users"].values()
            for e in u.get("savings", [])
        ))
        if not x_dates or x_dates[-1] != today:
            x_dates.append(today)
            y_wallet.append(live_wallet)
            y_savings.append(live_savings)

        if len(x_dates) < 2:
            await ctx.send(embed=emb("📊 Not Enough Data", "Need at least 2 days of history to draw a graph.", C_GOLD))
            return

        y_total = [w + s for w, s in zip(y_wallet, y_savings)]

        fig, ax = plt.subplots(figsize=(9, 4))
        fig.patch.set_facecolor("#2f3136")
        ax.set_facecolor("#36393f")

        ax.plot(x_dates, y_wallet, color="#2ecc71", linewidth=2, marker="o", markersize=4, label="Wallets")
        ax.plot(x_dates, y_savings, color="#9b59b6", linewidth=2, marker="o", markersize=4, label="Savings")
        ax.plot(x_dates, y_total, color="#f1c40f", linewidth=2, linestyle="--", marker="s", markersize=4, label="Total")

        ax.fill_between(x_dates, y_wallet, alpha=0.10, color="#2ecc71")
        ax.fill_between(x_dates, y_savings, alpha=0.10, color="#9b59b6")

        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_date(mdates.num2date(v).date())))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        fig.autofmt_xdate(rotation=35, ha="right")

        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
        ax.tick_params(colors="#dcddde", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#4f545c")
        ax.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)

        ax.set_title("Total Economy Balance — Last 2 Weeks", color="#ffffff", fontsize=12, pad=10)
        ax.set_ylabel("🪙 Coins", color="#b9bbbe", fontsize=9)
        ax.legend(facecolor="#2f3136", edgecolor="#4f545c", labelcolor="#dcddde", fontsize=8)

        buf = _render_graph(fig)
        plt.close(fig)
        await ctx.send(file=discord.File(buf, filename="total_balance.png"))


    @cmd_graph.command(name="server", aliases=["srv"])
    async def cmd_graph_server(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        history = load_bot_stats_history()
        dates = _sorted_dates(history)

        if not dates:
            await ctx.send(embed=emb("📊 No Data", "No server stats recorded yet — data is captured once per day at 5am CT.", C_GOLD))
            return

        x_dates = []
        y_messages = []
        y_commands = []
        for d in dates:
            snap = history[d]
            x_dates.append(datetime.date.fromisoformat(d))
            y_messages.append(snap.get("messages", 0))
            y_commands.append(snap.get("commands", 0))

        # Append today's live counts
        today = _ct_now().date()
        if not x_dates or x_dates[-1] != today:
            x_dates.append(today)
            y_messages.append(state.stats_messages_today)
            y_commands.append(state.stats_commands_today)

        if len(x_dates) < 2:
            await ctx.send(embed=emb("📊 Not Enough Data", "Need at least 2 days of history to draw a graph.", C_GOLD))
            return

        fig, ax = plt.subplots(figsize=(9, 4))
        fig.patch.set_facecolor("#2f3136")
        ax.set_facecolor("#36393f")

        ax.plot(x_dates, y_messages, color="#3498db", linewidth=2, marker="o", markersize=4, label="Messages")
        ax.plot(x_dates, y_commands, color="#e67e22", linewidth=2, marker="o", markersize=4, label="Commands")
        ax.fill_between(x_dates, y_messages, alpha=0.10, color="#3498db")
        ax.fill_between(x_dates, y_commands, alpha=0.10, color="#e67e22")

        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_date(mdates.num2date(v).date())))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        fig.autofmt_xdate(rotation=35, ha="right")

        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
        ax.tick_params(colors="#dcddde", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#4f545c")
        ax.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)

        ax.set_title("Server Activity — Last 2 Weeks", color="#ffffff", fontsize=12, pad=10)
        ax.set_ylabel("Count", color="#b9bbbe", fontsize=9)
        ax.legend(facecolor="#2f3136", edgecolor="#4f545c", labelcolor="#dcddde", fontsize=8)

        buf = _render_graph(fig)
        plt.close(fig)
        await ctx.send(file=discord.File(buf, filename="server_activity.png"))

    @cmd_graph.command(name="memory", aliases=["mem", "ram"])
    async def cmd_graph_memory(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        history = load_bot_stats_history()
        dates = _sorted_dates(history)

        if not dates:
            await ctx.send(embed=emb("📊 No Data", "No memory stats recorded yet — data is captured once per day at 5am CT.", C_GOLD))
            return

        x_dates = []
        y_mem = []
        for d in dates:
            snap = history[d]
            mb = snap.get("memory_mb", 0)
            if mb > 0:
                x_dates.append(datetime.date.fromisoformat(d))
                y_mem.append(mb)

        # Append live reading
        today = _ct_now().date()
        live_mem = get_memory_mb()
        if live_mem > 0 and (not x_dates or x_dates[-1] != today):
            x_dates.append(today)
            y_mem.append(live_mem)

        if len(x_dates) < 2:
            await ctx.send(embed=emb("📊 Not Enough Data", "Need at least 2 days of memory data. Memory readings are only available on Linux.", C_GOLD))
            return

        fig, ax = plt.subplots(figsize=(9, 4))
        fig.patch.set_facecolor("#2f3136")
        ax.set_facecolor("#36393f")

        ax.plot(x_dates, y_mem, color="#e74c3c", linewidth=2, marker="o", markersize=4, label="RSS Memory")
        ax.fill_between(x_dates, y_mem, alpha=0.15, color="#e74c3c")

        ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: _fmt_date(mdates.num2date(v).date())))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        fig.autofmt_xdate(rotation=35, ha="right")

        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f} MB"))
        ax.tick_params(colors="#dcddde", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#4f545c")
        ax.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)

        ax.set_title("Bot Memory Usage — Last 2 Weeks", color="#ffffff", fontsize=12, pad=10)
        ax.set_ylabel("MB", color="#b9bbbe", fontsize=9)
        ax.legend(facecolor="#2f3136", edgecolor="#4f545c", labelcolor="#dcddde", fontsize=8)

        buf = _render_graph(fig)
        plt.close(fig)
        await ctx.send(file=discord.File(buf, filename="memory.png"))

    @cmd_graph.command(name="ai")
    async def cmd_graph_ai(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        history = load_bot_stats_history()
        dates = _sorted_dates(history)

        if not dates:
            await ctx.send(embed=emb("📊 No Data", "No AI stats recorded yet — data is captured once per day at 5am CT.", C_GOLD))
            return

        x_dates = []
        y_responses = []
        ai_up_flags = []
        for d in dates:
            snap = history[d]
            x_dates.append(datetime.date.fromisoformat(d))
            y_responses.append(snap.get("ai_responses", 0))
            ai_up_flags.append(snap.get("ai_up", False))

        # Append today's live counts
        today = _ct_now().date()
        if not x_dates or x_dates[-1] != today:
            x_dates.append(today)
            y_responses.append(state.stats_ai_responses_today)
            ai_up_flags.append(None)  # unknown until reset probes Ollama

        if len(x_dates) < 2:
            await ctx.send(embed=emb("📊 Not Enough Data", "Need at least 2 days of history to draw a graph.", C_GOLD))
            return

        fig, (ax_bar, ax_line) = plt.subplots(2, 1, figsize=(9, 6), gridspec_kw={"height_ratios": [3, 1]})
        fig.patch.set_facecolor("#2f3136")
        for ax in (ax_bar, ax_line):
            ax.set_facecolor("#36393f")

        # Bar chart: AI responses per day
        x_nums = list(range(len(x_dates)))
        bar_colors = []
        for flag in ai_up_flags:
            if flag is True:
                bar_colors.append("#2ecc71")
            elif flag is False:
                bar_colors.append("#e74c3c")
            else:
                bar_colors.append("#95a5a6")  # today / unknown
        ax_bar.bar(x_nums, y_responses, color=bar_colors, alpha=0.85, width=0.6)
        ax_bar.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{int(v):,}"))
        ax_bar.set_xticks(x_nums)
        ax_bar.set_xticklabels([_fmt_date(d) for d in x_dates], rotation=35, ha="right", fontsize=8)
        ax_bar.tick_params(colors="#dcddde", labelsize=8)
        for spine in ax_bar.spines.values():
            spine.set_edgecolor("#4f545c")
        ax_bar.grid(axis="y", color="#4f545c", linestyle="--", linewidth=0.5, alpha=0.7)
        ax_bar.set_title("AI Activity — Last 2 Weeks", color="#ffffff", fontsize=12, pad=10)
        ax_bar.set_ylabel("Responses", color="#b9bbbe", fontsize=9)

        # Legend patches for up/down/unknown
        from matplotlib.patches import Patch
        legend_elements = [
            Patch(facecolor="#2ecc71", label="AI up"),
            Patch(facecolor="#e74c3c", label="AI down"),
            Patch(facecolor="#95a5a6", label="Unknown"),
        ]
        ax_bar.legend(handles=legend_elements, facecolor="#2f3136", edgecolor="#4f545c", labelcolor="#dcddde", fontsize=8)

        # Uptime strip: green/red/grey blocks per day
        for i, flag in enumerate(ai_up_flags):
            color = "#2ecc71" if flag is True else ("#e74c3c" if flag is False else "#95a5a6")
            ax_line.barh(0, 1, left=i, color=color, height=1, alpha=0.85)
        ax_line.set_xlim(0, len(x_dates))
        ax_line.set_ylim(-0.5, 0.5)
        ax_line.set_xticks([])
        ax_line.set_yticks([])
        ax_line.set_ylabel("Uptime", color="#b9bbbe", fontsize=8)
        for spine in ax_line.spines.values():
            spine.set_edgecolor("#4f545c")

        fig.tight_layout(pad=1.5)
        buf = _render_graph(fig)
        plt.close(fig)
        await ctx.send(file=discord.File(buf, filename="ai_activity.png"))


async def setup(bot):
    await bot.add_cog(GraphCog(bot))
