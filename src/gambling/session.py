"""!session / !thread — a public thread for gambling, gambling-only inside.

`!session` opens a public thread off the game channel it's run in and posts
the commands that work there as the thread's first message. Anyone can
play in it — the owner only matters for closing. A bot-wide check
(`GamblingSessionCog.bot_check`) refuses every other command inside — the
thread is a casino table, not a second copy of the channel. Only games
that don't open a thread of their own belong here (Discord can't nest
threads), so chess, ask, story and friends stay out.

The thread inherits its parent channel's game-channel / command-channel
status through `gate_channel_ids` in src.permissions — that's what lets
`!slots` run in the thread even though the thread's own id is never in
`game_channels`.

The thread names itself after the table's biggest winner ("Alice gained
1,200 coins") or, when nobody is up, its biggest loser ("Bob lost 400
coins"). It starts as NEW_THREAD_NAME. Results reach it through
`economy.GAMBLING_RESULT_HOOKS`: every gambling game already calls
`record_gambling_event` at outcome time, and the thread-eligible ones pass
their `channel_id`, so the tally updates on the result itself — nothing
polls gambling_history. Discord only allows two name changes per channel
per ten minutes, so renames are coalesced (see RENAME_MIN_INTERVAL).

Registry: `state.gambling_threads` {thread_id: {owner_id, guild_id,
parent_id, created_at, tally: {uid: {net, name}}}}, mirrored row-for-row
to the gambling_threads table so a reboot keeps the gate, the tally and
`!stop` working. The row goes when the owner (or an admin) closes the
table with `!stop` (`close_gambling_thread`, called from AICog.cmd_stop,
which then archives the thread), or when the thread is archived or deleted
out from under us.
"""
from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import NamedTuple

import discord
from discord.ext import commands

from src import state
# Looked up through the package at call time (not bound at import) so the
# conftest no-op stubs on src.persistence reach this module too.
import src.persistence as persistence
from src import economy
from src.economy import add_balance
from src.games.blackjack import blackjack_stand, retire_blackjack_buttons
from src.helpers import emb, C_GOLD, C_RED, log_bot_permission_error
from src.permissions import check_game_channel, is_admin, _wrong_channel_reply


log = logging.getLogger(__name__)

# Root command names that work inside a gambling thread. Aliases resolve to
# these through ctx.command.qualified_name, so `!bj` and `!scratch` pass.
GAMBLING_THREAD_COMMANDS: frozenset[str] = frozenset({
    "slots", "flip", "scratchoff", "scratches", "blackjack", "race", "stop",
})

# The thread's first message lists these, in this order.
GAMBLING_THREAD_COMMAND_LINES: tuple[str, ...] = (
    "`!slots <amount>` — 3-reel slot machine with progressive jackpot",
    "`!flip <amount>` — 50/50 coinflip",
    "`!scratchoff` / `!scratches` — daily scratchoffs (3 a day)",
    "`!blackjack <amount>` — Hit / Stand / Double Down",
    "`!race @user [@user …] [amount]` — race against others",
    "`!stop` — close the thread (owner or admin)",
)

# A day idle and Discord archives the thread; on_thread_update then drops
# the registry row, so a revived thread is an ordinary one.
GAMBLING_THREAD_AUTO_ARCHIVE_MINUTES = 1440

NEW_THREAD_NAME = "New Gambling Thread"
THREAD_NAME_MAX = 100

# Discord allows two name changes per channel per ten minutes, and a busy
# table changes leader far more often than that. The first rename goes out
# at once; while the budget is spent, one delayed rename waits this long
# after the previous one and then applies whatever the tally says by then.
RENAME_MIN_INTERVAL = 300.0


class GamblingThreadOnly(commands.CheckFailure):
    """Raised by the gambling-thread gate for a command that isn't on the
    list. The gate has already replied; on_command_error must swallow this."""


class CloseResult(NamedTuple):
    lines: list[str]      # summary lines for the ⏹️ Stopped embed
    close: bool           # archive the thread
    name: str | None      # final thread name to set with the archive edit, if any


def _command_list() -> str:
    return "\n".join(GAMBLING_THREAD_COMMAND_LINES)


def opening_embed(owner) -> discord.Embed:
    return emb(
        "🎰 Gambling Thread",
        "Only these commands work in here:\n" + _command_list()
        + "\n\nAnyone can play. The thread renames itself after whoever is up the most"
        " — or, if nobody is, down the most."
        + f"\n{owner.mention} or an admin closes the table with `!stop`.",
        C_GOLD,
    )


def leader_title(row: dict) -> str | None:
    """The thread name the tally calls for: the biggest winner, or — when
    nobody is up — the biggest loser. None when nobody has a non-zero net
    (a fresh table, or nothing but pushes), so the name is left alone.
    Ties go to whoever got there first (dict order)."""
    tally = row.get("tally") or {}
    if not tally:
        return None
    uid, entry = max(tally.items(), key=lambda kv: kv[1]["net"])
    if entry["net"] > 0:
        verb, amount = "gained", entry["net"]
    else:
        uid, entry = min(tally.items(), key=lambda kv: kv[1]["net"])
        if entry["net"] >= 0:
            return None
        verb, amount = "lost", -entry["net"]
    name = entry.get("name") or str(uid)
    suffix = f" {verb} {amount:,} coins"
    return name[:THREAD_NAME_MAX - len(suffix)] + suffix


