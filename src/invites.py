"""Reaction-based invite helpers for AI threads and games."""
import asyncio
import logging

from discord.ext import commands

from src.helpers import emb, C_BLUE

# How long an open-ended invite (_send_invite) keeps listening for ✅.
# Without a bound, every invite with a no-show invitee leaked a permanent
# gateway listener for the life of the process.
INVITE_LISTEN_SECS = 3600.0


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
            f"React ✅ within {_window_text(timeout)} to join!",
            C_BLUE,
        ),
        silent=False,
    )
    await invite_msg.add_reaction("✅")

    def check(reaction, user):
        return (
            reaction.message.id == invite_msg.id
            and str(reaction.emoji) == "✅"
            and user.id in invited_ids
        )

    confirmed_ids: set = set()
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            _, user = await ctx.bot.wait_for("reaction_add", check=check, timeout=remaining)
            confirmed_ids.add(user.id)
            if confirmed_ids == invited_ids:
                break
        except asyncio.TimeoutError:
            break
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
            f"{mentions}\n{ctx.author.mention} is inviting you. React ✅ to join!",
            C_BLUE,
        ),
        silent=False,
    )
    await invite_msg.add_reaction("✅")

    def check(reaction, user):
        return (
            reaction.message.id == invite_msg.id
            and str(reaction.emoji) == "✅"
            and user.id in invited_ids
        )

    async def _listen():
        reacted: set = set()
        deadline = asyncio.get_running_loop().time() + INVITE_LISTEN_SECS
        while reacted != invited_ids:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                _, user = await ctx.bot.wait_for("reaction_add", check=check, timeout=remaining)
            except asyncio.TimeoutError:
                break
            except asyncio.CancelledError:
                break
            if user.id not in reacted:
                reacted.add(user.id)
                if on_join:
                    try:
                        await on_join(user)
                    except Exception:
                        # One invitee's failed join (deleted thread, missing
                        # perms) must not stop the remaining invitees.
                        logging.exception("[invite] on_join failed for user %s", user.id)

    asyncio.create_task(_listen())
