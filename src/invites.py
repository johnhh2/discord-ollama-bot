"""Invite prompts for games and AI threads: Accept / Decline buttons.

An invite pings the invitees (content mentions, `silent=False` — the ping
is the delivery) and waits for their answers on two buttons. Only an
invitee can press; a stranger is told so privately. `_wait_for_confirmations`
returns once everyone has answered or the window closes;
`_send_invite` is the open-ended form that calls `on_join` per acceptance.
"""
import asyncio
import logging

import discord
from discord import ui
from discord.ext import commands

from src.helpers import emb, C_BLUE

# How long an open-ended invite (_send_invite) keeps its buttons live.
# Without a bound, every invite with a no-show invitee kept a view (and,
# before buttons, a gateway listener) for the life of the process.
INVITE_LISTEN_SECS = 3600.0

NOT_YOURS = "This invite isn't for you."


def _window_text(seconds: float) -> str:
    """'60 seconds' / '5 minutes' / '1 hour' — human wording for the invite
    window shown in the embed, so the text always matches the timeout."""
    secs = int(seconds)
    if secs >= 3600 and secs % 3600 == 0:
        h = secs // 3600
        return f"{h} hour" + ("s" if h != 1 else "")
    if secs >= 60 and secs % 60 == 0 and secs > 60:
        m = secs // 60
        return f"{m} minutes"
    return f"{secs} seconds"


class InviteView(ui.View):
    """Accept / Decline for `invited_ids`. `accepted` and `declined` fill
    as answers land; the view stops once everyone has answered. `on_accept`
    (async, takes the user) runs on each acceptance when given."""

    def __init__(self, invited_ids: set, *, timeout: float, on_accept=None):
        super().__init__(timeout=timeout)
        self.invited_ids = set(invited_ids)
        self.accepted: set = set()
        self.declined: set = set()
        self.on_accept = on_accept

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.invited_ids:
            return True
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return False

    def _answered(self) -> bool:
        return self.accepted | self.declined >= self.invited_ids

    @ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        uid = interaction.user.id
        already = uid in self.accepted
        self.accepted.add(uid)
        self.declined.discard(uid)
        await interaction.response.defer()
        if not already and self.on_accept is not None:
            try:
                await self.on_accept(interaction.user)
            except Exception:
                # One invitee's failed join (deleted thread, missing perms)
                # must not stop the remaining invitees.
                logging.exception("[invite] on_accept failed for user %s", uid)
        if self._answered():
            self.stop()

    @ui.button(label="Decline", style=discord.ButtonStyle.secondary, emoji="❌")
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        uid = interaction.user.id
        if uid not in self.accepted:  # an acceptance stands
            self.declined.add(uid)
        await interaction.response.defer()
        if self._answered():
            self.stop()


async def _post_invite(ctx, dest, invited_users: list, title: str, action: str, view: InviteView):
    mentions = " ".join(u.mention for u in invited_users)
    # silent=False: the invitees haven't done anything yet — this ping is the
    # only thing telling them an invite is waiting (ctx.send would otherwise
    # default to silent via SilentContext). Embed mentions never notify, so
    # the mentions also need to move into content for the ping to be real.
    return await dest.send(
        content=mentions,
        embed=emb(title, f"{mentions}\n{ctx.author.mention} is inviting you. {action}", C_BLUE),
        view=view,
        silent=False,
    )


async def _wait_for_confirmations(
    ctx: commands.Context,
    invited_users: list,
    title: str = "📨 Game Invite",
    timeout: float = 60.0,
) -> set:
    """Wait for the invitees' answers within `timeout`. Returns the set of
    user ids that accepted."""
    if not invited_users:
        return set()
    view = InviteView({u.id for u in invited_users}, timeout=timeout)
    invite_msg = await _post_invite(ctx, ctx, invited_users, title, f"**Accept** within {_window_text(timeout)} to join!", view)
    # Bounded here, not only by the view's own clock: discord.py starts that
    # clock when the client dispatches the view, which a stubbed send never does.
    try:
        await asyncio.wait_for(view.wait(), timeout)
    except asyncio.TimeoutError:
        view.stop()
    # Best-effort cleanup: a failed delete must not lose the confirmations.
    try:
        await invite_msg.delete()
    except Exception:
        pass
    return set(view.accepted)


async def _send_invite(
    ctx: commands.Context,
    invited_users: list,
    title: str = "📨 Game Invite",
    dest=None,
    on_join=None,
):
    """Send an invite into `dest` whose Accept calls `on_join(user)` for
    each invitee who presses it, for up to INVITE_LISTEN_SECS."""
    if not invited_users:
        return
    dest = dest or ctx
    view = InviteView({u.id for u in invited_users}, timeout=INVITE_LISTEN_SECS, on_accept=on_join)
    invite_msg = await _post_invite(ctx, dest, invited_users, title, "Press **Accept** to join!", view)

    async def _retire():
        try:
            await asyncio.wait_for(view.wait(), INVITE_LISTEN_SECS)
        except asyncio.TimeoutError:
            view.stop()
        try:
            await invite_msg.edit(view=None)
        except Exception:
            pass  # cosmetic — the answers were already acted on
    asyncio.create_task(_retire())