async def _try_create_gambling_thread(ctx: commands.Context, name: str):
    """Open the public thread, or None when the channel can't host one (DMs,
    missing Create Public Threads, the guild's active-thread cap)."""
    create = getattr(ctx.channel, "create_thread", None)
    if ctx.guild is None or create is None:
        return None
    try:
        return await create(
            name=name[:THREAD_NAME_MAX],
            type=discord.ChannelType.public_thread,
            auto_archive_duration=GAMBLING_THREAD_AUTO_ARCHIVE_MINUTES,
        )
    except discord.Forbidden:
        log_bot_permission_error(ctx, "Create Public Threads")
        return None
    except discord.HTTPException as e:
        log.warning("session: create_thread failed (%s)", type(e).__name__)
        return None


async def _stand_live_hands(channel, guild) -> list[str]:
    """Settle every dealt hand at this table by standing it — a hand never
    expires on its own, and nothing can act on it once the thread is
    archived. Returns the players' display names."""
    names: list[str] = []
    for uid, game in list(state.active_blackjack_games.items()):
        if game.get("channel_id") != channel.id or game.get("pending"):
            continue
        member = guild.get_member(uid) if guild is not None else None
        author = member if member is not None else SimpleNamespace(id=uid, display_name=str(uid))
        await blackjack_stand(author, channel, guild)
        names.append(author.display_name)
    return names


async def close_gambling_thread(ctx: commands.Context) -> CloseResult | None:
    """`!stop` in a gambling thread by its owner (or an admin).

    None when this isn't a gambling thread or the caller can't close it. A
    race still running here keeps the table open — its board edits and
    payout would otherwise land in an archived thread. Otherwise every live
    blackjack hand stands (the closer's included: standing never does worse
    than a forfeit), the registry row goes, and the caller archives — with
    the final leader name riding the same edit when Discord's rename budget
    allows (otherwise the name just lags the last result).
    """
    cid = ctx.channel.id
    row = state.gambling_threads.get(cid)
    if row is None or not (row["owner_id"] == ctx.author.id or is_admin(ctx)):
        return None
    if cid in state.active_race_games:
        return CloseResult(
            ["🎰 Gambling thread stays open — a race is still running; `!stop` again when it's done"],
            False, None,
        )
    settled = await _stand_live_hands(ctx.channel, ctx.guild)  # results land in the tally
    state.gambling_threads.pop(cid, None)
    task = row.get("_rename_task")
    if task is not None and not task.done():
        task.cancel()
    final = leader_title(row)
    if (
        final == getattr(ctx.channel, "name", None)
        or time.monotonic() - row.get("_renamed_at", 0.0) < RENAME_MIN_INTERVAL
    ):
        final = None
    await persistence.delete_gambling_thread(cid)
    lines = ["🎰 Gambling thread (closed)"]
    if settled:
        lines.append(f"🃏 Stood {len(settled)} live hand(s): {', '.join(settled)}")
    return CloseResult(lines, True, final)


class GamblingSessionCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        economy.GAMBLING_RESULT_HOOKS.append(self._on_gambling_result)

    def cog_unload(self):
        try:
            economy.GAMBLING_RESULT_HOOKS.remove(self._on_gambling_result)
        except ValueError:
            pass

    async def bot_check(self, ctx: commands.Context) -> bool:
        """Bot-wide gate (a Cog special method, registered for every command):
        inside a gambling thread only GAMBLING_THREAD_COMMANDS run — for
        anyone, not just the owner. Group subcommands pass on their root
        name."""
        if ctx.command is None or ctx.channel.id not in state.gambling_threads:
            return True
        if ctx.command.qualified_name.split(" ")[0] in GAMBLING_THREAD_COMMANDS:
            return True
        try:
            await _wrong_channel_reply(
                ctx, "Only gambling works in this thread:\n" + _command_list(),
                title="🎰 Gambling Thread",
            )
        except discord.HTTPException:
            pass  # can't reply here (thread locked, no send rights) — still deny
        raise GamblingThreadOnly()

    # ── the tally and the thread name ────────────────────────────────────────

    def _thread(self, thread_id: int):
        return self.bot.get_channel(thread_id) if self.bot is not None else None

    def _display_name(self, thread_id: int, guild_id: int, uid: int) -> str | None:
        guild = getattr(self._thread(thread_id), "guild", None)
        if guild is None and self.bot is not None:
            guild = self.bot.get_guild(guild_id)
        member = guild.get_member(uid) if guild is not None else None
        return member.display_name if member is not None else None

    async def _on_gambling_result(self, channel_id: int, guild_id: int, uid: int, net: int) -> None:
        """economy.GAMBLING_RESULT_HOOKS entry: fold a result into its
        thread's tally and line up a rename. Results anywhere else are
        ignored."""
        row = state.gambling_threads.get(channel_id)
        if row is None or net == 0:
            return
        entry = row.setdefault("tally", {}).setdefault(uid, {"net": 0, "name": None})
        entry["net"] += int(net)
        name = self._display_name(channel_id, guild_id, uid)
        if name:
            entry["name"] = name
        await persistence.save_gambling_thread(channel_id)
        self._schedule_rename(channel_id)

    def _schedule_rename(self, thread_id: int) -> None:
        """Rename now if the budget allows, else queue one delayed rename;
        a queued rename reads the tally when it fires, so later results
        need no task of their own."""
        row = state.gambling_threads.get(thread_id)
        if row is None:
            return
        task = row.get("_rename_task")
        if task is not None and not task.done():
            return
        delay = max(0.0, row.get("_renamed_at", 0.0) + RENAME_MIN_INTERVAL - time.monotonic())
        row["_rename_task"] = asyncio.create_task(self._rename_after(thread_id, delay))

    async def _rename_after(self, thread_id: int, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        row = state.gambling_threads.get(thread_id)
        if row is None:
            return  # closed while we waited
        title = leader_title(row)
        thread = self._thread(thread_id)
        if title is None or thread is None or thread.name == title:
            return
        try:
            await thread.edit(name=title)
        except discord.HTTPException as e:
            log.warning("session: rename of thread %s failed (%s)", thread_id, type(e).__name__)
            return
        row["_renamed_at"] = time.monotonic()

    # ── !session ─────────────────────────────────────────────────────────────

    async def _open_thread_for(self, guild, owner_id: int):
        """The owner's open gambling thread in this guild, or None. A row
        whose thread is gone from the cache (deleted, or archived while the
        bot was down — Discord evicts archived threads) is stale: drop it."""
        for tid, row in list(state.gambling_threads.items()):
            if row["guild_id"] != guild.id or row["owner_id"] != owner_id:
                continue
            thread = guild.get_thread(tid)
            if thread is not None:
                return thread
            state.gambling_threads.pop(tid, None)
            await persistence.delete_gambling_thread(tid)
        return None

    @commands.command(name="session", aliases=["thread"])
    async def cmd_session(self, ctx: commands.Context):
        if await check_game_channel(ctx, "Gambling"):
            return
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Server Only", "Gambling threads open from a server channel.", C_RED))
            return
        if isinstance(ctx.channel, discord.Thread):
            await ctx.send(embed=emb(
                "❌ Already a Thread",
                "Threads can't nest — run `!session` from the channel itself.",
                C_RED,
            ))
            return
        existing = await self._open_thread_for(ctx.guild, ctx.author.id)
        if existing is not None:
            await ctx.send(embed=emb(
                "🎰 Gambling Thread", f"You already have one open: {existing.mention}", C_GOLD,
            ))
            return
        thread = await _try_create_gambling_thread(ctx, NEW_THREAD_NAME)
        if thread is None:
            await ctx.send(embed=emb(
                "❌ Couldn't Open a Thread",
                "Check that I have **Create Public Threads** in this channel.",
                C_RED,
            ))
            return
        state.gambling_threads[thread.id] = {
            "owner_id": ctx.author.id,
            "guild_id": ctx.guild.id,
            "parent_id": ctx.channel.id,
            "created_at": int(time.time()),
            "tally": {},
        }
        await persistence.save_gambling_thread(thread.id)
        await thread.send(embed=opening_embed(ctx.author))
        await ctx.send(embed=emb("🎰 Gambling Thread", f"Opened {thread.mention}.", C_GOLD))

    # ── thread listeners ─────────────────────────────────────────────────────

    async def _forget(self, thread_id: int) -> bool:
        row = state.gambling_threads.pop(thread_id, None)
        if row is None:
            return False
        task = row.get("_rename_task")
        if task is not None and not task.done():
            task.cancel()
        await persistence.delete_gambling_thread(thread_id)
        return True

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        # Archived (by hand, or idle past the auto-archive window) means the
        # table is closed. `!stop` pops the row before it archives, so this
        # is a no-op on that path.
        if after.archived and not before.archived:
            await self._forget(after.id)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        if not await self._forget(thread.id):
            return
        # The live hands lost their table — nothing can act on them now.
        # Return the stakes, as chess does when its thread is deleted.
        for uid, game in list(state.active_blackjack_games.items()):
            if game.get("channel_id") != thread.id or game.get("pending"):
                continue
            state.active_blackjack_games.pop(uid, None)
            await retire_blackjack_buttons(game)
            await add_balance(uid, int(game.get("amount", 0)))
        log.info("session: gambling thread %s deleted; live hands refunded", thread.id)


async def setup(bot):
    await bot.add_cog(GamblingSessionCog(bot))
