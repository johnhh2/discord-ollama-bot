"""JSON-safe projections of the bot's in-memory state for the /state/* routes.

These feed the read-only MCP sidecar (botmcp/), which reaches the loopback
health server through the bot's network namespace and has no other way to
see what is live in this process. Everything here is an allowlist: counts,
ids, timings and switches. Nothing a user typed (puzzle answers, AI
histories, bounty conditions, heist embeds) and no discord.py objects leave
this module — see CLAUDE.md: Read-only MCP sidecar.

Pure functions of `state` and `bot`; no awaits, no DB.
"""
import math
import time

from src import ai, state, status_manager
from src.helpers import get_memory_mb, get_version


def _latency_ms(bot) -> "float | None":
    latency = getattr(bot, "latency", None)
    if latency is None or not math.isfinite(latency):
        return None
    return round(latency * 1000, 1)


def _guilds(bot) -> list:
    out = []
    for g in getattr(bot, "guilds", None) or ():
        out.append({
            "id": g.id,
            "name": g.name,
            "member_count": getattr(g, "member_count", None),
            "text_channels": len(getattr(g, "text_channels", ()) or ()),
            "voice_channels": len(getattr(g, "voice_channels", ()) or ()),
        })
    return out


def _db_pool() -> dict:
    from src import db
    pool = db._pool
    if pool is None:
        return {"connected": False}
    return {
        "connected": True,
        "size": getattr(pool, "size", None),
        "free": getattr(pool, "freesize", None),
        "max": getattr(pool, "maxsize", None),
    }


def overview(bot) -> dict:
    """Process-level facts: uptime, counters, connectivity, loaded-state sizes."""
    uptime = int(time.monotonic() - state.bot_start_time)
    return {
        "version": get_version(),
        "ready": bool(bot is not None and bot.is_ready()),
        "uptime_seconds": uptime,
        "started_at": int(time.time() - uptime),
        "discord_latency_ms": _latency_ms(bot),
        "memory_mb": round(get_memory_mb(), 1),
        "guilds": _guilds(bot),
        "counters": {
            "commands_ran": state.stats_commands_ran,
            "messages_seen": state.stats_messages_seen,
            "messages_today": state.stats_messages_today,
            "commands_today": state.stats_commands_today,
            "ai_responses_today": state.stats_ai_responses_today,
            "commands_today_by_cog": dict(state.stats_commands_today_by_cog),
            "lottery_tickets_today": dict(state.lottery_tickets_today),
        },
        "ai": {
            "enabled": bool(state.bot_settings.get("ai_enabled", True)),
            "streams_in_flight": len(ai._in_flight),
        },
        "minecraft": {
            "online": state.mc_last_online,
            "ping_ms": state.mc_last_ping_ms,
        },
        "db_pool": _db_pool(),
        "presence_statuses": status_manager.active_statuses(),
        "loaded": {
            "economy_users": len(state.economy["users"]),
            "guild_settings": len(state.guild_settings),
            "leveling_rows": len(state.leveling),
            "property_owners": len(state.property_owners),
            "user_artifacts": len(state.user_artifacts),
            "idle_characters": sum(len(c) for c in state.idle_characters.values()),
            "counters": sum(len(c) for c in state.counters.values()),
            "insurance_policies": len(state.insurance),
            "command_perms": len(state.command_perms),
        },
    }


def _channel_ids(games: dict) -> list:
    return sorted(int(k) for k in games.keys())


def activity(bot) -> dict:
    """What is live right now: games, lobbies, effects, threads — by count and
    channel id only. Keyed-by-channel dicts give their keys; a game's board,
    players' Member objects and a puzzle's answer never do."""
    economy_cog = bot.get_cog("EconomyCog") if bot is not None else None
    heists = getattr(economy_cog, "_active_heists", {}) or {}
    crimes = getattr(economy_cog, "_crime_active", set()) or set()
    ai_threads_by_kind: dict = {}
    for row in state.ai_threads.values():
        kind = str(row.get("kind", "unknown"))
        ai_threads_by_kind[kind] = ai_threads_by_kind.get(kind, 0) + 1
    return {
        "games": {
            "blackjack": {"count": len(state.active_blackjack_games)},
            "hangman": {"count": len(state.active_hangman_games), "channels": _channel_ids(state.active_hangman_games)},
            "tictactoe": {"count": len(state.active_ttt_games), "channels": _channel_ids(state.active_ttt_games)},
            "connect4": {"count": len(state.active_c4_games), "channels": _channel_ids(state.active_c4_games)},
            "race": {"count": len(state.active_race_games), "channels": _channel_ids(state.active_race_games)},
            "chess": {"count": len(state.active_chess_games), "channels": _channel_ids(state.active_chess_games)},
            "puzzles": {"count": len(state.active_puzzles), "channels": _channel_ids(state.active_puzzles)},
        },
        "heists": {"count": len(heists), "channels": _channel_ids(heists)},
        "crimes_in_progress": len(crimes),
        "coin_events": {"count": len(state.active_events), "message_ids": _channel_ids(state.active_events)},
        "effects": {
            "mocks": len(state.active_mocks),
            "ragebaits": len(state.active_ragebaits),
            "taxes": len(state.active_taxes),
            "curses": len(state.active_curses),
            "insurance": len(state.insurance),
        },
        "bounties_open": len(state.active_bounties),
        "ai_threads": {"count": len(state.ai_threads), "by_kind": ai_threads_by_kind},
        "gambling_threads": len(state.gambling_threads),
        "idle": {
            "guilds": len(state.idle_characters),
            "characters": sum(len(c) for c in state.idle_characters.values()),
            "quests_running": sum(1 for q in state.idle_quests.values() if q.get("ends_at")),
            "world_events": sum(len(rows) for rows in state.idle_guild_events.values()),
        },
    }


VIEWS = {
    "overview": overview,
    "activity": activity,
}
