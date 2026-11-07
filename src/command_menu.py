"""A row of buttons under a help card, each running a typed command.

`!chess`, `!puzzle`, `!count` / `!counter` and the free `!flip` carry one.
A button with `fields` opens a modal first; either way the press ends in the
command's own callback through `forwarding` (see CLAUDE.md: Buttons that run
commands), for the presser, after `refusal_for` and `is_silenced`. `build`
turns the modal's values into the command's arguments; the converters don't
run on a direct call, so it passes Members, not mentions. Invoker-only, since
the card is tailored to them; the row strips itself on timeout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import discord
from discord import ui

from src.forwarding import button_context, forward, refusal_for
from src.permissions import is_silenced
from src.settings_views import FormModal

MENU_TIMEOUT = 300.0


@dataclass(frozen=True)
class Call:
    """What a press runs: positional and keyword arguments, the members the
    command will read off `ctx.message.mentions`, and the audit-log text."""
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    mentions: tuple = ()
    content: str = ""


@dataclass(frozen=True, kw_only=True)
class Action:
    label: str
    emoji: str
    command: str                       # qualified name
    fields: tuple = ()                 # a modal when non-empty
    title: str = ""                    # the modal's title
    build: Callable[[dict, "discord.Guild | None"], "Call | str"] = lambda values, guild: Call()
    style: discord.ButtonStyle = discord.ButtonStyle.secondary
    row: int = 0
    level: bool = True                 # apply the level lock (refusal_for)


def member(guild, values: dict, key: str = "user"):
    """The first picked member of a users field, or None."""
    ids = values.get(key) or []
    return guild.get_member(int(ids[0])) if ids and guild is not None else None


class CommandMenu(ui.View):
    def __init__(self, bot, owner_id: int, actions: list[Action], *, timeout: float = MENU_TIMEOUT):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.owner_id = owner_id
        self.message = None
        for action in actions:
            self.add_item(_ActionButton(action))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Not your menu. Run the command for your own.", ephemeral=True)
            return False
        if is_silenced(interaction.user.id, interaction.guild.id if interaction.guild else None):
            await interaction.response.defer()
            return False
        return True

    async def run(self, interaction: discord.Interaction, action: Action, values: dict) -> None:
        built = action.build(values, interaction.guild)
        if isinstance(built, str):  # the build refused: say why, privately
            await interaction.response.send_message(built, ephemeral=True)
            return
        command = self.bot.get_command(action.command) if self.bot is not None else None
        if command is None:
            await interaction.response.send_message("That isn't available right now.", ephemeral=True)
            return
        ctx = button_context(self.bot, interaction, command, content=built.content or f"!{action.command}",
                             mentions=built.mentions)
        refusal = refusal_for(ctx, command, level=action.level)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        await forward(ctx, command, *built.args, **built.kwargs)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass  # the card may be gone


class _ActionButton(ui.Button):
    def __init__(self, action: Action):
        super().__init__(label=action.label, emoji=action.emoji, style=action.style, row=action.row)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view: CommandMenu = self.view  # type: ignore[assignment]
        if not self.action.fields:
            await view.run(interaction, self.action, {})
            return

        async def _submitted(submit: discord.Interaction, values: dict):
            await view.run(submit, self.action, values)
        await interaction.response.send_modal(
            FormModal(self.action.title or self.action.label, self.action.fields, on_submit=_submitted))


async def send_with_menu(bot, ctx, actions: list[Action], *, send=None, **send_kwargs):
    """Send the card (via `send(ctx, ...)`, default `ctx.send`) with the menu
    under it; with no actions, the card alone."""
    async def _default(c, **kw):
        return await c.send(**kw)
    send = send or _default
    if not actions:
        return await send(ctx, **send_kwargs)
    view = CommandMenu(bot, ctx.author.id, actions)
    view.message = await send(ctx, view=view, **send_kwargs)
    return view.message
