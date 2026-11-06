"""The Repair button on a property theft's owner ping.

A press is the owner typing `!assets repair <deed>` (see CLAUDE.md: Buttons
that run commands), so the confirm prompt, the insurance split and the
synchronous claim are the command's. Not persistent: after a restart or the
timeout the typed command still works.
"""
from __future__ import annotations

import discord
from discord import ui

from src import state
from src.forwarding import button_context, forward, refusal_for
from src.permissions import is_silenced
from src.property_theft import is_damaged

REPAIR_TIMEOUT = 3600.0


class RepairView(ui.View):
    def __init__(self, bot, owner_id: int, pid: str, *, timeout: float = REPAIR_TIMEOUT):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.owner_id = owner_id
        self.pid = pid
        self.message = None

    def _repairable(self) -> bool:
        row = state.property_owners.get(self.pid)
        return bool(row) and row["owner_id"] == self.owner_id and is_damaged(row)

    @ui.button(label="Repair", emoji="🔧", style=discord.ButtonStyle.success)
    async def repair(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the owner can repair it.", ephemeral=True)
            return
        if is_silenced(interaction.user.id, interaction.guild.id if interaction.guild else None):
            await interaction.response.defer()
            return
        command = self.bot.get_command("assets repair")
        ctx = button_context(self.bot, interaction, command, content=f"!assets repair {self.pid}")
        refusal = refusal_for(ctx, command, level=False)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await interaction.response.defer()
        await forward(ctx, command, cog=command.cog, name=self.pid)
        if not self._repairable():
            button.disabled = True
            self.stop()
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass  # the ping may be gone; the repair stands

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass
