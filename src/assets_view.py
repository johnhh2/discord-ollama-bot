"""Buttons and dropdowns on the `!assets` cards.

One message, three modes: the portfolio (Browse / Manage), the market (a
dropdown of everything for sale plus Buy) and manage (a dropdown of the
invoker's deeds, then Sell / Unlist / Upgrade / Rename for the one picked).
Every action runs the typed `!assets` subcommand for the presser through
`forwarding` (see CLAUDE.md: Buttons that run commands), so its confirm
prompts, re-checks and race guards are the typed path's; the card is
redrawn afterwards so prices and listings stay current.
"""
from __future__ import annotations

import discord
from discord import ui

from src.forwarding import button_context, forward, refusal_for
from src.helpers import emb, C_PURPLE
from src.permissions import is_silenced
from src.properties import upgrade_types_for
from src.settings_views import Field, FormModal, MAX_OPTIONS

ASSETS_TIMEOUT = 300.0
_MAX_SELECTS = 4  # rows 0-3; row 4 holds the buttons


class AssetsView(ui.View):
    """`mode` is "home", "browse" or "manage". `home` adds a Back button to
    the portfolio (the card was opened by bare `!assets`). `notice` replaces
    the market list as the browse embed — the `!assets buy` prompt."""

    def __init__(self, cog, owner, *, mode: str = "home", home: bool = False,
                 tier: int | None = None, notice: discord.Embed | None = None,
                 timeout: float = ASSETS_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner = owner
        self.mode = mode
        self.home = home
        self.tier = tier
        self.notice = notice
        self.choice: str | None = None
        self.message = None
        self.build()

    # ── state → components ───────────────────────────────────────────────

    def build(self) -> None:
        from src.cogs.assets_cog import buyable_properties, owned_rows
        self.clear_items()
        if self.mode == "home":
            self.add_item(_ModeButton("Browse offerings", "🏘️", "browse", discord.ButtonStyle.primary))
            manage = _ModeButton("Manage properties", "🛠️", "manage", discord.ButtonStyle.secondary)
            manage.disabled = not owned_rows(self.owner.id)
            self.add_item(manage)
            return
        if self.mode == "browse":
            options = buyable_properties(self.owner.id, self.tier)
            if self.choice not in {o[0] for o in options}:
                self.choice = None
            chunks = [options[i:i + MAX_OPTIONS] for i in range(0, len(options), MAX_OPTIONS)][:_MAX_SELECTS]
            for row, chunk in enumerate(chunks):
                placeholder = "Pick a property to buy…" if len(chunks) == 1 else (
                    f"Pick a property ({chunk[0][3]} – {chunk[-1][3]})…")
                self.add_item(_PropertySelect(chunk, self.choice, placeholder, row))
            buy = _ActionButton("Buy", "🛒", discord.ButtonStyle.success, self._buy)
            buy.disabled = self.choice is None
            self.add_item(buy)
        else:
            rows = owned_rows(self.owner.id)
            if self.choice not in {pid for pid, _, _ in rows}:
                self.choice = None
            if rows:
                options = [(pid, label, detail, label) for pid, label, detail in rows]
                self.add_item(_PropertySelect(options, self.choice, "Pick one of your properties…", 0))
            if self.choice is not None:
                row = self.cog.owner_row(self.choice)
                self.add_item(_ActionButton("Sell", "🏷️", discord.ButtonStyle.primary, self._sell, row=1))
                if row.get("list_price"):
                    self.add_item(_ActionButton("Unlist", "↩️", discord.ButtonStyle.secondary, self._unlist, row=1))
                if not all(row.get(self.cog._UPGRADE_FLAGS[k]) for k in upgrade_types_for(self.choice)):
                    self.add_item(_ActionButton("Upgrade", "⭐", discord.ButtonStyle.success, self._upgrade, row=1))
                self.add_item(_ActionButton("Rename", "✏️", discord.ButtonStyle.secondary, self._rename, row=1))
        if self.home:
            self.add_item(_ModeButton("Back", "◀️", "home", discord.ButtonStyle.secondary, row=4))

    def embed(self) -> discord.Embed:
        if self.mode == "home":
            return self.cog.portfolio_embed(self.owner)
        if self.mode == "browse":
            return self.notice or self.cog.browse_embed(self.owner.id, self.tier) or emb(
                "🏘️ Real Estate", "Nothing is for sale right now.", C_PURPLE)
        return self.cog.manage_embed(self.owner, self.choice)

    # ── interaction plumbing ─────────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("Not your menu. Run `!assets` for your own.", ephemeral=True)
            return False
        if is_silenced(interaction.user.id, interaction.guild.id if interaction.guild else None):
            await interaction.response.defer()
            return False
        if self.message is None:
            self.message = interaction.message
        return True

    async def show(self, interaction: discord.Interaction) -> None:
        self.build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def redraw(self) -> None:
        self.build()
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.embed(), view=self)
        except discord.HTTPException:
            pass  # the card may be gone; the action stands

    async def run(self, interaction: discord.Interaction, sub: str, *args, content: str, **kwargs) -> None:
        """Run `!assets <sub>` as the presser, refuse privately, then redraw."""
        command = self.cog.bot.get_command(f"assets {sub}") if self.cog.bot else None
        command = command or getattr(self.cog, f"assets_{sub}")
        ctx = button_context(self.cog.bot, interaction, command, content=content)
        refusal = refusal_for(ctx, command, level=False)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        await forward(ctx, command, *args, cog=self.cog, **kwargs)
        await self.redraw()

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    # ── actions ──────────────────────────────────────────────────────────

    async def _buy(self, interaction):
        await self.run(interaction, "buy", name=self.choice, content=f"!assets buy {self.choice}")

    async def _unlist(self, interaction):
        await self.run(interaction, "unlist", name=self.choice, content=f"!assets unlist {self.choice}")

    async def _upgrade(self, interaction):
        await self.run(interaction, "upgrade", name=self.choice, content=f"!assets upgrade {self.choice}")

    async def _sell(self, interaction):
        pid = self.choice

        async def submitted(submit, values):
            price = values["price"]
            await self.run(submit, "sell", pid, price, content=f"!assets sell {pid} {price}")
        await interaction.response.send_modal(FormModal("Sell a property", (
            Field("price", "Asking price", placeholder="250k", max_length=12,
                  description="At 75% of its value or less, the bank offers to buy it instantly."),
        ), on_submit=submitted))

    async def _rename(self, interaction):
        pid = self.choice
        current = self.cog.owner_row(pid).get("custom_name") or ""

        async def submitted(submit, values):
            name = values["name"]
            await self.run(submit, "rename", pid, name, content=f"!assets rename {pid} {name}")
        await interaction.response.send_modal(FormModal("Rename your business", (
            Field("name", "New name", default=current or None, max_length=48),
        ), on_submit=submitted))


class _ModeButton(ui.Button):
    def __init__(self, label: str, emoji: str, mode: str, style, row: int = 0):
        super().__init__(label=label, emoji=emoji, style=style, row=row)
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        view: AssetsView = self.view  # type: ignore[assignment]
        view.mode = self.mode
        view.choice = None
        view.notice = None
        await view.show(interaction)


class _ActionButton(ui.Button):
    def __init__(self, label: str, emoji: str, style, handler, row: int = 4):
        super().__init__(label=label, emoji=emoji, style=style, row=row)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        await self.handler(interaction)


class _PropertySelect(ui.Select):
    """Options are (pid, label, description, sort label) tuples."""

    def __init__(self, options, chosen: str | None, placeholder: str, row: int):
        super().__init__(placeholder=placeholder[:150], row=row, options=[
            discord.SelectOption(label=label[:100], value=pid, description=(detail or None) and detail[:100],
                                 default=pid == chosen)
            for pid, label, detail, _ in options
        ])

    async def callback(self, interaction: discord.Interaction):
        view: AssetsView = self.view  # type: ignore[assignment]
        view.choice = self.values[0]
        await view.show(interaction)
