"""Deposit / Withdraw / Pay / Shop buttons under `!balance` and `!savings`.

Each button runs the typed command for whoever pressed it — a real Context
for the clicker via `forwarding.button_context`, after `refusal_for` (the
`savings` level gate, the economy switch) — so a press can never do what the
typed form can't. Amounts and the payee come from a modal. The card is
re-rendered after each action so the numbers on it stay current.
"""
from __future__ import annotations

import asyncio

import discord
from discord import ui

from src.forwarding import button_context, forward, refusal_for
from src.settings_views import Field, FormModal

WALLET_TIMEOUT = 300.0


class WalletView(ui.View):
    """`render()` is an async callable returning the card's embed. `pay` and
    `shop` add those buttons (the wallet card); the savings card has only
    Deposit / Withdraw."""

    def __init__(self, cog, *, render, pay: bool = False, shop: bool = False, timeout: float = WALLET_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.render = render
        self.message = None
        self.add_item(_FormButton("Deposit", "🐷", discord.ButtonStyle.success, self._deposit,
                                  (Field("amount", "Amount to deposit", placeholder="5k, half, all", max_length=12),)))
        self.add_item(_FormButton("Withdraw", "💸", discord.ButtonStyle.secondary, self._withdraw,
                                  (Field("amount", "Amount to withdraw", placeholder="5k, half, all", max_length=12),)))
        if pay:
            self.add_item(_FormButton("Pay", "🤝", discord.ButtonStyle.primary, self._pay,
                                      (Field("user", "Who?", kind="users"), Field("amount", "Amount", placeholder="500", max_length=12))))
        if shop:
            self.add_item(_ShopButton())

    async def run(self, interaction: discord.Interaction, command, args: tuple, *, content: str) -> None:
        """Run `command` as the clicker, refuse privately, then redraw the card."""
        ctx = button_context(self.cog.bot, interaction, command, content=content)
        refusal = refusal_for(ctx, command)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        await forward(ctx, command, *args, cog=self.cog)
        await self.redraw()

    async def redraw(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.edit(embed=await self.render(), view=self)
        except discord.HTTPException:
            pass  # the card may be gone; the action stands

    async def _deposit(self, interaction, values):
        await self.run(interaction, self.cog.cmd_deposit, (values["amount"],), content=f"!deposit {values['amount']}")

    async def _withdraw(self, interaction, values):
        await self.run(interaction, self.cog.cmd_withdraw, (values["amount"],), content=f"!withdraw {values['amount']}")

    async def _pay(self, interaction, values):
        ids = values.get("user") or []
        member = interaction.guild.get_member(int(ids[0])) if ids and interaction.guild else None
        if member is None:
            await interaction.response.send_message("Pick someone in this server.", ephemeral=True)
            return
        # The converter never runs on a direct call, so the command gets the Member.
        await self.run(interaction, self.cog.cmd_pay, (member, values["amount"]), content=f"!pay {member.mention} {values['amount']}")

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class _FormButton(ui.Button):
    def __init__(self, label: str, emoji: str, style, handler, fields):
        super().__init__(label=label, emoji=emoji, style=style)
        self.handler = handler
        self.fields = fields

    async def callback(self, interaction: discord.Interaction):
        async def _submitted(submit: discord.Interaction, values: dict):
            await self.handler(submit, values)
        await interaction.response.send_modal(FormModal(self.label, self.fields, on_submit=_submitted))


class _ShopButton(ui.Button):
    def __init__(self):
        super().__init__(label="Shop", emoji="🛒", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: WalletView = self.view  # type: ignore[assignment]
        bot = view.cog.bot
        shop_cog = bot.get_cog("ShopCog") if bot is not None else None
        command = bot.get_command("shop") if bot is not None else None
        if shop_cog is None or command is None:
            await interaction.response.send_message("The shop isn't available right now.", ephemeral=True)
            return
        ctx = button_context(bot, interaction, command, content="!shop")
        refusal = refusal_for(ctx, command, level=False)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        await interaction.response.defer()
        from src.shop_hub import open_shop_hub
        # The panel waits until it is closed — its own task, not this click's.
        asyncio.ensure_future(open_shop_hub(ctx, shop_cog, overview=shop_cog._overview_embed))
