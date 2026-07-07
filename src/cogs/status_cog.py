"""Rotates the bot's presence text through all active statuses.

Sources register providers with src.status_manager; this cog is the only
place that calls bot.change_presence. Each tick it re-evaluates the
providers, advances to the next visible status, and only talks to Discord
when the displayed text actually changes (presence updates are heavily
rate-limited). With no visible statuses the presence is cleared.
"""
import logging

import discord
from discord.ext import commands, tasks

from src import status_manager

logger = logging.getLogger(__name__)

STATUS_ROTATE_SECONDS = 30


class StatusCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._idx = 0
        self._current: "str | None" = None
        self.rotate_status.start()

    def cog_unload(self):
        self.rotate_status.cancel()

    @tasks.loop(seconds=STATUS_ROTATE_SECONDS)
    async def rotate_status(self):
        statuses = status_manager.active_statuses()
        if statuses:
            self._idx %= len(statuses)
            name = statuses[self._idx]
            self._idx += 1
        else:
            name = None
        if name == self._current:
            return
        try:
            activity = (
                discord.Activity(type=discord.ActivityType.watching, name=name)
                if name else None
            )
            await self.bot.change_presence(activity=activity)
            self._current = name
        except Exception:
            logger.exception("[status] presence update failed")

    @rotate_status.before_loop
    async def _before_rotate(self):
        # Providers read loaded state (economy counters, monitor results).
        await self.bot.wait_until_ready()
        from src.persistence import init_done
        await init_done.wait()


async def setup(bot):
    await bot.add_cog(StatusCog(bot))
