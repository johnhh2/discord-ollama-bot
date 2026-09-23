"""A generic panel: one message, a page dropdown, an item dropdown, Close.

The settings and shop panels grew their own copies of this shape; a new
panel subclasses `Panel` instead and fills in `pages`, `items`, `embed` and
`on_pick`. A pick on an item with `fields` opens a `FormModal` and calls
`on_pick` with the submitted values; a pick on a plain item calls it with
None. The panel is invoker-only, times out, and deletes itself when closed.
"""
from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import ui

from src.helpers import emb, C_GREY
from src.settings_views import FormModal, MAX_OPTIONS, _OwnedView

PANEL_TIMEOUT = 300.0
DESCRIPTION_MAX = 100  # a select option's description


@dataclass(frozen=True, kw_only=True)
class PanelItem:
    key: str
    label: str            # ≤100 chars
    description: str = ""
    fields: tuple = ()    # a form when non-empty
    title: str = ""       # the modal's title


class _PageSelect(ui.Select):
    def __init__(self, pages: list[tuple[str, str]], current: str, placeholder: str):
        super().__init__(
            placeholder=placeholder, min_values=1, max_values=1, row=0,
            options=[discord.SelectOption(label=label, value=key, default=key == current) for key, label in pages],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.view.show(interaction, self.values[0])


class _ItemSelect(ui.Select):
    def __init__(self, items: list[PanelItem], placeholder: str):
        self.items = {item.key: item for item in items[:MAX_OPTIONS]}
        super().__init__(
            placeholder=placeholder, min_values=1, max_values=1, row=1,
            options=[discord.SelectOption(label=item.label[:100], value=item.key, description=item.description[:DESCRIPTION_MAX] or None)
                     for item in self.items.values()],
        )

    async def callback(self, interaction: discord.Interaction):
        panel: Panel = self.view  # type: ignore[assignment]
        item = self.items[self.values[0]]
        if item.fields:
            async def _submitted(submit: discord.Interaction, values: dict):
                await panel.on_pick(submit, item, values)
            # A modal is the only reply a pick can open, so no defer first.
            await interaction.response.send_modal(FormModal(item.title or item.label, item.fields, on_submit=_submitted))
            return
        await panel.on_pick(interaction, item, None)


class _CloseButton(ui.Button):
    def __init__(self):
        super().__init__(label="Close", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction):
        await self.view.finish(interaction, True, "closed")


class Panel(_OwnedView):
    """Subclass and implement `pages`, `items`, `embed`, `on_pick`. `on_pick`
    answers the interaction (defer or send) and usually ends with
    `await self.refresh(interaction)`."""

    page_placeholder = "Section…"
    item_placeholder = "Pick…"
    closed_title = "closed"
    closed_hint = "Run the command again to open the panel."

    def __init__(self, ctx, page: str, *, timeout: float = PANEL_TIMEOUT):
        super().__init__(ctx.author.id, timeout)
        self.ctx = ctx
        self.page = page
        self._build()

    # ── to implement ──
    def pages(self) -> list[tuple[str, str]]:
        raise NotImplementedError

    def items(self) -> list[PanelItem]:
        raise NotImplementedError

    def embed(self) -> discord.Embed:
        raise NotImplementedError

    async def on_pick(self, interaction: discord.Interaction, item: PanelItem, values: dict | None) -> None:
        raise NotImplementedError

    # ── mechanics ──
    def _build(self) -> None:
        self.clear_items()
        pages = self.pages()
        if self.page not in dict(pages):
            self.page = pages[0][0] if pages else self.page
        self.add_item(_PageSelect(pages, self.page, self.page_placeholder))
        items = self.items()
        if items:
            self.add_item(_ItemSelect(items, self.item_placeholder))
        self.add_item(_CloseButton())

    async def show(self, interaction: discord.Interaction, page: str) -> None:
        self.page = page
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        try:
            await interaction.edit_original_response(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass  # the panel may be gone (closed, timed out); the action stands


async def open_panel(ctx, panel: Panel, *, ephemeral: bool = False) -> None:
    """Post the panel and wait it out. The message is deleted when it closes."""
    kwargs = {"ephemeral": True} if ephemeral else {}
    msg = await ctx.send(embed=panel.embed(), view=panel, **kwargs)
    await panel.wait()
    try:
        await msg.delete()
    except discord.HTTPException:
        try:
            await msg.edit(embed=emb(panel.closed_title, panel.closed_hint, C_GREY), view=None)
        except discord.HTTPException:
            pass  # cosmetic — every action already took effect
