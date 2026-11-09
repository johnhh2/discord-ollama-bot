"""The "Show IP address" button under `!gameservers`, `!mc`, `!vrising` and
`!vintagestory`.

Off until a server sets a minimum level (`!settings server-ip <level>`,
`cfg["server_ip_min_level"]`). The button is attached only when the invoker
is at that level, and every click re-checks the clicker, since anyone in the
channel can press it. The answer is ephemeral.

Minecraft and Vintage Story show their configured host (the DDNS name). V
Rising shows its current public IP, because its direct-connect box takes no
hostnames: its host resolved through DNS, or the bot's own public IP when that
resolves to a LAN address (the servers share the bot's connection).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

import aiohttp
import discord
from discord import ui

from src.config import (
    MC_SERVER_HOST, MC_SERVER_PORT, VRISING_SERVER_HOST,
    VINTAGESTORY_SERVER_HOST, VINTAGESTORY_SERVER_PORT,
)
from src.guild_config import get_guild_cfg
from src.level_unlocks import user_display_level
from src.permissions import is_silenced

logger = logging.getLogger(__name__)

MIN_LEVEL_KEY = "server_ip_min_level"
PUBLIC_IP_URL = "https://api.ipify.org"
LOOKUP_TIMEOUT_SECS = 5.0

# key → display name, in the order `!gameservers` lists them.
GAMES = {"minecraft": "⛏️ Minecraft", "vrising": "🧛 V Rising", "vintagestory": "🌿 Vintage Story"}


def configured(key: str) -> bool:
    return bool({"minecraft": MC_SERVER_HOST, "vrising": VRISING_SERVER_HOST,
                 "vintagestory": VINTAGESTORY_SERVER_HOST}[key])


def min_level(guild_id: "int | None") -> "int | None":
    """The guild's minimum level to see addresses, or None while it's off."""
    if not guild_id:
        return None
    return get_guild_cfg(guild_id).get(MIN_LEVEL_KEY)


def refusal(user_id: int, guild_id: "int | None") -> "str | None":
    """Why `user_id` may not see the addresses here, or None if they may."""
    required = min_level(guild_id)
    if required is None or is_silenced(user_id, guild_id):
        return "Server addresses aren't shared here."
    if user_display_level(user_id, guild_id) < required:
        return f"🔒 Server addresses unlock at **level {required}** in this server."
    return None


async def _public_ip(host: str) -> str:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, None, family=socket.AF_INET), LOOKUP_TIMEOUT_SECS)
    ip = infos[0][4][0]
    if ipaddress.ip_address(ip).is_global:
        return ip
    async with aiohttp.ClientSession() as session:
        async with session.get(PUBLIC_IP_URL, timeout=aiohttp.ClientTimeout(total=LOOKUP_TIMEOUT_SECS)) as resp:
            resp.raise_for_status()
            return str(ipaddress.ip_address((await resp.text()).strip()))


async def address_line(key: str) -> str:
    name = GAMES[key]
    if key == "minecraft":
        return f"{name}: `{MC_SERVER_HOST}` · port `{MC_SERVER_PORT}`"
    if key == "vintagestory":
        return f"{name}: `{VINTAGESTORY_SERVER_HOST}:{VINTAGESTORY_SERVER_PORT}`"
    try:
        return f"{name}: `{await _public_ip(VRISING_SERVER_HOST)}`"
    except Exception:
        logger.warning("[vrising] public IP lookup failed", exc_info=True)
        return f"{name}: couldn't look up the address right now; try again shortly."


class ShowAddressButton(ui.DynamicItem[ui.Button], template=r"srvaddr:(?P<keys>[a-z,]+)"):
    """Its custom_id carries the games, so a click still works after a
    restart (registered in GameServersCog.cog_load)."""

    def __init__(self, keys: "tuple[str, ...]"):
        super().__init__(ui.Button(label="Show IP address", emoji="📍", style=discord.ButtonStyle.secondary,
                                   custom_id="srvaddr:" + ",".join(keys)))
        self.keys = keys

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(tuple(k for k in match["keys"].split(",") if k in GAMES))

    async def callback(self, interaction: discord.Interaction):
        gid = interaction.guild.id if interaction.guild else None
        refused = refusal(interaction.user.id, gid)
        keys = [k for k in self.keys if configured(k)]
        if refused is None and not keys:
            refused = "None of these servers is configured anymore."
        if refused:
            await interaction.response.send_message(refused, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lines = await asyncio.gather(*(address_line(k) for k in keys))
        await interaction.followup.send("\n".join(lines), ephemeral=True)


def address_view(ctx, keys) -> "ui.View | None":
    """A view with the button for `ctx.author`, or None when it shouldn't show."""
    keys = tuple(k for k in GAMES if k in keys and configured(k))
    if not keys or refusal(ctx.author.id, ctx.guild.id if ctx.guild else None):
        return None
    view = ui.View(timeout=None)
    view.add_item(ShowAddressButton(keys))
    return view
