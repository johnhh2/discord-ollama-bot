"""The rankings row under `!leaderboard`, `!levels`, `!records` and `!roles`:
one button per board, in `BOARDS` order on every one of them, then Server /
Global for the boards that have both, and the records section dropdown on
top. The current board and scope are disabled blue buttons, so the row
doubles as the label.

A board is offered only where its command would run: `disabled_feature_for`
its command, and the Idle RPG only once its channel is set. Anyone may
press; the embeds are public and read nothing of the presser's. The row
strips itself on timeout.
"""
from __future__ import annotations

import discord
from discord import ui

from src.features import disabled_feature_for
from src.guild_config import get_guild_cfg

RANKINGS_TIMEOUT = 300.0

# (key, label, the command whose feature switch gates it)
BOARDS = (
    ("coins", "🪙 Coins", "leaderboard"),
    ("levels", "📊 Levels", "levels"),
    ("records", "🏆 Records", "records"),
    ("idle", "⚔️ Idle RPG", "leaderboard"),
    ("roles", "👑 Roles", "roles"),
)
SCOPED = ("coins", "records")  # the boards with a Server / Global choice
SCOPES = (("Server", "server"), ("Global", "global"))


def boards_for(guild_id: int) -> list[tuple[str, str]]:
    out = []
    for key, label, command in BOARDS:
        if disabled_feature_for(command, guild_id) is not None:
            continue
        if key == "idle" and not get_guild_cfg(guild_id).get("idle_channel"):
            continue
        out.append((key, label))
    return out


async def render(owner, ctx, board: str, scope: str, section: str) -> discord.Embed:
    """Each board's own embed builder, on the cog that owns it: `owner` (the
    invoking command's cog) when it's that one, else looked up on the bot."""
    def cog(name: str):
        return owner if type(owner).__name__ == name else owner.bot.get_cog(name)
    if board == "levels":
        return await cog("LevelingCog").levels_embed(ctx)
    if board == "roles":
        return cog("ShopCog").roles_embed(ctx)
    economy = cog("EconomyCog")
    if board == "records":
        return await economy._records_embed(ctx, scope, section)
    if board == "idle":
        return await economy._idle_leaderboard(ctx)
    return await economy._leaderboard_embed(ctx, scope)


class _Button(ui.Button):
    def __init__(self, label: str, current: bool, row: int, on_press):
        super().__init__(label=label, style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary,
                         disabled=current, row=row)
        self.on_press = on_press

    async def callback(self, interaction: discord.Interaction):
        await self.on_press(interaction)


class _SectionSelect(ui.Select):
    def __init__(self, sections, current: str):
        super().__init__(placeholder="Section…", min_values=1, max_values=1, row=0,
                         options=[discord.SelectOption(label=label, value=key, default=key == current)
                                  for key, label in sections])

    async def callback(self, interaction: discord.Interaction):
        self.view.section = self.values[0]
        await self.view.redraw(interaction)


class RankingsView(ui.View):
    def __init__(self, owner, ctx, board: str, *, scope: str | None = None, section: str = "all",
                 timeout: float = RANKINGS_TIMEOUT):
        super().__init__(timeout=timeout)
        self.owner, self.ctx = owner, ctx
        self.boards = boards_for(ctx.guild.id)
        if board not in dict(self.boards):
            board = self.boards[0][0] if self.boards else board
        self.board = board
        # Each scoped board keeps its own scope: a bare !lb opens on the server's default.
        self.scopes = {"coins": get_guild_cfg(ctx.guild.id).get("leaderboard_default_scope", "global"),
                       "records": "server"}
        if scope and board in SCOPED:
            self.scopes[board] = scope
        self.section = section
        self.message = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        if self.board == "records":
            from src.cogs.economy_cog import EconomyCog
            self.add_item(_SectionSelect(EconomyCog.RECORD_SECTIONS, self.section))
        for key, label in self.boards:
            self.add_item(_Button(label, key == self.board, 1, lambda i, k=key: self._pick_board(i, k)))
        if self.board in SCOPED:
            for label, value in SCOPES:
                self.add_item(_Button(label, value == self.scopes[self.board], 2,
                                      lambda i, v=value: self._pick_scope(i, v)))

    async def embed(self) -> discord.Embed:
        return await render(self.owner, self.ctx, self.board, self.scopes.get(self.board, "server"), self.section)

    async def _pick_board(self, interaction: discord.Interaction, board: str) -> None:
        self.board = board
        await self.redraw(interaction)

    async def _pick_scope(self, interaction: discord.Interaction, scope: str) -> None:
        self.scopes[self.board] = scope
        await self.redraw(interaction)

    async def redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=await self.embed(), view=self)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass  # cosmetic — the embed stays


async def send_rankings(owner, ctx, board: str, *, scope: str | None = None, section: str = "all"):
    """Post `board` with the rankings row under it; `owner` is the invoking cog."""
    view = RankingsView(owner, ctx, board, scope=scope, section=section)
    view.message = await ctx.send(embed=await view.embed(), view=view)
    return view.message
