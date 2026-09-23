"""Scope buttons under a read-only embed — `!records` and `!leaderboard`
switch between server and global (and the idle ladder) with a press
instead of a second command.

Anyone may press: the embed is public and the scopes read nothing of the
presser's. The current scope's button is disabled, so the row doubles as
the scope label. The row strips itself on timeout.
"""
from __future__ import annotations

import discord
from discord import ui

SCOPE_TIMEOUT = 300.0


class _ScopeButton(ui.Button):
    def __init__(self, label: str, value: str, current: bool):
        super().__init__(label=label, style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary,
                         disabled=current)
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        await self.view.pick(interaction, self.value)


class ScopeView(ui.View):
    """`render(scope)` is an async callable returning the embed for that
    scope; `scopes` is `[(label, value)]`."""

    def __init__(self, render, scopes: list[tuple[str, str]], current: str, *, timeout: float = SCOPE_TIMEOUT):
        super().__init__(timeout=timeout)
        self.render = render
        self.scopes = scopes
        self.current = current
        self.message = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        for label, value in self.scopes:
            self.add_item(_ScopeButton(label, value, value == self.current))

    async def pick(self, interaction: discord.Interaction, value: str) -> None:
        self.current = value
        self._build()
        embed = await self.render(value)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass  # cosmetic — the embed stays


async def send_scoped(ctx, render, scopes: list[tuple[str, str]], current: str):
    """Send `render(current)` with the scope buttons attached."""
    view = ScopeView(render, scopes, current)
    view.message = await ctx.send(embed=await render(current), view=view)
    return view.message
