"""Per-game Discord threads, shared by chess, tic-tac-toe, Connect 4 and hangman.

Each match plays out in a public thread under the channel it was started
from. The game is keyed by the thread's id — so the parent channel is free
for the next match the moment the thread exists — and when the game ends
the thread is renamed with the outcome ("👑 X won against Y") and then
archived + locked. Everything degrades to an in-channel game when a thread
can't be created (DMs, missing Create Public Threads, active-thread cap).
"""
import logging

import discord
from discord.ext import commands

from src.helpers import emb, C_RED, log_bot_permission_error


# Threads hide from the channel after a week of inactivity; a new message
# (a move, !stop) auto-unarchives them, so long-running games survive.
GAME_THREAD_AUTO_ARCHIVE_MINUTES = 10080


async def _refuse_in_thread(ctx: commands.Context) -> bool:
    """Refuse (with an error embed) to start a game from inside a thread —
    Discord forbids nesting. Returns True when the caller must bail. Never
    awaits on the success path, so a caller's synchronous slot claim right
    after it stays atomic."""
    if isinstance(ctx.channel, discord.Thread):
        await ctx.send(embed=emb(
            "❌ Game Threads",
            "Start games from the channel itself — each game gets its own thread.",
            C_RED,
        ))
        return True
    return False


async def _try_create_game_thread(ctx: commands.Context, name: str):
    """Open the public thread a new game will be played in, or None to fall
    back to playing in the invoking channel (DMs, channels that can't host
    threads, missing Create Public Threads permission, active-thread cap).
    PvP wagers are already escrowed by the time this runs, so degrading to
    an in-channel game keeps the match honoured."""
    if ctx.guild is None:
        return None
    create = getattr(ctx.channel, "create_thread", None)
    if create is None:
        return None
    try:
        return await create(
            name=name[:100],
            type=discord.ChannelType.public_thread,
            auto_archive_duration=GAME_THREAD_AUTO_ARCHIVE_MINUTES,
        )
    except discord.Forbidden:
        log_bot_permission_error(ctx, "Create Public Threads")
        return None
    except discord.HTTPException as e:
        logging.warning(
            "game thread: create_thread failed (%s); playing in channel", type(e).__name__,
        )
        return None


async def _add_thread_members(thread, *users) -> None:
    """Pull the players into a fresh game thread so it lands in their
    sidebar. Best-effort: a member the bot can't add just has to click in."""
    if thread is None:
        return
    for user in users:
        try:
            await thread.add_user(user)
        except discord.HTTPException:
            pass


async def _close_game_thread(
    channel: discord.abc.Messageable, name: str | None = None,
    *, archive: bool = True,
):
    """Rename and/or close a finished game's thread. No-op outside threads.

    Locking needs Manage Threads (archiving/renaming our own thread doesn't),
    so a Forbidden on the full close retries without the lock — the outcome
    name and the archive matter more than keeping the thread sealed."""
    if not isinstance(channel, discord.Thread):
        return
    kwargs: dict = {}
    if name:
        kwargs["name"] = name[:100]
    if archive:
        kwargs.update(archived=True, locked=True)
    try:
        await channel.edit(**kwargs)
    except discord.Forbidden:
        if archive:
            kwargs.pop("locked", None)
            try:
                await channel.edit(**kwargs)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass
        logging.warning(f"game thread: couldn't close thread {channel.id}")
    except discord.HTTPException as e:
        logging.warning(f"game thread: closing thread {channel.id} failed: {e}")


def _join_names(names: list[str], limit: int = 3) -> str:
    """'A, B, C +2' — thread names cap at 100 chars, so a big hangman lobby
    can't list everyone; the helper's slice would otherwise cut mid-name."""
    shown = names[:limit]
    extra = len(names) - len(shown)
    text = ", ".join(shown)
    return f"{text} +{extra}" if extra > 0 else text
