"""Scope buttons under a read-only embed — `!records` and `!leaderboard`
switch between server and global (and the idle ladder) with a press
instead of a second command. An optional section dropdown above the
buttons narrows the embed (the records board by category).

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
                         disabled=current, row=1)
        self.value = value

    async def callback(self, interaction: discord.Interaction):
        await self.view.pick(interaction, self.value)


class _SectionSelect(ui.Select):
    def __init__(self, sections: list[tuple[str, str]], current: str):
        super().__init__(
            placeholder="Section…", min_values=1, max_values=1, row=0,
            options=[discord.SelectOption(label=label, value=value, default=value == current) for label, value in sections],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.pick_section(interaction, self.values[0])


class ScopeView(ui.View):
    """`render(scope)` — or `render(scope, section)` when `sections` is
    given — is an async callable returning the embed; `scopes` and
    `sections` are `[(label, value)]`."""

    def __init__(self, render, scopes: list[tuple[str, str]], current: str, *,
                 sections: list[tuple[str, str]] | None = None, section: str | None = None,
                 timeout: float = SCOPE_TIMEOUT):
        super().__init__(timeout=timeout)
        self.render = render
        self.scopes = scopes
        self.current = current
        self.sections = sections
        self.section = section
        self.message = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        if self.sections:
            self.add_item(_SectionSelect(self.sections, self.section))
        for label, value in self.scopes:
            self.add_item(_ScopeButton(label, value, value == self.current))

    async def _embed(self) -> discord.Embed:
        if self.sections:
            return await self.render(self.current, self.section)
        return await self.render(self.current)

    async def pick(self, interaction: discord.Interaction, value: str) -> None:
        self.current = value
        self._build()
        await interaction.response.edit_message(embed=await self._embed(), view=self)

    async def pick_section(self, interaction: discord.Interaction, value: str) -> None:
        self.section = value
        self._build()
        await interaction.response.edit_message(embed=await self._embed(), view=self)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass  # cosmetic — the embed stays


async def send_scoped(ctx, render, scopes: list[tuple[str, str]], current: str, *,
                      sections: list[tuple[str, str]] | None = None, section: str | None = None):
    """Send the rendered embed with the scope buttons (and, with `sections`,
    the section dropdown) attached."""
    view = ScopeView(render, scopes, current, sections=sections, section=section)
    view.message = await ctx.send(embed=await view._embed(), view=view)
    return view.message
