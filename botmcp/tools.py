"""The tool catalog: named, parameterised reads. Every tool is a plain async
function here (so the tests call them directly) and `register` hangs them
on the server with read-only annotations and an audit line per call.

Rules for a new tool: go through `query.select` (never raw SQL); take ids
and enums, never free text that reaches the query; cap rows; return a dict;
and remember `redact.scrub` runs on the result, so a key named like
something sensitive vanishes — pick plain names.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import re
import time
from typing import Any, Literal

from mcp.types import ToolAnnotations

from botmcp import catalog, query, redact, state_client

log = logging.getLogger("botmcp.audit")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WORD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ToolInputError(ValueError):
    """A caller's argument was bad. Goes back to the model as the tool result."""


def _date(value: "str | None", name: str) -> "str | None":
    if value is None:
        return None
    if not _DATE_RE.match(value):
        raise ToolInputError(f"{name} must be YYYY-MM-DD")
    return value


def _word(value: "str | None", name: str) -> "str | None":
    if value is None:
        return None
    if not _WORD_RE.match(value):
        raise ToolInputError(f"{name} may only contain letters, digits, '_', '.' and '-'")
    return value


def _json(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _truncated(rows: list, limit: int) -> dict[str, Any]:
    return {"rows": rows, "count": len(rows), "truncated": len(rows) >= limit}


# ── program state (via the bot's loopback /state views) ──────────────────────

async def bot_status() -> dict[str, Any]:
    """Uptime, counters, Discord latency, Ollama and DB connectivity, guild list and
    the sizes of the state the bot has loaded — plus the schema version."""
    overview = await state_client.view("overview")
    health = await state_client.health()
    latest = await query.select("schema_migrations", ("version", "name", "applied_at"), order_by="version DESC", limit=1)
    return {"health": health, "overview": overview, "schema": latest[0] if latest else None}


async def live_activity() -> dict[str, Any]:
    """What is live in the bot right now: games, heists, coin drops, effects and
    AI threads, by count and channel id."""
    return await state_client.view("activity")


# ── economy ──────────────────────────────────────────────────────────────────

async def economy_summary() -> dict[str, Any]:
    """Bot-wide totals: coins in wallets and savings, house pots, jackpot, lottery
    pools, deeds, artifacts and insurance policies."""
    wallets = await query.select("economy_users", (("COUNT(user_id)", "users"), ("COALESCE(SUM(balance), 0)", "wallet_total")))
    savings_rows = await query.select("economy_users", ("savings",), where="savings IS NOT NULL", limit=query.MAX_ROWS)
    savings_total = 0
    for row in savings_rows:
        for entry in _json(row["savings"], []):
            if isinstance(entry, dict):
                savings_total += int(entry.get("amount", 0) or 0)
    house = await query.select("guild_house_balance", ("guild_id", "balance"), order_by="balance DESC")
    jackpot = await query.select("slots_jackpot", ("jackpot",), limit=1)
    lottery = await query.select("lottery", ("guild_id", "prize_pool", "last_drawn_week"))
    deeds = await query.select("property_owners", (("COUNT(property_id)", "owned"), ("COUNT(list_price)", "listed")))
    artifacts = await query.select("user_artifacts", (("COUNT(DISTINCT user_id)", "owners"), ("COALESCE(SUM(quantity), 0)", "quantity")))
    insured = await query.select("shop_effects", (("COUNT(user_id)", "policies"),), where="effect_type = %s", params=("insurance",))
    return {
        "users": wallets[0]["users"],
        "wallet_total": int(wallets[0]["wallet_total"] or 0),
        "savings_deposited_total": savings_total,
        "house_pots": house,
        "slots_jackpot": jackpot[0]["jackpot"] if jackpot else None,
        "lottery": lottery,
        "properties": deeds[0],
        "artifacts": artifacts[0],
        "insurance_policies": insured[0]["policies"],
    }


async def user_economy(user_id: int) -> dict[str, Any]:
    """One user's wallet, savings, jail state, levels per server, artifacts, deeds,
    streaks, chess stats and insurance."""
    user = await query.select_one(
        "economy_users",
        ("user_id", "balance", "savings", "daily_date", "scratch_used", "scratch_date", "jail_until", "bail_amount",
         "crime_eligible", "property_revenue_total", "daily_gamble_property", "ins_paid_since_claim", "ins_lapsed_since_claim"),
        where="user_id = %s", params=(user_id,),
    )
    if user is not None:
        deposits = [e for e in _json(user.pop("savings"), []) if isinstance(e, dict)]
        user["savings_deposits"] = len(deposits)
        user["savings_deposited"] = sum(int(e.get("amount", 0) or 0) for e in deposits)
        user["in_jail"] = (user.get("jail_until") or 0) > time.time()
    levels = await query.select("leveling", ("guild_id", "data"), where="user_id = %s", params=(user_id,))
    artifacts = await query.select("user_artifacts", ("artifact_id", "quantity", "acquired_at"), where="user_id = %s", params=(user_id,))
    deeds = await query.select("property_owners", ("property_id", "acquired_at", "list_price", "upgraded"), where="owner_id = %s", params=(user_id,))
    command_streak = await query.select_one("command_streak", ("last_date", "streak_count"), where="user_id = %s", params=(user_id,))
    gambler_streak = await query.select_one("gambler_streak", ("last_full_date", "streak_count"), where="user_id = %s", params=(user_id,))
    chess = await query.select_one("chess_user_stats", ("max_elo_defeated", "total_elo_defeated", "elo_spent"), where="user_id = %s", params=(user_id,))
    insurance = await query.select_one("shop_effects", ("insurance_tier", "expires_at"), where="user_id = %s AND effect_type = %s", params=(user_id, "insurance"))
    blocks = await query.select_one("mc_players", ("blocks", "linked_at"), where="user_id = %s", params=(user_id,))
    return {
        "user_id": user_id,
        "economy": user,
        "levels": [{"guild_id": r["guild_id"], **_project_leveling(r["data"])} for r in levels],
        "artifacts": artifacts,
        "properties": deeds,
        "command_streak": command_streak,
        "gambler_streak": gambler_streak,
        "chess": chess,
        "insurance": insurance,
        "minecraft_blocks": blocks,
    }


def _project_leveling(data) -> dict[str, Any]:
    parsed = _json(data, {})
    return {k: parsed.get(k) for k in catalog.LEVELING_DATA_KEYS}


Metric = Literal["balance", "level", "artifacts", "properties", "command_streak", "gambler_streak", "chess", "idle", "mc_blocks"]


async def leaderboard(metric: Metric, guild_id: "int | None" = None, limit: int = 10) -> dict[str, Any]:
    """Top users by one metric. `level` and `idle` are per server and need guild_id;
    the rest are bot-wide and ignore it."""
    limit = query.clamp_limit(limit)
    per_guild = metric in ("level", "idle")
    if per_guild and guild_id is None:
        raise ToolInputError(f"{metric} is per server: pass guild_id")
    if metric == "balance":
        rows = await query.select("economy_users", ("user_id", "balance"), order_by="balance DESC", limit=limit)
    elif metric == "level":
        raw = await query.select("leveling", ("user_id", "data"), where="guild_id = %s", params=(guild_id,), limit=query.MAX_ROWS)
        rows = sorted(({"user_id": r["user_id"], **_project_leveling(r["data"])} for r in raw),
                      key=lambda r: (r.get("level") or 0, r.get("xp") or 0), reverse=True)[:limit]
    elif metric == "artifacts":
        rows = await query.select("user_artifacts", ("user_id", ("SUM(quantity)", "artifacts")), group_by="user_id", order_by="artifacts DESC", limit=limit)
    elif metric == "properties":
        rows = await query.select("property_owners", ("owner_id", ("COUNT(property_id)", "properties")),
                                  group_by="owner_id", order_by="properties DESC", limit=limit)
    elif metric == "command_streak":
        rows = await query.select("command_streak", ("user_id", "streak_count", "last_date"), order_by="streak_count DESC", limit=limit)
    elif metric == "gambler_streak":
        rows = await query.select("gambler_streak", ("user_id", "streak_count", "last_full_date"), order_by="streak_count DESC", limit=limit)
    elif metric == "chess":
        rows = await query.select("chess_user_stats", ("user_id", "max_elo_defeated", "total_elo_defeated"), order_by="max_elo_defeated DESC", limit=limit)
    elif metric == "idle":
        rows = await query.select("idle_characters", ("user_id", "class_name", "level", "prestige", "gold", "claimed"),
                                  where="guild_id = %s", params=(guild_id,), order_by="level DESC, prestige DESC", limit=limit)
    elif metric == "mc_blocks":
        rows = await query.select("mc_players", ("user_id", "blocks"), order_by="blocks DESC", limit=limit)
    else:
        raise ToolInputError(f"unknown metric {metric!r}")
    return {"metric": metric, "guild_id": guild_id if per_guild else None, "rows": rows}


async def records(guild_id: int, category_prefix: "str | None" = None) -> dict[str, Any]:
    """A server's all-time records (slots jackpot, biggest loss, crime scores, …)
    with the holder's id. Filter by a category prefix such as `chess` or `crime`."""
    prefix = _word(category_prefix, "category_prefix")
    where, params = "guild_id = %s", [guild_id]
    if prefix:
        where += " AND category LIKE %s"
        params.append(prefix.replace("%", "").replace("_", r"\_") + "%")
    rows = await query.select("records", ("category", "value", "holder_id", "extra_json"), where=where, params=params, order_by="category")
    for r in rows:
        r["extra"] = _json(r.pop("extra_json"), None)
    return {"guild_id": guild_id, "rows": rows}


Series = Literal["bot_stats", "commands", "balance", "crime", "gambling", "levelups"]


async def history(
    series: Series,
    since: "str | None" = None,
    until: "str | None" = None,
    guild_id: "int | None" = None,
    user_id: "int | None" = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Six-hourly snapshot rows (`bucket` 0–3 splits a day into 6h windows).
    `balance` needs user_id; `crime`, `gambling` and `levelups` take guild_id
    and/or user_id. Dates are YYYY-MM-DD, newest first."""
    since, until = _date(since, "since"), _date(until, "until")
    limit = query.clamp_limit(limit)
    where, params = [], []
    if since:
        where.append("snapshot_date >= %s")
        params.append(since)
    if until:
        where.append("snapshot_date <= %s")
        params.append(until)
    order = "snapshot_date DESC, bucket DESC"
    if series == "bot_stats":
        table, cols = "bot_stats_history", ("snapshot_date", "bucket", "messages", "commands", "ai_responses", "ai_up", "memory_mb", "ping_ms", "mc_up", "mc_ping_ms")
    elif series == "commands":
        table, cols = "bot_command_usage_history", ("snapshot_date", "bucket", "cog_name", "count")
    elif series == "balance":
        if user_id is None:
            raise ToolInputError("balance history is per user: pass user_id")
        table, cols = "balance_history", ("snapshot_date", "bucket", "wallet", "savings", "assets", "asset_revenue")
        where.append("user_id = %s")
        params.append(user_id)
    elif series in ("crime", "gambling", "levelups"):
        table = {"crime": "crime_history", "gambling": "gambling_history", "levelups": "levelup_history"}[series]
        cols = ("snapshot_date", "bucket", "guild_id", "user_id") + (("count",) if series == "levelups" else ("gained", "lost"))
        if guild_id is not None:
            where.append("guild_id = %s")
            params.append(guild_id)
        if user_id is not None:
            where.append("user_id = %s")
            params.append(user_id)
    else:
        raise ToolInputError(f"unknown series {series!r}")
    rows = await query.select(table, cols, where=" AND ".join(where) or None, params=params, order_by=order, limit=limit)
    return {"series": series, **_truncated(rows, limit)}


# ── servers ──────────────────────────────────────────────────────────────────

async def guilds() -> dict[str, Any]:
    """Every server the bot knows: name and size from the live bot (when up),
    plus how many members have leveling rows and the house pot, from the DB."""
    overview = await state_client.view("overview")
    live = {g["id"]: g for g in overview.get("guilds", [])} if "guilds" in overview else {}
    members = await query.select("leveling", ("guild_id", ("COUNT(user_id)", "leveling_rows")), group_by="guild_id", order_by="leveling_rows DESC")
    house = {r["guild_id"]: r["balance"] for r in await query.select("guild_house_balance", ("guild_id", "balance"))}
    ids = sorted({r["guild_id"] for r in members} | set(house) | set(live))
    rows = []
    by_members = {r["guild_id"]: r["leveling_rows"] for r in members}
    for gid in ids:
        rows.append({
            "guild_id": gid,
            "name": live.get(gid, {}).get("name"),
            "member_count": live.get(gid, {}).get("member_count"),
            "bot_present": gid in live if live else None,
            "leveling_rows": by_members.get(gid, 0),
            "house_pot": house.get(gid, 0),
        })
    return {"rows": rows, "bot_reachable": "unavailable" not in overview}


async def guild_settings(guild_id: int) -> dict[str, Any]:
    """A server's configuration: feature switches, channel assignments, models and
    limits. Keys outside the allowlist are named under `omitted_keys`, not shown."""
    row = await query.select_one("guild_settings", ("settings_json",), where="guild_id = %s", params=(guild_id,))
    if row is None:
        return {"guild_id": guild_id, "settings": None}
    cfg = _json(row["settings_json"], {})
    shown = {k: cfg[k] for k in catalog.GUILD_SETTINGS_KEYS if k in cfg}
    return {
        "guild_id": guild_id,
        "settings": shown,
        "omitted_keys": sorted(k for k in cfg if k not in catalog.GUILD_SETTINGS_KEYS),
    }


async def active_effects(guild_id: "int | None" = None) -> dict[str, Any]:
    """Shop effects in force (mock, ragebait, tax, curse, insurance): who holds
    which, bought by whom, and what remains. guild_id 0 is bot-wide insurance."""
    where, params = None, []
    if guild_id is not None:
        where, params = "guild_id = %s", [guild_id]
    rows = await query.select(
        "shop_effects",
        ("guild_id", "user_id", "effect_type", "remaining", "started_by", "cursed_by", "master_id", "tax_type",
         "tax_emoji", "channel_id", "activated_at", "expires_at", "insurance_tier"),
        where=where, params=params, order_by="effect_type, guild_id",
    )
    return _truncated(rows, query.MAX_ROWS)


async def counters(guild_id: int) -> dict[str, Any]:
    """A server's custom `!count` tallies: each counter's kind and total, and the
    per-user values behind it."""
    defs = await query.select("counters", ("name", "kind", "user_required", "created_by", "created_at"),
                              where="guild_id = %s", params=(guild_id,), order_by="name")
    values = await query.select("counter_values", ("name", "user_id", "value"), where="guild_id = %s", params=(guild_id,), order_by="name, value DESC")
    by_name: dict = {}
    for v in values:
        by_name.setdefault(v["name"], []).append({"user_id": v["user_id"], "value": v["value"]})
    for d in defs:
        d["values"] = by_name.get(d["name"], [])
        d["total"] = sum(v["value"] for v in d["values"])
    return {"guild_id": guild_id, "rows": defs}


async def command_permissions() -> dict[str, Any]:
    """Every command's permission tier (everyone, server_admin, bot_admin,
    global_admin) and whether a denial is silent."""
    rows = await query.select("command_perms", ("command_name", "tier", "hidden"), order_by="command_name")
    return {"rows": rows}


async def migrations() -> dict[str, Any]:
    """Applied schema migrations, newest first."""
    rows = await query.select("schema_migrations", ("version", "name", "applied_at"), order_by="version DESC")
    return {"rows": rows}


# ── games ────────────────────────────────────────────────────────────────────

async def idle_overview(guild_id: int) -> dict[str, Any]:
    """A server's idle RPG: every character's class, level, gold, position and
    kills, the running quest, and the world events and blessings in force."""
    chars = await query.select(
        "idle_characters",
        ("user_id", "class_name", "level", "prestige", "gold", "hp", "x", "y", "law", "moral", "claimed", "title",
         "mob_kills", "mob_deaths", "hunts_done", "travel_to", "next_level_at", "remaining", "last_seen"),
        where="guild_id = %s", params=(guild_id,), order_by="level DESC, prestige DESC",
    )
    quest = await query.select_one("idle_quests", ("kind", "stage", "description", "members_json", "ends_at", "not_before", "p1x", "p1y", "p2x", "p2y"),
                                   where="guild_id = %s", params=(guild_id,))
    if quest is not None:
        quest["members"] = _json(quest.pop("members_json"), [])
        quest["text"] = quest.pop("description")  # the game's own words, but the key name is reserved for user text
    events = await query.select("idle_guild_events", ("kind", "detail", "cast_by", "stage", "starts_at", "ends_at"),
                                where="guild_id = %s", params=(guild_id,), order_by="starts_at")
    return {"guild_id": guild_id, "characters": chars, "quest": quest, "events": events}


async def idle_character(guild_id: int, user_id: int) -> dict[str, Any]:
    """One idle RPG character in full: clock, items, bag, titles, hunt and gambling tallies."""
    row = await query.select_one("idle_characters", catalog.columns_of("idle_characters"),
                                 where="guild_id = %s AND user_id = %s", params=(guild_id, user_id))
    if row is None:
        return {"guild_id": guild_id, "user_id": user_id, "character": None}
    for key in ("items_json", "loot_json", "titles_json"):
        row[key[:-5]] = _json(row.pop(key), None)
    return {"guild_id": guild_id, "user_id": user_id, "character": row}


async def chess_stats(user_id: "int | None" = None, limit: int = 20) -> dict[str, Any]:
    """Bot-chess stats and recent finished games (result, Elo, PGN). With user_id,
    that player's games; without, the latest across the bot."""
    limit = query.clamp_limit(limit)
    stats = None
    where, params = None, []
    if user_id is not None:
        stats = await query.select_one("chess_user_stats", ("max_elo_defeated", "total_elo_defeated", "elo_spent", "bonus_bins"),
                                       where="user_id = %s", params=(user_id,))
        where, params = "white_id = %s OR black_id = %s", [user_id, user_id]
    games = await query.select("chess_reports", ("report_id", "guild_id", "white_id", "black_id", "winner_id", "result", "elo", "finished_at", "pgn"),
                               where=where, params=params, order_by="finished_at DESC", limit=limit)
    live = await query.select("chess_games", ("channel_id", "white_id", "black_id", "current_id", "amount", "elo", "started_at"))
    return {"user_id": user_id, "stats": stats, "recent_games": games, "live_games": live}


async def minecraft_status(days: int = 7) -> dict[str, Any]:
    """The Minecraft monitor: latest sample from the bot, daily player and ping
    rollups for the last `days`, and the server version history."""
    if not isinstance(days, int) or days < 1 or days > 90:
        raise ToolInputError("days must be 1–90")
    overview = await state_client.view("overview")
    players = await query.select("mc_daily_player_stats", ("stat_date", "max_concurrent", "total_joins", "player_seconds"), order_by="stat_date DESC", limit=days)
    pings = await query.select("mc_daily_ping_stats", ("stat_date", "avg_ping", "min_ping", "max_ping"), order_by="stat_date DESC", limit=days)
    versions = await query.select("mc_server_versions", ("ts", "version"), order_by="ts DESC", limit=10)
    return {"live": overview.get("minecraft", overview), "daily_players": players, "daily_ping": pings, "versions": versions}


# ── reports ──────────────────────────────────────────────────────────────────

async def issues(status: "str | None" = None, kind: "str | None" = None, limit: int = 50) -> dict[str, Any]:
    """Bug reports, tasks and auto-filed errors by kind and status, newest first —
    ids, reporter and timestamps only; the report text stays in Discord."""
    status, kind = _word(status, "status"), _word(kind, "kind")
    limit = query.clamp_limit(limit)
    where, params = ["deleted = 0"], []
    if status:
        where.append("status = %s")
        params.append(status)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    rows = await query.select("issues", ("id", "kind", "status", "guild_id", "reporter_id", "created_at", "resolved_by", "resolved_at"),
                              where=" AND ".join(where), params=params, order_by="created_at DESC", limit=limit)
    return _truncated(rows, limit)


async def feature_requests(status: "str | None" = None, limit: int = 50) -> dict[str, Any]:
    """Feature requests by status, newest first — ids, reporter and timestamps
    only; the request text stays in Discord."""
    status = _word(status, "status")
    limit = query.clamp_limit(limit)
    where, params = None, []
    if status:
        where, params = "status = %s", [status]
    rows = await query.select("feature_requests", ("id", "status", "guild_id", "reporter_id", "feature_issue_id", "created_at", "resolved_by", "resolved_at"),
                              where=where, params=params, order_by="created_at DESC", limit=limit)
    return _truncated(rows, limit)


TOOLS = (
    bot_status, live_activity, guilds, economy_summary, user_economy, leaderboard, records, history,
    guild_settings, active_effects, counters, command_permissions, migrations,
    idle_overview, idle_character, chess_stats, minecraft_status, issues, feature_requests,
)


def _audited(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        t0 = time.monotonic()
        outcome = "ok"
        try:
            result = await fn(*args, **kwargs)
            return redact.scrub(result)
        except ToolInputError as e:
            outcome = f"bad_input:{e}"
            raise
        except Exception as e:
            outcome = f"error:{type(e).__name__}"
            raise
        finally:
            log.info("tool=%s args=%s outcome=%s ms=%d", fn.__name__, json.dumps(kwargs, default=str),
                     outcome, int((time.monotonic() - t0) * 1000))
    return wrapper


def register(mcp) -> None:
    for fn in TOOLS:
        mcp.tool(name=fn.__name__, description=inspect.getdoc(fn), annotations=READ_ONLY)(_audited(fn))

    @mcp.resource("schema://tables", name="tables", mime_type="application/json",
                  description="Every table the sidecar can read, with its readable columns, and the ones it can't, with why.")
    def tables_resource() -> dict[str, Any]:
        return {
            "readable": {name: list(t.columns) for name, t in catalog.readable().items()},
            "hidden_columns": {name: t.hidden for name, t in catalog.readable().items() if t.hidden},
            "never": {name: t.never for name, t in catalog.TABLES.items() if not t.readable},
        }

    @mcp.resource("schema://tables/{table}", name="table", mime_type="application/json",
                  description="One table's readable columns and hidden ones.")
    def table_resource(table: str) -> dict[str, Any]:
        entry = catalog.TABLES.get(table)
        if entry is None:
            return {"table": table, "known": False}
        return {"table": table, "readable": entry.readable, "columns": list(entry.columns),
                "hidden": entry.hidden, "never": entry.never, "note": entry.note}
