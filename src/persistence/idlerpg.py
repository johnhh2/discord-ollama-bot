"""idle_characters / idle_quests tables — !idle (src/cogs/idle_cog.py).
Mirrors state.idle_characters and state.idle_quests."""
import json

from src import state
from src.db import with_cursor


def parse_items(items_json: "str | None") -> dict:
    try:
        items = json.loads(items_json or "{}")
    except ValueError:
        return {}
    return items if isinstance(items, dict) else {}


def parse_list(items_json: "str | None") -> list:
    try:
        items = json.loads(items_json or "[]")
    except ValueError:
        return []
    return items if isinstance(items, list) else []


def parse_members(members_json: "str | None") -> list:
    try:
        members = json.loads(members_json or "[]")
    except ValueError:
        return []
    return [int(uid) for uid in members] if isinstance(members, list) else []


async def save_idle_character(guild_id: int, user_id: int) -> None:
    """Mirror the whole of state.idle_characters[guild_id][user_id], so a
    call site can't drop a column."""
    c = state.idle_characters[guild_id][user_id]
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO idle_characters (guild_id, user_id, class_name, level, next_level_at, remaining, "
            "law, moral, prestige, penalty_total, last_seen, last_penalty_at, thread_id, created_at, "
            "align_changed_at, duel_day, items_json, x, y, gold, rush_day, extra_duel_day, "
            "auto_trade, traded_at, travel_to, mob_kills, mob_deaths, gamble_town, gamble_visit_at, "
            "gamble_budget, gambles, gamble_won, gamble_lost, claimed, hp, loot_json, titles_json, "
            "title, boost_pct, boost_until, hunt_mob, hunt_count, hunt_killed, hunt_x, hunt_y, "
            "hunt_at, hunts_done, potions) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE class_name=VALUES(class_name), level=VALUES(level), "
            "next_level_at=VALUES(next_level_at), remaining=VALUES(remaining), law=VALUES(law), "
            "moral=VALUES(moral), prestige=VALUES(prestige), penalty_total=VALUES(penalty_total), "
            "last_seen=VALUES(last_seen), last_penalty_at=VALUES(last_penalty_at), "
            "thread_id=VALUES(thread_id), created_at=VALUES(created_at), "
            "align_changed_at=VALUES(align_changed_at), duel_day=VALUES(duel_day), "
            "items_json=VALUES(items_json), x=VALUES(x), y=VALUES(y), gold=VALUES(gold), "
            "rush_day=VALUES(rush_day), extra_duel_day=VALUES(extra_duel_day), "
            "auto_trade=VALUES(auto_trade), traded_at=VALUES(traded_at), travel_to=VALUES(travel_to), "
            "mob_kills=VALUES(mob_kills), mob_deaths=VALUES(mob_deaths), "
            "gamble_town=VALUES(gamble_town), gamble_visit_at=VALUES(gamble_visit_at), "
            "gamble_budget=VALUES(gamble_budget), gambles=VALUES(gambles), "
            "gamble_won=VALUES(gamble_won), gamble_lost=VALUES(gamble_lost), claimed=VALUES(claimed), "
            "hp=VALUES(hp), loot_json=VALUES(loot_json), titles_json=VALUES(titles_json), "
            "title=VALUES(title), boost_pct=VALUES(boost_pct), boost_until=VALUES(boost_until), "
            "hunt_mob=VALUES(hunt_mob), hunt_count=VALUES(hunt_count), hunt_killed=VALUES(hunt_killed), "
            "hunt_x=VALUES(hunt_x), hunt_y=VALUES(hunt_y), hunt_at=VALUES(hunt_at), "
            "hunts_done=VALUES(hunts_done), potions=VALUES(potions)",
            (
                int(guild_id), int(user_id), c["class"], int(c["level"]), c["next_level_at"], c["remaining"],
                c["law"], c["moral"], int(c["prestige"]), int(c["penalty_total"]), int(c["last_seen"]),
                int(c["last_penalty_at"]), c["thread_id"], int(c["created_at"]),
                int(c["align_changed_at"]), c["duel_day"], json.dumps(c["items"]),
                c.get("x"), c.get("y"), int(c["gold"]), c["rush_day"], c["extra_duel_day"],
                int(c["auto_trade"]), int(c["traded_at"]), c["travel_to"],
                int(c["mob_kills"]), int(c["mob_deaths"]),
                c["gamble_town"], int(c["gamble_visit_at"]), int(c["gamble_budget"]),
                int(c["gambles"]), int(c["gamble_won"]), int(c["gamble_lost"]), int(c["claimed"]), int(c["hp"]),
                json.dumps(c.get("loot") or []), json.dumps(c.get("titles") or []), c.get("title"),
                int(c.get("boost_pct", 0)), int(c.get("boost_until", 0)),
                c.get("hunt_mob"), int(c.get("hunt_count", 0)), int(c.get("hunt_killed", 0)),
                c.get("hunt_x"), c.get("hunt_y"), int(c.get("hunt_at", 0)), int(c.get("hunts_done", 0)),
                int(c.get("potions", 0)),
            ),
        )


async def save_idle_guild_events(guild_id: int) -> None:
    """Mirror the whole of state.idle_guild_events[guild_id]. The list is a
    handful of rows that change a few times a day, so rewriting it entire is
    cheaper than giving every blessing an id to be updated by."""
    rows = state.idle_guild_events.get(guild_id) or []
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM idle_guild_events WHERE guild_id=%s", (int(guild_id),))
        for row in rows:
            await cur.execute(
                "INSERT INTO idle_guild_events (guild_id, kind, detail, cast_by, stage, starts_at, ends_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    int(guild_id), row["kind"], row["detail"],
                    None if row["cast_by"] is None else int(row["cast_by"]),
                    int(row["stage"]), int(row["starts_at"]), int(row["ends_at"]),
                ),
            )


async def delete_idle_character(guild_id: int, user_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM idle_characters WHERE guild_id=%s AND user_id=%s", (int(guild_id), int(user_id)),
        )


async def save_idle_optout(guild_id: int, user_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO idle_optouts (guild_id, user_id) VALUES (%s,%s) "
            "ON DUPLICATE KEY UPDATE user_id=VALUES(user_id)",
            (int(guild_id), int(user_id)),
        )


async def delete_idle_optout(guild_id: int, user_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute(
            "DELETE FROM idle_optouts WHERE guild_id=%s AND user_id=%s", (int(guild_id), int(user_id)),
        )


async def delete_idle_guild(guild_id: int) -> None:
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM idle_characters WHERE guild_id=%s", (int(guild_id),))
        await cur.execute("DELETE FROM idle_quests WHERE guild_id=%s", (int(guild_id),))
        await cur.execute("DELETE FROM idle_guild_events WHERE guild_id=%s", (int(guild_id),))


async def save_idle_quest(guild_id: int) -> None:
    q = state.idle_quests[guild_id]
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO idle_quests (guild_id, members_json, description, ends_at, not_before, "
            "kind, stage, p1x, p1y, p2x, p2y) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE members_json=VALUES(members_json), description=VALUES(description), "
            "ends_at=VALUES(ends_at), not_before=VALUES(not_before), kind=VALUES(kind), stage=VALUES(stage), "
            "p1x=VALUES(p1x), p1y=VALUES(p1y), p2x=VALUES(p2x), p2y=VALUES(p2y)",
            (
                int(guild_id), json.dumps(q["members"]), q["description"], q["ends_at"], int(q["not_before"]),
                q["kind"], int(q["stage"]), *(q["p1"] or (None, None)), *(q["p2"] or (None, None)),
            ),
        )
