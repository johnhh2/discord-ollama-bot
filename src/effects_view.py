"""Admin / Add / Remove buttons under the `!effects` card.

Only bot and server admins get the view; everyone else sees the card alone.
Bare `!effects` shows the invoker's own effects with an Admin button whose
modal picks a player. `!effects @user` shows that player's with Add and
Remove straight away. Add's modal offers the effects this admin may set (the
bot-wide ones need a global admin) plus a duration; Remove's offers what the
player has on. Both forward to the typed `!effects <id> add|remove …`, so its
checks stay the only ones; the card is redrawn afterwards.
"""
from __future__ import annotations

import discord
from discord import ui

from src.forwarding import forward
from src.permissions import is_global_admin, is_silenced
from src.settings_views import Field, FormModal, _OwnedView

EFFECTS_TIMEOUT = 300.0


class EffectsView(_OwnedView):
    def __init__(self, cog, ctx, target, *, chosen: bool, timeout: float = EFFECTS_TIMEOUT):
        super().__init__(ctx.author.id, timeout)
        self.cog = cog
        self.ctx = ctx
        self.target = target
        self.chosen = chosen  # a player was named or picked: Add / Remove apply to them
        self.message = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        if self.chosen:
            self.add_item(_Button("Add", "➕", discord.ButtonStyle.success, self._open_add))
            self.add_item(_Button("Remove", "➖", discord.ButtonStyle.danger, self._open_remove))
        self.add_item(_Button("Change player" if self.chosen else "Admin", "🔑",
                              discord.ButtonStyle.secondary, self._open_player))

    def _settable(self) -> list[str]:
        from src.cogs.effects_cog import _EFFECTS
        return [name for name, spec in _EFFECTS.items()
                if spec["admin_settable"] and (not spec.get("global") or is_global_admin(self.ctx))]

    def _removable(self) -> list[str]:
        settable = set(self._settable())
        return [name for name, _ in self.cog._active_effects_for(self.ctx.guild.id, self.target.id) if name in settable]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if is_silenced(interaction.user.id, interaction.guild.id if interaction.guild else None):
            await interaction.response.defer()
            return False
        return True

    # ── presses ──────────────────────────────────────────────────────────
    async def _open_player(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(FormModal(
            "Choose a player", (Field("user", "Whose effects?", kind="users"),), on_submit=self._picked))

    async def _picked(self, interaction: discord.Interaction, values: dict) -> None:
        ids = values.get("user") or []
        member = interaction.guild.get_member(int(ids[0])) if ids and interaction.guild else None
        if member is None:
            await interaction.response.send_message("Pick someone in this server.", ephemeral=True)
            return
        self.target, self.chosen = member, True
        self._build()
        await interaction.response.edit_message(embed=self.cog.effects_embed(self.ctx, member), view=self)

    async def _open_add(self, interaction: discord.Interaction) -> None:
        from src.cogs.effects_cog import _EFFECTS
        names = self._settable()
        if not names:
            await interaction.response.send_message("There are no effects you can add.", ephemeral=True)
            return
        await interaction.response.send_modal(FormModal(f"Add to {self.target.display_name}", (
            Field("effect", "Effect", kind="choices",
                  options=tuple((f"{_EFFECTS[n]['emoji']} {n}", n) for n in names)),
            Field("duration", "Duration (empty = permanent)", required=False, placeholder="30m, 2h, 3d, 1w", max_length=20),
        ), on_submit=self._add))

    async def _open_remove(self, interaction: discord.Interaction) -> None:
        from src.cogs.effects_cog import _EFFECTS
        names = self._removable()
        if not names:
            await interaction.response.send_message(
                f"**{self.target.display_name}** has nothing you can remove.", ephemeral=True)
            return
        await interaction.response.send_modal(FormModal(f"Remove from {self.target.display_name}", (
            Field("effect", "Effect", kind="choices",
                  options=tuple((f"{_EFFECTS[n]['emoji']} {n}", n) for n in names)),
        ), on_submit=self._remove))

    async def _add(self, interaction: discord.Interaction, values: dict) -> None:
        args = ["add", values["effect"][0]] + ([values["duration"]] if values.get("duration") else [])
        await self._run(interaction, args)

    async def _remove(self, interaction: discord.Interaction, values: dict) -> None:
        await self._run(interaction, ["remove", values["effect"][0]])

    async def _run(self, interaction: discord.Interaction, args: list[str]) -> None:
        await interaction.response.defer()
        # The typed command resolves the player from the id, as from `!effects <id> add …`.
        await forward(self.ctx, self.cog.cmd_effects, str(self.target.id), *args, cog=self.cog)
        await self.redraw()

    async def redraw(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.cog.effects_embed(self.ctx, self.target), view=self)
        except discord.HTTPException:
            pass  # the card may be gone; the change stands

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class _Button(ui.Button):
    def __init__(self, label: str, emoji: str, style, handler):
        super().__init__(label=label, emoji=emoji, style=style)
        self.handler = handler

    async def callback(self, interaction: discord.Interaction):
        await self.handler(interaction)
