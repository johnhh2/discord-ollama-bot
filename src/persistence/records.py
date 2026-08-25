import json

from src.db import with_cursor

# Categories whose ties break on the lower holder user id instead of on
# "whoever got there first". A strict `>` comparison already gives every
# other category first-come-wins: the incumbent keeps the record until
# somebody strictly beats it. For these, an equal value held by a HIGHER
# user id is displaced by the lower one, so the holder is a pure function
# of (value, user id) rather than of arrival order. Both `try_set_record`
# and `load_global_records` consult this so the per-guild and cross-guild
# views agree on who holds a tied record.
UID_TIEBREAK_CATEGORIES = {"command_streak"}

# Categories whose value is a GLOBAL per-user stat — one number that reads the
# same from every server — rather than something that happened in one guild.
# Your artifact count, balance, command streak and best Stockfish Elo are
# properties of *you*; buying an artifact in server A raises the number server
# B sees too, so B's "most artifacts owned" record has to move with it. A 50k
# slots win, by contrast, is an event that happened in A and belongs to A.
#
# For these, `try_set_record` mirrors the write into every other guild the
# holder is active in. Deliberately excluded: the gambling categories, the
# per-guild `crime` score, `chess_pvp_wins` (counted per guild by
# `count_pvp_wins_in_guild`) and `hangman_wins_*` (a per-guild tally kept in
# these very rows) — all four are already scoped correctly.
GLOBAL_STAT_CATEGORIES = {
    "highest_balance",
    "total_artifacts",
    "command_streak",
    "highest_bot_chess_elo_defeated",
}


def _mirror_guilds(holder_id: int, exclude_guild_id: int) -> list[int]:
    """Guilds other than `exclude_guild_id` where `holder_id` is active.

    `state.leveling` is the membership proxy: `_ensure_lvl_record` writes a
    (guild, user) row on the user's first command or message in a guild —
    before any rate-limit early-return — so a row means "this user does things
    here". Preferred over `bot.guilds` because plenty of call sites have no bot
    reference (`add_balance`, for one), and because the backfill migrations
    have only SQL to work with and must agree with this on who counts.

    A user present in a guild but never active there has no leveling row, and
    so no records there to be wrong about either.
    """
    import src.state as state
    uid = str(holder_id)
    return [
        int(gid) for gid, users in state.leveling.items()
        if uid in users and int(gid) != int(exclude_guild_id)
    ]


def _beats(category: str, value: int, holder_id: int, current: dict) -> bool:
    """Does (value, holder_id) take `category` from the `current` holder?"""
    cur_val = current.get("value")
    if cur_val is None:
        return True
    if value != cur_val:
        return value > cur_val
    if category not in UID_TIEBREAK_CATEGORIES:
        return False
    cur_holder = current.get("holder_id")
    return cur_holder is not None and int(holder_id) < int(cur_holder)


async def load_records(guild_id: int) -> dict:
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT category, value, holder_id, holder_name, extra_json FROM records WHERE guild_id=%s",
            (guild_id,),
        )
        rows = await cur.fetchall()
    result = {}
    for cat, val, holder_id, holder_name, extra_json in rows:
        entry = {"value": val, "holder_id": holder_id, "holder_name": holder_name}
        if extra_json:
            entry.update(json.loads(extra_json))
        result[cat] = entry
    return result


async def load_global_records() -> dict:
    """Top record per category across every guild."""
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT category, value, holder_id, holder_name, extra_json FROM records",
        )
        rows = await cur.fetchall()
    result = {}
    for cat, val, holder_id, holder_name, extra_json in rows:
        existing = result.get(cat)
        if existing is not None and not _beats(cat, val, holder_id, existing):
            continue
        entry = {"value": val, "holder_id": holder_id, "holder_name": holder_name}
        if extra_json:
            entry.update(json.loads(extra_json))
        result[cat] = entry
    return result


async def save_records(guild_id: int, records: dict):
    async with with_cursor() as cur:
        for cat, data in records.items():
            known_keys = {"value", "holder_id", "holder_name"}
            extra = {k: v for k, v in data.items() if k not in known_keys}
            await cur.execute(
                "INSERT INTO records (guild_id, category, value, holder_id, holder_name, extra_json)"
                " VALUES (%s,%s,%s,%s,%s,%s)"
                " ON DUPLICATE KEY UPDATE value=VALUES(value), holder_id=VALUES(holder_id),"
                " holder_name=VALUES(holder_name), extra_json=VALUES(extra_json)",
                (guild_id, cat, data["value"], data["holder_id"], data["holder_name"],
                 json.dumps(extra) if extra else None),
            )


async def _set_one(guild_id: int, category: str, value: int, holder_id: int, holder_name: str, **meta) -> bool:
    """Take `category` in a single guild if (value, holder_id) beats the row."""
    records = await load_records(guild_id)
    if not _beats(category, value, holder_id, records.get(category, {})):
        return False
    # Write back only the row that changed. Re-upserting the guild's other
    # categories with their unchanged values reaches the same end state, but
    # the mirror below multiplies every write by the holder's guild count.
    await save_records(guild_id, {
        category: {"value": value, "holder_id": holder_id, "holder_name": holder_name, **meta},
    })
    return True


async def try_set_record(guild_id: int, category: str, value: int, holder_id: int, holder_name: str, **meta) -> bool:
    """Offer (value, holder) to `category`'s record in `guild_id`.

    Returns whether *this* guild's record changed. For GLOBAL_STAT_CATEGORIES
    the same offer is mirrored into every other guild the holder is active in,
    which is what keeps a global stat's record consistent across servers — but
    those mirrors never affect the return value, so the caller's announcement
    fires only where the action happened rather than in every server at once.
    """
    if guild_id is None:
        return False
    took = await _set_one(guild_id, category, value, holder_id, holder_name, **meta)
    if category in GLOBAL_STAT_CATEGORIES:
        for other_gid in _mirror_guilds(holder_id, guild_id):
            await _set_one(other_gid, category, value, holder_id, holder_name, **meta)
    return took


async def is_global_top(category: str, value: int, source_guild_id: int) -> bool:
    """Return True iff *value* strictly beats the best value held by any guild
    other than *source_guild_id* for *category*. Used to decide whether a new
    per-guild record also constitutes a new global record.

    Deliberately strict even for UID_TIEBREAK_CATEGORIES: tying another
    guild's value shouldn't fan a "New Global Record!" embed out to every
    configured records channel, even when the uid tiebreak means this guild
    would win the display in `load_global_records`.

    Call this AFTER try_set_record has persisted the new value — the source
    guild is excluded so its own freshly-written row doesn't shadow the
    comparison.
    """
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT MAX(value) FROM records WHERE category=%s AND guild_id<>%s",
            (category, source_guild_id),
        )
        row = await cur.fetchone()
    other_max = row[0] if row and row[0] is not None else -1
    return value > other_max
