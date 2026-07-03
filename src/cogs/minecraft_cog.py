"""Minecraft Bedrock server status & monitor.

Talks to the itzg/minecraft-bedrock-server container over the same public UDP
port players use (RakNet "unconnected ping" via mcstatus). MC_SERVER_HOST is
the server's EXTERNAL address so latency reflects the internet-facing route.
The Bedrock pong carries player *counts* only, never names — so the monitor
posts anonymous "a player joined — 3/10" notices, not per-gamertag events.
Named join/leave, playtime, and console commands would all need docker-socket
access (deliberately not mounted).
"""
import asyncio
import dataclasses
import logging
import time

import discord
from discord.ext import commands, tasks
from mcstatus import BedrockServer

from src import state
from src.config import (
    MC_SERVER_HOST, MC_SERVER_PORT, MC_POLL_SECONDS, MC_SERVER_SHOW_IP,
)
from src.helpers import emb, C_GREEN, C_RED, C_GREY
from src.guild_config import get_guild_cfg
from src.permissions import requires_perm

logger = logging.getLogger(__name__)

MC_STATUS_TIMEOUT_SECS = 5.0
# Consecutive failed pings before the monitor declares the server offline.
# One lost UDP packet must not produce an offline/online alert pair.
MC_OFFLINE_AFTER_FAILURES = 2


@dataclasses.dataclass
class McStatus:
    players: int
    max_players: int
    latency_ms: int
    version: str
    motd: str
    gamemode: str
    map_name: str


@dataclasses.dataclass
class MonitorState:
    # online is tri-state: None = no confirmed baseline yet (bot just booted),
    # so the first observation never fires an alert — only changes do.
    online: bool | None = None
    count: int = 0
    fail_streak: int = 0


def _server_label() -> str:
    """How embeds refer to the server. The address is only exposed when the
    operator opts in via MC_SERVER_SHOW_IP."""
    if MC_SERVER_SHOW_IP:
        return f"**{MC_SERVER_HOST}:{MC_SERVER_PORT}**"
    return "The Minecraft server"


async def fetch_mc_status() -> "McStatus | None":
    """One status ping. Returns None if the server didn't answer.

    Module-level (not a method) so tests can monkeypatch
    src.cogs.minecraft_cog.fetch_mc_status.
    """
    try:
        server = BedrockServer.lookup(f"{MC_SERVER_HOST}:{MC_SERVER_PORT}")
        status = await asyncio.wait_for(
            server.async_status(), timeout=MC_STATUS_TIMEOUT_SECS
        )
    except Exception:
        return None
    return McStatus(
        players=status.players.online,
        max_players=status.players.max,
        latency_ms=round(status.latency),
        version=status.version.name or "",
        motd=status.motd.to_plain().strip(),
        gamemode=status.gamemode or "",
        map_name=status.map_name or "",
    )


def _mc_events(prev: MonitorState, status: "McStatus | None") -> tuple[MonitorState, list[str]]:
    """Fold one poll result into the monitor state.

    Pure — returns (next_state, events); the loop owns all I/O. Events:
    "came_online", "went_offline", "count_up", "count_down".
    """
    if status is None:
        streak = prev.fail_streak + 1
        if prev.online and streak >= MC_OFFLINE_AFTER_FAILURES:
            return MonitorState(online=False, count=0, fail_streak=streak), ["went_offline"]
        # Not enough failures yet, or we never saw it up (don't alert on a
        # server that was already down when the bot booted).
        return MonitorState(online=prev.online, count=prev.count, fail_streak=streak), []

    events = []
    if prev.online is False:
        events.append("came_online")
    elif prev.online is True:
        if status.players > prev.count:
            events.append("count_up")
        elif status.players < prev.count:
            events.append("count_down")
    # prev.online is None → first confirmed sighting; baseline silently.
    return MonitorState(online=True, count=status.players, fail_streak=0), events


class MinecraftCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._monitor = MonitorState()
        self._last_seen_online: float | None = None
        self._presence_str: str | None = None
        if MC_SERVER_HOST:
            self.mc_monitor.start()

    def cog_unload(self):
        if MC_SERVER_HOST:
            self.mc_monitor.cancel()

    # ── !mc ───────────────────────────────────────────────────────────────────
    @commands.command(name="mc", aliases=["minecraft", "mcstatus"])
    @requires_perm
    async def cmd_mc(self, ctx: commands.Context):
        if not MC_SERVER_HOST:
            await ctx.send(embed=emb(
                "⛏️ Minecraft",
                "Minecraft integration isn't configured (set `MC_SERVER_HOST`).",
                C_GREY,
            ))
            return

        status = await fetch_mc_status()
        if status is None:
            desc = f"{_server_label()} didn't respond."
            if self._last_seen_online:
                desc += f"\nLast seen online <t:{int(self._last_seen_online)}:R>."
            await ctx.send(embed=emb("🔴 Server Offline", desc, C_RED))
            return

        self._last_seen_online = time.time()
        embed = discord.Embed(title="🟢 Minecraft Bedrock Server", color=C_GREEN)
        if MC_SERVER_SHOW_IP:
            embed.description = f"**{MC_SERVER_HOST}:{MC_SERVER_PORT}**"
        embed.add_field(name="Players", value=f"{status.players}/{status.max_players}")
        embed.add_field(name="Ping", value=f"{status.latency_ms} ms")
        embed.add_field(name="Version", value=status.version or "?")
        if status.motd:
            embed.add_field(name="MOTD", value=status.motd, inline=False)
        if status.gamemode:
            embed.add_field(name="Gamemode", value=status.gamemode)
        if status.map_name:
            embed.add_field(name="World", value=status.map_name)
        await ctx.send(embed=embed)

    # ── Monitor loop ──────────────────────────────────────────────────────────
    @tasks.loop(seconds=MC_POLL_SECONDS)
    async def mc_monitor(self):
        status = await fetch_mc_status()
        prev = self._monitor
        self._monitor, events = _mc_events(prev, status)
        if status is not None:
            self._last_seen_online = time.time()

        # Publish the sample for the graph scheduler's bot-stats snapshot.
        # During the offline debounce window online stays True — that's fine,
        # the graph treats "up but no ping this sample" as no data point.
        state.mc_last_online = self._monitor.online
        state.mc_last_ping_ms = float(status.latency_ms) if status else None

        if events:
            await self._announce(self._event_payloads(events, prev, status))
        await self._update_presence(status)

    @mc_monitor.before_loop
    async def _before_monitor(self):
        await self.bot.wait_until_ready()
        import src.persistence as _pkg
        await _pkg.init_done.wait()

    def _event_payloads(self, events: list[str], prev: MonitorState,
                        status: "McStatus | None") -> list[dict]:
        """Turn events into channel.send kwargs (colored embeds)."""
        payloads = []
        for ev in events:
            if ev == "came_online":
                payloads.append({"embed": emb(
                    "🟢 Minecraft Server Online",
                    f"{_server_label()} is back up — "
                    f"**{status.players}/{status.max_players}** online.",
                    C_GREEN,
                )})
            elif ev == "went_offline":
                desc = f"{_server_label()} stopped responding."
                if self._last_seen_online:
                    desc += f"\nLast seen online <t:{int(self._last_seen_online)}:R>."
                payloads.append({"embed": emb("🔴 Minecraft Server Offline", desc, C_RED)})
            elif ev == "count_up":
                delta = status.players - prev.count
                who = "A player" if delta == 1 else f"{delta} players"
                payloads.append({"embed": emb(
                    "🟢 Player Joined",
                    f"{who} joined the Minecraft server — "
                    f"**{status.players}/{status.max_players}** online",
                    C_GREEN,
                )})
            elif ev == "count_down":
                delta = prev.count - status.players
                who = "A player" if delta == 1 else f"{delta} players"
                payloads.append({"embed": emb(
                    "🔴 Player Left",
                    f"{who} left the Minecraft server — "
                    f"**{status.players}/{status.max_players}** online",
                    C_RED,
                )})
        return payloads

    async def _announce(self, payloads: list[dict]):
        for guild in self.bot.guilds:
            channel_id = get_guild_cfg(guild.id).get("minecraft_channel")
            if not channel_id:
                continue
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                for payload in payloads:
                    await channel.send(**payload)
            except Exception:
                logger.exception("[minecraft] announce failed for guild %s", guild.id)

    async def _update_presence(self, status: "McStatus | None"):
        if status is not None:
            world = status.map_name or "Bedrock"
            name = f"⛏️ {status.players}/{status.max_players} on {world}"
        elif self._monitor.online is False:
            name = "⛏️ server offline"
        else:
            return  # no confirmed baseline yet — leave presence alone
        if name == self._presence_str:
            return  # presence updates are heavily rate-limited; only send changes
        try:
            await self.bot.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name=name)
            )
            self._presence_str = name
        except Exception:
            logger.exception("[minecraft] presence update failed")


async def setup(bot):
    await bot.add_cog(MinecraftCog(bot))
