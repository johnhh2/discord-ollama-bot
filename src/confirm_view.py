"""Reusable Confirm/Cancel purchase prompt for variable-cost commands.

The caller is responsible for charging *after* a True return — confirm_purchase
deliberately does not touch balances. This lets the caller re-validate state
(balance, jail status, etc.) right before debiting, since state can drift
during the timeout window.
"""
from __future__ import annotations

import discord
from discord import ui

from src.helpers import emb, C_GREEN, C_GREY, C_GOLD


class _ConfirmView(ui.View):
    def __init__(self, payer_id: int, timeout: float):
        super().__init__(timeout=timeout)
        self.payer_id = payer_id
        self.value: bool | None = None

    @ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.payer_id:
            await interaction.response.send_message("Not your purchase.", ephemeral=True)
            return
        self.value = True
        await interaction.response.defer()
        self.stop()

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.payer_id:
            await interaction.response.send_message("Not your purchase.", ephemeral=True)
            return
        self.value = False
        await interaction.response.defer()
        self.stop()


async def confirm_prompt(
    ctx,
    *,
    title: str,
    description: str,
    payer: discord.Member,
    timeout: float = 30.0,
) -> bool:
    """Show a Confirm/Cancel embed with a free-form body. Returns True only
    if `payer` clicks Confirm (False on Cancel or timeout). The generic core
    behind confirm_purchase — use directly when the "Cost:" framing doesn't
    fit (e.g. an offer paying the user)."""
    body = (
        f"{description}\n\n"
        f"Click **Confirm** within {int(timeout)}s to proceed."
    )
    view = _ConfirmView(payer_id=payer.id, timeout=timeout)
    msg = await ctx.send(embed=emb(title, body, C_GOLD), view=view)

    timed_out = await view.wait()

    if timed_out and view.value is None:
        closing, result = emb(f"⌛ {title} — Timed Out", description, C_GREY), False
    elif view.value is True:
        closing, result = emb(f"✅ {title} — Confirmed", description, C_GREEN), True
    else:
        closing, result = emb(f"🚫 {title} — Cancelled", description, C_GREY), False

    try:
        await msg.edit(embed=closing, view=None)
    except discord.HTTPException:
        pass
    return result


async def confirm_purchase(
    ctx,
    *,
    title: str,
    description: str,
    cost: int,
    payer: discord.Member,
    timeout: float = 30.0,
) -> bool:
    """Show a Confirm/Cancel embed with a Cost line. Returns True only if
    `payer` clicks Confirm."""
    return await confirm_prompt(
        ctx,
        title=title,
        description=f"{description}\n\n**Cost:** {cost:,} 🪙",
        payer=payer,
        timeout=timeout,
    )
