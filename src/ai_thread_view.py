"""Continue / Invite / Stop buttons under the latest post in an AI thread.

`!continue`, `!invite @user` and `!stop` are the three things anyone does
in a story, roleplay or RPG thread, and each is a command to remember and
type. The row runs those very commands for whoever clicks — a real
`Context` for the clicker (`forwarding.button_context`), through
`forwarding.forward` after the permission and feature gates — so a button
can never do what the typed form can't. Only the thread's group (owner and
invitees) may press; anyone else is told so privately.

The row rides on the *latest* AI post only: `attach_row` strips it from the
previous one, so a thread never grows a column of dead buttons. `respond`
in src/ai.py calls it through `THREAD_POST_HOOKS` after every streamed
answer in a registered thread. The view is not persistent — after a
restart the row on an old post stops answering and the typed commands
still work.
"""
from __future__ import annotations

import logging

import discord
from discord import ui

from src import state
from src.forwarding import button_context, forward, refusal_for
from src.permissions import is_bot_admin_id
from src.settings_views import Field, FormModal

ROW_TIMEOUT = 6 * 3600.0
NOT_IN_GROUP = "This thread's buttons are for its group — ask the host for an invite."

# thread id → (message, view) of the post currently carrying the row
_current: dict[int, tuple] = {}


class ThreadActionsView(ui.View):
    def __init__(self, cog, thread_id: int, *, timeout: float = ROW_TIMEOUT):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.thread_id = thread_id
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        t = state.ai_threads.get(self.thread_id)
        uid = interaction.user.id
        if t is not None and (uid == t["owner_id"] or uid in t["invited_ids"] or is_bot_admin_id(uid)):
            return True
        await interaction.response.send_message(NOT_IN_GROUP if t is not None else "This thread is closed.", ephemeral=True)
        return False

    async def run(self, interaction: discord.Interaction, command, *, content: str, mentions=()) -> None:
        """Run `command` as the clicker, answering a refusal privately."""
        ctx = button_context(self.cog.bot, interaction, command, content=content, mentions=mentions)
        refusal = refusal_for(ctx, command, level=False)
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        await forward(ctx, command, cog=self.cog)

    @ui.button(label="Continue", style=discord.ButtonStyle.primary, emoji="▶️")
    async def continue_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.run(interaction, self.cog.cmd_continue, content="!continue")

    @ui.button(label="Invite", style=discord.ButtonStyle.secondary, emoji="📨")
    async def invite_button(self, interaction: discord.Interaction, button: ui.Button):
        async def _submitted(submit: discord.Interaction, values: dict):
            guild = submit.guild
            members = [m for m in (guild.get_member(uid) for uid in values.get("users") or []) if m is not None] if guild else []
            if not members:
                await submit.response.send_message("Nobody picked, or they aren't in this server.", ephemeral=True)
                return
            await self.run(submit, self.cog.cmd_invite_activity, content="!invite", mentions=members)
        await interaction.response.send_modal(FormModal(
            "Invite to this thread", [Field("users", "Who joins?", kind="users", max_values=10)], on_submit=_submitted,
        ))

    @ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.run(interaction, self.cog.cmd_stop, content="!stop")

    async def on_timeout(self) -> None:
        await _strip(self.thread_id, self)


async def _strip(thread_id: int, view: ThreadActionsView) -> None:
    """Remove the row from its post if it is still the current one."""
    current = _current.get(thread_id)
    if current is None or current[1] is not view:
        return
    _current.pop(thread_id, None)
    message = current[0]
    try:
        await message.edit(view=None)
    except Exception:
        pass  # the post may be gone; nothing to strip


async def attach_row(cog, message, thread_id: int) -> None:
    """Put the row under `message`, taking it off the previous post."""
    if thread_id not in state.ai_threads:
        return
    previous = _current.pop(thread_id, None)
    if previous is not None:
        previous[1].stop()
        try:
            await previous[0].edit(view=None)
        except Exception:
            pass  # best-effort — an old post that keeps its dead row is cosmetic
    view = ThreadActionsView(cog, thread_id)
    view.message = message
    try:
        await message.edit(view=view)
    except Exception:
        logging.warning("ai_thread_row_attach_failed thread=%s", thread_id)
        return
    _current[thread_id] = (message, view)
