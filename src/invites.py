"""Reaction-based invite helpers for AI threads and games."""
import asyncio
import logging

from discord.ext import commands

from src.helpers import emb, C_BLUE
from src.reactions import ReactionCollector, seed_reactions

# How long an open-ended invite (_send_invite) keeps listening for ✅.
# Without a bound, every invite with a no-show invitee leaked a permanent
# gateway listener for the life of the process.
INVITE_LISTEN_SECS = 3600.0

ACCEPT_EMOJI = "✅"


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


async def _wait_for_confirmations(
    ctx: commands.Context,
    invited_users: list,
    title: str = "📨 Game Invite",
    timeout: float = 60.0,
) -> set:
    """Wait for invited users to react with ✅ within timeout. Returns set of confirmed user IDs."""
    if not invited_users:
        return set()
    invited_ids = {u.id for u in invited_users}
    mentions = " ".join(u.mention for u in invited_users)
    # silent=False: the invitees haven't done anything yet — this ping is the
    # only thing telling them a timed invite is waiting (ctx.send would
    # otherwise default to silent via SilentContext). Embed mentions never
    # notify, so the mentions also need to move into content for the ping
    # to be real.
    invite_msg = await ctx.send(
        content=mentions,
        embed=emb(
            title,
            f"{mentions}\n{ctx.author.mention} is inviting you. "
            f"React {ACCEPT_EMOJI} within {_window_text(timeout)} to join!",
            C_BLUE,
        ),
        silent=False,
    )

    confirmed_ids: set = set()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # Listen before seeding the ✅: an invitee who clicks it the instant it
    # appears must not be ignored (see src/reactions.py).
    async with ReactionCollector(ctx.bot, invite_msg) as reactions:
        await seed_reactions(invite_msg, [ACCEPT_EMOJI], what="invite")
        while confirmed_ids != invited_ids:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                emoji, user = await reactions.next(timeout=remaining)
            except asyncio.TimeoutError:
                break
            if emoji == ACCEPT_EMOJI and user.id in invited_ids:
                confirmed_ids.add(user.id)
    try:
        await invite_msg.delete()
    except Exception:
        pass
    return confirmed_ids


async def _send_invite(
    ctx: commands.Context,
    invited_users: list,
    title: str = "📨 Game Invite",
    dest=None,
    on_join=None,
):
    """Send an invite into dest and start a background task that calls on_join(user) whenever someone reacts ✅."""
    if not invited_users:
        return
    dest = dest or ctx
    invited_ids = {u.id for u in invited_users}
    mentions = " ".join(u.mention for u in invited_users)
    # silent=False + content mentions: same reasoning as _wait_for_confirmations
    # — the ping is the invite's delivery mechanism.
    invite_msg = await dest.send(
        content=mentions,
        embed=emb(
            title,
            f"{mentions}\n{ctx.author.mention} is inviting you. React {ACCEPT_EMOJI} to join!",
            C_BLUE,
        ),
        silent=False,
    )

    async def _listen():
        reacted: set = set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INVITE_LISTEN_SECS
        # The collector is live before the ✅ is seeded, so a click on it the
        # moment it appears is queued for the loop below.
        async with ReactionCollector(ctx.bot, invite_msg) as reactions:
            await seed_reactions(invite_msg, [ACCEPT_EMOJI], what="invite")
            while reacted != invited_ids:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    emoji, user = await reactions.next(timeout=remaining)
                except asyncio.TimeoutError:
                    break
                except asyncio.CancelledError:
                    break
                if emoji != ACCEPT_EMOJI or user.id not in invited_ids or user.id in reacted:
                    continue
                reacted.add(user.id)
                if on_join:
                    try:
                        await on_join(user)
                    except Exception:
                        # One invitee's failed join (deleted thread, missing
                        # perms) must not stop the remaining invitees.
                        logging.exception("[invite] on_join failed for user %s", user.id)

    asyncio.create_task(_listen())
