"""Steal / Mug / Bank heist / Jailbreak / Bail / Insurance buttons under `!crime`.

Jailbreak and Bail show only while the invoker is jailed; Insurance shows
`held/total` of personal and property cover and hides once both are held.

Each press runs the typed command for the clicker (`forwarding.button_context`)
after `refusal_for`, so a press can't do what the typed form can't. Targets
and amounts come from a modal; the converters don't run on a direct call, so
the command gets the Member. The card is invoker-only (its jail line is
theirs) and is redrawn after each action.
"""
from __future__ import annotations

import discord
from discord import ui

from src.config import PROPERTY_INSURANCE_LIVE
from src.economy import get_insurance_expiry, insurance_policies
from src.features import feature_enabled
from src.forwarding import button_context, forward, refusal_for
from src.permissions import is_silenced
from src.settings_views import Field, FormModal, _OwnedView

CRIME_TIMEOUT = 300.0

_WHO = Field("user", "Who?", kind="users")


class CrimeView(_OwnedView):
    def __init__(self, cog, ctx, *, render, timeout: float = CRIME_TIMEOUT):
        super().__init__(ctx.author.id, timeout)
        self.cog = cog
        self.render = render
        self.message = None
        self.uid = ctx.author.id
        self.gid = ctx.guild.id if ctx.guild else None
        self._build()

    def _insurance_kinds(self) -> list[str]:
        """The policies the Insurance button counts: personal, and property where it's sold."""
        kinds = ["personal"]
        if PROPERTY_INSURANCE_LIVE and (self.gid is None or feature_enabled(self.gid, "assets")):
            kinds.append("property")
        return kinds

    def _build(self) -> None:
        """Rebuilt on every redraw: jail and coverage change under the card."""
        from src.cogs.economy_cog import _is_jailed
        gid = self.gid
        self.clear_items()
        self.add_item(_Button("Steal", "🦹", discord.ButtonStyle.danger, self._steal, (_WHO,)))
        self.add_item(_Button("Mug", "🔪", discord.ButtonStyle.danger, self._mug,
                              (_WHO, Field("amount", "Amount to pay the muggers", placeholder="5k", max_length=12))))
        if gid is None or feature_enabled(gid, "savings"):
            self.add_item(_Button("Bank heist", "🏦", discord.ButtonStyle.danger, self._heist, (_WHO,)))
        if _is_jailed(self.uid):
            self.add_item(_Button("Jailbreak", "🔓", discord.ButtonStyle.secondary, self._jailbreak))
            self.add_item(_Button("Bail", "💸", discord.ButtonStyle.secondary, self._bail))
        if gid is None or feature_enabled(gid, "shop"):
            kinds = self._insurance_kinds()
            held = sum(1 for k in kinds if self.uid in insurance_policies(k)[1] or get_insurance_expiry(self.uid, k))
            if held < len(kinds):
                self.add_item(_Button(f"Insurance {held}/{len(kinds)}", "🛡️", discord.ButtonStyle.primary,
                                      self._insurance, row=1))

    async def run(self, interaction: discord.Interaction, name: str, args: tuple = (), *, content: str) -> None:
        """Run the `name` command as the clicker, refuse privately, then redraw."""
        bot = self.cog.bot
        command = bot.get_command(name) if bot is not None else None
        if command is None:
            await interaction.response.send_message("That isn't available right now.", ephemeral=True)
            return
        if is_silenced(interaction.user.id, interaction.guild.id if interaction.guild else None):
            await interaction.response.defer()
            return
        ctx = button_context(bot, interaction, command, content=content)
        refusal = refusal_for(ctx, command)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        await forward(ctx, command, *args, cog=self.cog)
        await self.redraw()

    async def _target(self, interaction: discord.Interaction, values: dict):
        """The picked Member, or None after telling the clicker."""
        ids = values.get("user") or []
        member = interaction.guild.get_member(int(ids[0])) if ids and interaction.guild else None
        if member is None:
            await interaction.response.send_message("Pick someone in this server.", ephemeral=True)
        return member

    async def _steal(self, interaction, values):
        member = await self._target(interaction, values)
        if member is not None:
            await self.run(interaction, "steal", (member,), content=f"!steal {member.mention}")

    async def _mug(self, interaction, values):
        member = await self._target(interaction, values)
        if member is not None:
            await self.run(interaction, "mug", (member, values["amount"]),
                           content=f"!mug {member.mention} {values['amount']}")

    async def _heist(self, interaction, values):
        member = await self._target(interaction, values)
        if member is not None:
            await self.run(interaction, "bankheist", (member,), content=f"!bankheist {member.mention}")

    async def _jailbreak(self, interaction, values):
        await self.run(interaction, "jailbreak", content="!jailbreak")

    async def _bail(self, interaction, values):
        await self.run(interaction, "bail", content="!bail")

    async def _insurance(self, interaction, values):
        # Bare `!shop insurance` opens InsuranceView; ShopCog.forward applies the shop's gates.
        bot = self.cog.bot
        shop_cog = bot.get_cog("ShopCog") if bot is not None else None
        command = bot.get_command("shop insurance") if bot is not None else None
        if shop_cog is None or command is None:
            await interaction.response.send_message("Insurance isn't available right now.", ephemeral=True)
            return
        if is_silenced(interaction.user.id, interaction.guild.id if interaction.guild else None):
            await interaction.response.defer()
            return
        await interaction.response.defer()
        ctx = button_context(bot, interaction, command, content="!shop insurance")
        refusal = await shop_cog.forward(ctx, "shop_insurance")
        if refusal:
            await interaction.followup.send(refusal, ephemeral=True)

    async def redraw(self) -> None:
        if self.message is None:
            return
        self._build()
        try:
            await self.message.edit(embed=await self.render(), view=self)
        except discord.HTTPException:
            pass  # the card may be gone (it deletes itself); the action stands

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class _Button(ui.Button):
    """A press opens a modal of `fields`, or calls `handler` straight away when there are none."""

    def __init__(self, label: str, emoji: str, style, handler, fields: tuple = (), *, row: int = 0):
        super().__init__(label=label, emoji=emoji, style=style, row=row)
        self.handler = handler
        self.fields = fields

    async def callback(self, interaction: discord.Interaction):
        if not self.fields:
            await self.handler(interaction, {})
            return

        async def _submitted(submit: discord.Interaction, values: dict):
            await self.handler(submit, values)
        await interaction.response.send_modal(FormModal(self.label, self.fields, on_submit=_submitted))
