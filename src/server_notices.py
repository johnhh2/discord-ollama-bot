"""Channel notices for monitored game servers (Minecraft, V Rising, Vintage Story).

The cogs fold each poll into events (minecraft_cog._mc_events); this module
words them and posts them to every guild's `!settings channel game-servers`.
"""
import logging

from src.helpers import emb, C_GREEN, C_RED, C_BLUE, C_ORANGE
from src.guild_config import get_guild_cfg

logger = logging.getLogger(__name__)

JOIN_EVENTS = ("count_up", "count_down")


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric prefix of a version string, for ordering: "1.21.51" →
    (1, 21, 51); anything unparsable contributes nothing ("" → ())."""
    parts = []
    for piece in version.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def event_payloads(game: str, label: str, events: list[str], prev, status,
                   last_seen_online: "float | None") -> list[tuple[str, dict]]:
    """Turn events into (event, channel.send kwargs) pairs. `label` is how the
    text names the server ("The Minecraft server")."""
    # An empty server's "0/10 online" is noise, so the count is left off.
    online = f"**{status.players}/{status.max_players}** online" if status and status.players else ""
    payloads = []
    for ev in events:
        if ev == "came_online":
            desc = f"{label} is back up" + (f" — {online}." if online else ".")
            payloads.append((ev, {"embed": emb(f"🟢 {game} Server Online", desc, C_GREEN)}))
        elif ev == "went_offline":
            desc = f"{label} stopped responding."
            if last_seen_online:
                desc += f"\nLast seen online <t:{int(last_seen_online)}:R>."
            payloads.append((ev, {"embed": emb(f"🔴 {game} Server Offline", desc, C_RED)}))
        elif ev == "count_up":
            delta = status.players - prev.count
            who = "A player" if delta == 1 else f"{delta} players"
            payloads.append((ev, {"embed": emb(
                "🟢 Player Joined", f"{who} joined the {game} server — {online}", C_GREEN)}))
        elif ev == "count_down":
            delta = prev.count - status.players
            who = "A player" if delta == 1 else f"{delta} players"
            desc = f"{who} left the {game} server" + (f" — {online}" if online else " — it's empty now")
            payloads.append((ev, {"embed": emb("🔴 Player Left", desc, C_RED)}))
        elif ev == "version_changed":
            old, new = _version_key(prev.version), _version_key(status.version)
            rollback = bool(old and new and new < old)
            title = f"⬇️ {game} Server Rolled Back" if rollback else f"⬆️ {game} Server Updated"
            what = "rolled back" if rollback else "updated"
            # This embed replaces the "back online" one when the monitor
            # saw the server go down for the update.
            lead = "is back up," if prev.online is False else "was"
            desc = f"{label} {lead} {what}: **{prev.version}** → **{status.version}**"
            if online:
                desc += f" — {online}"
            payloads.append((ev, {"embed": emb(title, desc, C_ORANGE if rollback else C_BLUE)}))
    return payloads


async def announce(bot, payloads: list[tuple[str, dict]], *, joins_key: "str | None" = None):
    """Post to each guild's game-servers channel; join/leave notices only
    where `cfg[joins_key]` isn't switched off (missing means on)."""
    for guild in bot.guilds:
        cfg = get_guild_cfg(guild.id)
        channel_id = cfg.get("game_servers_channel")
        if not channel_id:
            continue
        wanted = [payload for ev, payload in payloads
                  if ev not in JOIN_EVENTS or joins_key is None or cfg.get(joins_key, True)]
        if not wanted:
            continue
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            for payload in wanted:
                await channel.send(silent=True, **payload)
        except Exception:
            logger.exception("[game servers] announce failed for guild %s", guild.id)
