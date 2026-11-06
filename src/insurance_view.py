"""The bare `!shop insurance` menu: one message, two pages.

The overview lists every type on sale with the invoker's coverage and a type
dropdown; a pick redraws the same message as that type's page, its tiers as
buttons plus Cancel subscription and Back. A press runs the typed path's
`_insurance_apply` / `_insurance_unsubscribe` with `ctx.send` captured, and
the reply shows as a field under the page instead of a new message.
"""
from __future__ import annotations

import discord
from discord import ui

from src.config import INSURANCE_TYPES
from src.economy import get_insurance_tier, insurance_policies
from src.forwarding import CapturingContext
from src.helpers import emb, C_PURPLE
from src.permissions import is_silenced

INSURANCE_TIMEOUT = 300.0


class InsuranceView(ui.View):
    def __init__(self, cog, ctx, *, timeout: float = INSURANCE_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.ctx = ctx
        self.owner = ctx.author
        self.kind: str | None = None
        self.notice: discord.Embed | None = None
        self.message = None
        self.build()

    def build(self) -> None:
        self.clear_items()
        uid = self.owner.id
        if self.kind is None:
            self.add_item(_TypeSelect(self.cog, uid))
            return
        subscribed = insurance_policies(self.kind)[1].get(uid)
        preferred = get_insurance_tier(uid, self.kind) or INSURANCE_TYPES[self.kind]["default"]
        for choice in self.cog._insurance_tier_choices(uid, self.kind, preferred):
            button = _TierButton(choice["label"], choice["value"])
            # Pressing the tier you already pay for would only say so.
            button.disabled = choice["value"] == subscribed
            if button.disabled:
                button.style = discord.ButtonStyle.success
            self.add_item(button)
        if subscribed:
            self.add_item(_CancelButton())
        self.add_item(_BackButton())

    def embed(self) -> discord.Embed:
        uid = self.owner.id
        if self.kind is None:
            e = emb("🛡️ Insurance", "Pick a type to subscribe or manage it. Premiums are charged at "
                    "the 5am reset each day.\n\n" + self.cog._insurance_overview(uid), C_PURPLE)
        else:
            spec = INSURANCE_TYPES[self.kind]
            e = emb(f"{spec['emoji']} {spec['label']} Insurance",
                    self.cog._insurance_prompt(uid, self.kind), C_PURPLE)
        if self.notice is not None:
            e.add_field(name=self.notice.title or "Result", value=(self.notice.description or "—")[:1024],
                        inline=False)
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message("Not your menu. Run `!shop insurance` for your own.",
                                                    ephemeral=True)
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

    async def run(self, interaction: discord.Interaction, action, *args) -> None:
        """Run a typed-path step with its reply captured, then redraw."""
        await interaction.response.defer()
        cap = CapturingContext(self.ctx)
        await action(cap, *args)
        last = cap.last
        self.notice = last if isinstance(last, discord.Embed) else None
        self.build()
        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass  # the menu may be gone; the purchase stands

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class _TypeSelect(ui.Select):
    def __init__(self, cog, uid: int):
        options = []
        for kind in cog._insurance_kinds():
            spec = INSURANCE_TYPES[kind]
            sub = insurance_policies(kind)[1].get(uid)
            low = min(t["cost"] for t in spec["tiers"].values())
            if sub:
                detail = "Subscribed" if len(spec["tiers"]) == 1 else f"Subscribed: {sub.title()}"
            else:
                detail = f"{low:,} coins/day" if len(spec["tiers"]) == 1 else f"From {low:,} coins/day"
            options.append(discord.SelectOption(label=f"{spec['label']} insurance", value=kind,
                                                emoji=spec["emoji"], description=detail))
        super().__init__(placeholder="Pick an insurance type…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: InsuranceView = self.view  # type: ignore[assignment]
        view.kind = self.values[0]
        view.notice = None
        await view.show(interaction)


class _TierButton(ui.Button):
    def __init__(self, label: str, tier: str):
        super().__init__(label=label[:80], style=discord.ButtonStyle.primary, row=0)
        self.tier = tier

    async def callback(self, interaction: discord.Interaction):
        view: InsuranceView = self.view  # type: ignore[assignment]
        await view.run(interaction, view.cog._insurance_apply, view.kind, self.tier)


class _CancelButton(ui.Button):
    def __init__(self):
        super().__init__(label="Cancel subscription", emoji="✖️", style=discord.ButtonStyle.danger, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: InsuranceView = self.view  # type: ignore[assignment]
        await view.run(interaction, view.cog._insurance_unsubscribe, view.kind)


class _BackButton(ui.Button):
    def __init__(self):
        super().__init__(label="Back", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction):
        view: InsuranceView = self.view  # type: ignore[assignment]
        view.kind = None
        view.notice = None
        await view.show(interaction)
