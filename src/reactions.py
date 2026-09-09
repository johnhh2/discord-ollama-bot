"""Reaction "buttons" that never miss an early click.

Discord throttles ``add_reaction`` to roughly four calls a second per
channel, so a message with five buttons takes over a second to finish
appearing — and players click the first one while the rest are still being
added. Two things used to swallow those clicks:

1. ``bot.wait_for("reaction_add", check=...)`` only listens while the
   ``wait_for`` is pending. Every caller seeded the buttons *first* and
   listened *second*, so a click during seeding went nowhere. The same
   listener also vanished between two ``wait_for`` calls: a bankheist join
   that landed while the lobby was busy editing its embed after the previous
   join was lost the same way.
2. Persistent ``on_raw_reaction_add`` listeners are always registered, but
   several sites wrote the state they key on (``state.active_events``,
   ``cfg["dailies_message_id"]``, the bounty row) *after* seeding, so a click
   during seeding found nothing to match and was ignored.

``ReactionCollector`` fixes (1): it registers a persistent listener the
moment it starts and queues every reaction on the message until the consumer
asks for it, so seeding happens *inside* the listening window and an early
click waits in the queue instead of vanishing. Sites of kind (2) are fixed by
ordering — register the state, *then* seed — and ``seed_reactions`` is the
one seeding loop they all share, so the rule has a single place to live.

Rule for new code: whatever the reaction handler keys on — the collector, the
game dict, the ``state`` entry, the DB row — must exist before the first
``add_reaction`` call. Seed last.
"""
import asyncio
import logging
from typing import Iterable

import aiohttp
import discord

# Errors that mean every later add would fail the same way (no Add Reactions
# permission, message already deleted): stop seeding at the first one.
_STOP_ERRORS = (discord.Forbidden, discord.NotFound)
# Everything else (a 5xx, a dropped connection) is per-call: skip and go on.
_SKIP_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError)


async def seed_reactions(message, emojis: Iterable[str], *, what: str = "reactions") -> bool:
    """Add ``emojis`` to ``message`` in order, one HTTP call each.

    Best-effort and never raises: a failed emoji is logged and skipped, except
    a permission or missing-message error, which would fail every later one
    too, so seeding stops there. Returns True when every emoji landed.

    Call this *after* whatever the reaction handler keys on is in place (see
    the module docstring) — the buttons appear one by one and users click the
    first ones while the rest are still on their way.
    """
    ok = True
    for emoji in emojis:
        try:
            await message.add_reaction(emoji)
        except _STOP_ERRORS as ex:
            logging.warning("[reactions] %s: can't add %s (%s) — stopping", what, emoji, ex)
            return False
        except _SKIP_ERRORS as ex:
            logging.warning("[reactions] %s: failed to add %s: %s", what, emoji, ex)
            ok = False
    return ok


class ReactionCollector:
    """Queue every reaction added to one message, from ``start()`` on.

    Replaces the ``bot.wait_for("reaction_add", check=...)`` loop::

        async with ReactionCollector(bot, msg) as reactions:
            await seed_reactions(msg, ["✅"], what="invite")   # clicks queue up
            emoji, user = await reactions.next(timeout=60)

    Listens on the *raw* event, so the message needn't be in discord.py's
    message cache (a busy channel evicts it within minutes). Reactions from
    the bot itself and from other bots are dropped. ``user`` is the guild
    ``Member`` carried by the payload when there is one, else the cached or
    fetched ``User``. ``next`` raises ``asyncio.TimeoutError`` like
    ``wait_for``.

    Reactions are queued unfiltered; the caller decides what counts when it
    dequeues, so a check that depends on state (a lobby slot filling) sees
    the state as of the moment the reaction is handled, not the moment it
    arrived.
    """

    def __init__(self, bot, message):
        self._bot = bot
        self.message_id = message.id
        self._queue: asyncio.Queue = asyncio.Queue()
        self._started = False

    def start(self) -> "ReactionCollector":
        """Register the listener. Synchronous, so nothing can slip in between
        the registration and the seeding that follows it."""
        if not self._started:
            self._bot.add_listener(self._on_raw_reaction_add, "on_raw_reaction_add")
            self._started = True
        return self

    def stop(self) -> None:
        if self._started:
            self._bot.remove_listener(self._on_raw_reaction_add, "on_raw_reaction_add")
            self._started = False

    async def __aenter__(self) -> "ReactionCollector":
        return self.start()

    async def __aexit__(self, *exc) -> bool:
        self.stop()
        return False

    async def _on_raw_reaction_add(self, payload) -> None:
        if payload.message_id != self.message_id:
            return
        me = self._bot.user
        if me is not None and payload.user_id == me.id:
            return
        user = getattr(payload, "member", None)
        if user is None:
            user = self._bot.get_user(payload.user_id)
        if user is None:
            try:
                user = await self._bot.fetch_user(payload.user_id)
            except discord.HTTPException:
                return
        if getattr(user, "bot", False):
            return
        self._queue.put_nowait((str(payload.emoji), user))

    async def next(self, timeout: "float | None" = None):
        """The next queued ``(emoji, user)``; ``asyncio.TimeoutError`` after
        ``timeout`` seconds with nothing to hand back."""
        return await asyncio.wait_for(self._queue.get(), timeout)
