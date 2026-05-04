import json
import os

from src.config import (
    COMMAND_PERMS_FILE, SLOT_JACKPOT_SEED, INITIAL_BOT_ADMIN_IDS,
)
# get_pool re-exported for tests that read the pool directly
# (e.g. tests/test_slots_flow.py:_read_jackpot).
from src.db import get_pool, with_cursor  # noqa: F401


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(filepath, default):
    """Read a JSON file; return default on missing/corrupt. Kept for migration use."""
    os.makedirs("data", exist_ok=True)
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ── Economy ───────────────────────────────────────────────────────────────────

def _economy_user_row(uid_str: str, u: dict) -> tuple:
    return (
        int(uid_str),
        u.get("balance", 0),
        u.get("last_daily", 0.0),
        u.get("daily_date"),
        u.get("scratch_used", 0),
        u.get("scratch_date"),
        bool(u.get("jailbreak_used", False)),
        u.get("jail_until", 0.0),
        json.dumps(u.get("savings", [])),
    )


_ECONOMY_UPSERT_SQL = """INSERT INTO economy_users
    (user_id, balance, last_daily, daily_date, scratch_used,
     scratch_date, jailbreak_used, jail_until, savings)
   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
   ON DUPLICATE KEY UPDATE
     balance=VALUES(balance),
     last_daily=VALUES(last_daily),
     daily_date=VALUES(daily_date),
     scratch_used=VALUES(scratch_used),
     scratch_date=VALUES(scratch_date),
     jailbreak_used=VALUES(jailbreak_used),
     jail_until=VALUES(jail_until),
     savings=VALUES(savings)"""


async def save_economy(uid: int = None):
    """Write economy state to DB.

    If `uid` is provided, only that user's row is written (plus economy_meta and
    that user's guild_house if relevant) — safe even if state.economy was never
    fully loaded. If `uid` is None, writes ALL rows in state (legacy bulk save,
    used by do_daily_reset).
    """
    from src import state
    async with with_cursor() as cur:
        if uid is not None:
            u = state.economy["users"].get(str(uid))
            if u is not None:
                await cur.execute(_ECONOMY_UPSERT_SQL, _economy_user_row(str(uid), u))
        else:
            for uid_str, u in state.economy["users"].items():
                await cur.execute(_ECONOMY_UPSERT_SQL, _economy_user_row(uid_str, u))

        # last_daily_reset (cheap, always write)
        await cur.execute(
            "INSERT INTO economy_meta (key_name, value_text) VALUES ('last_daily_reset', %s)"
            " ON DUPLICATE KEY UPDATE value_text=VALUES(value_text)",
            (state.economy.get("last_daily_reset"),),
        )
        # guild_house: in bulk mode, write all; in targeted mode, leave alone
        if uid is None:
            for gid_str, bal in state.economy.get("guild_house", {}).items():
                await cur.execute(
                    "INSERT INTO guild_house_balance (guild_id, balance) VALUES (%s,%s)"
                    " ON DUPLICATE KEY UPDATE balance=VALUES(balance)",
                    (int(gid_str), bal),
                )


async def save_guild_house(guild_id: int):
    """Write a single guild's house balance."""
    from src import state
    bal = state.economy.get("guild_house", {}).get(str(guild_id), 0)
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO guild_house_balance (guild_id, balance) VALUES (%s,%s)"
            " ON DUPLICATE KEY UPDATE balance=VALUES(balance)",
            (int(guild_id), bal),
        )


async def save_insurance():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_insurance")
        for uid_str, entry in state.insurance.items():
            await cur.execute(
                "INSERT INTO shop_insurance (user_id, expires_at, protected_from) VALUES (%s,%s,%s)",
                (int(uid_str), entry["expires_at"], json.dumps(entry.get("protected_from", []))),
            )


async def save_jackpot(value: int):
    from src import state
    state.slot_jackpot = value
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO slots_jackpot (id, jackpot) VALUES (1,%s)"
            " ON DUPLICATE KEY UPDATE jackpot=VALUES(jackpot)",
            (value,),
        )


# ── Guild settings ────────────────────────────────────────────────────────────

async def save_guild_settings():
    from src import state
    async with with_cursor() as cur:
        for gid_str, settings in state.guild_settings.items():
            await cur.execute(
                "INSERT INTO guild_settings (guild_id, settings_json) VALUES (%s,%s)"
                " ON DUPLICATE KEY UPDATE settings_json=VALUES(settings_json)",
                (int(gid_str), json.dumps(settings)),
            )


# get_guild_cfg moved to src/guild_config.py (it was never a DB function).
# Re-exported here so existing `from src.persistence import get_guild_cfg`
# keeps working until callers migrate.
from src.guild_config import get_guild_cfg  # noqa: E402, F401


# ── Bot roles / godmode / settings ────────────────────────────────────────────

async def save_bot_roles():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM bot_roles")
        for role_id in state.bot_roles:
            await cur.execute("INSERT IGNORE INTO bot_roles (role_id) VALUES (%s)", (role_id,))


async def save_godmode_users():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM godmode_users")
        for uid in state.godmode_users:
            await cur.execute("INSERT IGNORE INTO godmode_users (user_id) VALUES (%s)", (uid,))


async def save_bot_settings():
    from src import state
    async with with_cursor() as cur:
        for k, v in state.bot_settings.items():
            await cur.execute(
                "INSERT INTO bot_settings (key_name, value_text) VALUES (%s,%s)"
                " ON DUPLICATE KEY UPDATE value_text=VALUES(value_text)",
                (k, str(v)),
            )


# ── Shop effects ──────────────────────────────────────────────────────────────

async def save_ragebait():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='ragebait'")
        for uid, data in state.active_ragebaits.items():
            await cur.execute(
                "INSERT INTO shop_effects (user_id, effect_type, remaining, started_by, history_json, channel_id)"
                " VALUES (%s,'ragebait',%s,%s,%s,%s)",
                (int(uid), data.get("remaining"), data.get("started_by"),
                 json.dumps(data.get("history", [])), data.get("channel_id")),
            )


async def save_mock():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='mock'")
        for uid, data in state.active_mocks.items():
            await cur.execute(
                "INSERT INTO shop_effects (user_id, effect_type, remaining, started_by, channel_id)"
                " VALUES (%s,'mock',%s,%s,%s)",
                (int(uid), data.get("remaining"), data.get("started_by"), data.get("channel_id")),
            )


async def save_curse(curse_data: dict):
    from src import state
    state.active_curses = curse_data
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='curse'")
        for uid, data in curse_data.items():
            await cur.execute(
                "INSERT INTO shop_effects (user_id, effect_type, remaining, cursed_by, channel_id)"
                " VALUES (%s,'curse',%s,%s,%s)",
                (int(uid), data.get("remaining"), data.get("cursed_by"), data.get("channel_id")),
            )


async def save_tax(tax_data: dict):
    from src import state
    state.active_taxes = tax_data
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='tax'")
        for uid, data in tax_data.items():
            await cur.execute(
                "INSERT INTO shop_effects (user_id, effect_type, master_id, tax_type, tax_emoji, channel_id, activated_at)"
                " VALUES (%s,'tax',%s,%s,%s,%s,%s)",
                (int(uid), data.get("master"), data.get("type", "tax"),
                 data.get("emoji", "💰"), data.get("channel_id"), data.get("activated_at")),
            )


# ── Rigged ────────────────────────────────────────────────────────────────────

async def save_rigged_slots():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM rigged_slots")
        for uid, symbol in state.rigged_slots.items():
            await cur.execute(
                "INSERT INTO rigged_slots (user_id, symbol) VALUES (%s,%s)",
                (int(uid), symbol),
            )


async def save_rigged_flips():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM rigged_flips")
        for uid, wins in state.rigged_flips.items():
            await cur.execute(
                "INSERT INTO rigged_flips (user_id, remaining_wins) VALUES (%s,%s)",
                (int(uid), wins),
            )


async def save_rigged_scratch():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM rigged_scratch")
        for uid, count in state.rigged_scratch.items():
            await cur.execute(
                "INSERT INTO rigged_scratch (user_id, symbols_count) VALUES (%s,%s)",
                (int(uid), count),
            )


async def save_rigged_steal():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM rigged_steal")
        for uid, remaining in state.rigged_steal.items():
            await cur.execute(
                "INSERT INTO rigged_steal (user_id, remaining_successes) VALUES (%s,%s)",
                (int(uid), remaining),
            )


# ── Gambler streak ────────────────────────────────────────────────────────────

async def save_gambler_streak():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM gambler_streak")
        for uid_str, entry in state.gambler_streak.items():
            if isinstance(entry, dict):
                date_str = entry.get("date", "")
                count = int(entry.get("count", 1))
            else:
                date_str = entry
                count = 1
            if not date_str:
                continue
            await cur.execute(
                "INSERT INTO gambler_streak (user_id, last_full_date, streak_count) VALUES (%s,%s,%s)",
                (int(uid_str), date_str, count),
            )


# ── Chess ─────────────────────────────────────────────────────────────────────

async def save_chess_games():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM chess_games")
        for ch_id, game in state.active_chess_games.items():
            await cur.execute(
                "INSERT INTO chess_games (channel_id, game_json) VALUES (%s,%s)",
                (int(ch_id), json.dumps(game)),
            )


# ── AI threads (ask, story, roleplay, rpg) ───────────────────────────────────

async def save_ai_threads():
    from src import state
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM ai_threads")
        for tid, t in state.ai_threads.items():
            await cur.execute(
                "INSERT INTO ai_threads "
                "(thread_id, kind, owner_id, guild_id, invited_ids_json, system_prompt, character_prompt, history_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    int(tid),
                    t["kind"],
                    int(t["owner_id"]),
                    int(t["guild_id"]) if t.get("guild_id") is not None else None,
                    json.dumps(list(t.get("invited_ids", set()))),
                    t.get("system_prompt"),
                    t.get("character_prompt"),
                    json.dumps(list(t.get("history", []))),
                ),
            )


# ── Quotes ────────────────────────────────────────────────────────────────────

async def save_quote_log(log: list):
    from src import state
    trimmed = log[-10:]
    state.quote_log = trimmed
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM quote_log")
        for entry in trimmed:
            await cur.execute(
                "INSERT INTO quote_log (content) VALUES (%s)", (entry,)
            )


async def save_saved_quotes(quotes: dict):
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM saved_quotes")
        for guild_id_str, guild_quotes in quotes.items():
            for q in guild_quotes:
                await cur.execute(
                    "INSERT INTO saved_quotes (guild_id, quote_json) VALUES (%s,%s)",
                    (str(guild_id_str), json.dumps(q)),
                )


async def load_saved_quotes() -> dict:
    async with with_cursor() as cur:
        await cur.execute("SELECT guild_id, quote_json FROM saved_quotes")
        rows = await cur.fetchall()
    result = {}
    for guild_id_str, quote_json in rows:
        q = json.loads(quote_json)
        result.setdefault(guild_id_str, []).append(q)
    return result


# ── Leveling ──────────────────────────────────────────────────────────────────

async def save_leveling(guild_id: int = None, uid: int = None):
    """Write leveling rows. state.leveling = {guild_id_str: {uid_str: {...}}}.

    If both guild_id and uid are passed, only that one row is written — safe
    even if state.leveling was never fully loaded. If both are None, writes
    every row in state (bulk; should rarely be needed now).
    """
    from src import state
    async with with_cursor() as cur:
        if guild_id is not None and uid is not None:
            rec = state.leveling.get(str(guild_id), {}).get(str(uid))
            if rec is not None:
                await cur.execute(
                    "INSERT INTO leveling (guild_id, user_id, data) VALUES (%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE data=VALUES(data)",
                    (int(guild_id), int(uid), json.dumps(rec)),
                )
            return
        for gid_str, users in state.leveling.items():
            for uid_str, rec in users.items():
                await cur.execute(
                    "INSERT INTO leveling (guild_id, user_id, data) VALUES (%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE data=VALUES(data)",
                    (int(gid_str), int(uid_str), json.dumps(rec)),
                )


# ── Lottery ───────────────────────────────────────────────────────────────────

async def load_lottery(guild_id: int) -> dict:
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT prize_pool, last_posted_week, last_drawn_week FROM lottery WHERE guild_id=%s",
            (guild_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return {"prize_pool": 0, "players": {}, "last_posted_week": 0, "last_drawn_week": 0}
        prize_pool, last_posted_week, last_drawn_week = row
        await cur.execute(
            "SELECT user_id, tickets FROM lottery_players WHERE guild_id=%s", (guild_id,)
        )
        players = {str(r[0]): r[1] for r in await cur.fetchall()}
    return {
        "prize_pool": prize_pool,
        "players": players,
        "last_posted_week": last_posted_week,
        "last_drawn_week": last_drawn_week,
    }


async def save_lottery(guild_id: int, lottery_data: dict):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO lottery (guild_id, prize_pool, last_posted_week, last_drawn_week)"
            " VALUES (%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE prize_pool=VALUES(prize_pool),"
            " last_posted_week=VALUES(last_posted_week),"
            " last_drawn_week=VALUES(last_drawn_week)",
            (
                guild_id,
                lottery_data.get("prize_pool", 0),
                lottery_data.get("last_posted_week", 0),
                lottery_data.get("last_drawn_week", 0),
            ),
        )
        await cur.execute("DELETE FROM lottery_players WHERE guild_id=%s", (guild_id,))
        for uid_str, tickets in lottery_data.get("players", {}).items():
            await cur.execute(
                "INSERT INTO lottery_players (guild_id, user_id, tickets) VALUES (%s,%s,%s)",
                (guild_id, int(uid_str), tickets),
            )


# ── Records ───────────────────────────────────────────────────────────────────

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


async def try_set_record(guild_id: int, category: str, value: int, holder_id: int, holder_name: str, **meta) -> bool:
    if guild_id is None:
        return False
    records = await load_records(guild_id)
    current = records.get(category, {})
    if value > current.get("value", -1):
        records[category] = {"value": value, "holder_id": holder_id, "holder_name": holder_name, **meta}
        await save_records(guild_id, records)
        return True
    return False


# ── Balance / bot stats history ───────────────────────────────────────────────

async def load_balance_history() -> dict:
    async with with_cursor() as cur:
        await cur.execute("SELECT snapshot_date, user_id, wallet, savings FROM balance_history")
        rows = await cur.fetchall()
    result = {}
    for date_str, uid, wallet, savings in rows:
        result.setdefault(date_str, {})[str(uid)] = {"wallet": wallet, "savings": savings}
    return result


async def save_balance_history(history: dict):
    async with with_cursor() as cur:
        for date_str, users in history.items():
            for uid_str, vals in users.items():
                await cur.execute(
                    "INSERT INTO balance_history (snapshot_date, user_id, wallet, savings)"
                    " VALUES (%s,%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE wallet=VALUES(wallet), savings=VALUES(savings)",
                    (date_str, int(uid_str), vals.get("wallet", 0), vals.get("savings", 0)),
                )


async def load_bot_stats_history() -> dict:
    async with with_cursor() as cur:
        await cur.execute(
            "SELECT snapshot_date, messages, commands, ai_responses, ai_up, memory_mb FROM bot_stats_history"
        )
        rows = await cur.fetchall()
    return {
        r[0]: {"messages": r[1], "commands": r[2], "ai_responses": r[3], "ai_up": bool(r[4]), "memory_mb": r[5]}
        for r in rows
    }


async def save_bot_stats_history(history: dict):
    async with with_cursor() as cur:
        for date_str, vals in history.items():
            await cur.execute(
                "INSERT INTO bot_stats_history"
                " (snapshot_date, messages, commands, ai_responses, ai_up, memory_mb)"
                " VALUES (%s,%s,%s,%s,%s,%s)"
                " ON DUPLICATE KEY UPDATE messages=VALUES(messages), commands=VALUES(commands),"
                " ai_responses=VALUES(ai_responses), ai_up=VALUES(ai_up), memory_mb=VALUES(memory_mb)",
                (date_str, vals.get("messages", 0), vals.get("commands", 0),
                 vals.get("ai_responses", 0), vals.get("ai_up", False), vals.get("memory_mb", 0.0)),
            )


# ── Channel prompts ───────────────────────────────────────────────────────────

async def save_channel_prompts(prompts: dict):
    from src import state
    state.channel_prompts = prompts
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM channel_prompts")
        for ch_id, prompt in prompts.items():
            await cur.execute(
                "INSERT INTO channel_prompts (channel_id, prompt_text) VALUES (%s,%s)",
                (int(ch_id), prompt),
            )


# ── Command perms ─────────────────────────────────────────────────────────────

async def save_command_perms():
    from src import state
    async with with_cursor() as cur:
        for cmd, data in state.command_perms.items():
            await cur.execute(
                "INSERT INTO command_perms (command_name, tier, hidden) VALUES (%s,%s,%s)"
                " ON DUPLICATE KEY UPDATE tier=VALUES(tier), hidden=VALUES(hidden)",
                (cmd, data["tier"], bool(data.get("hidden", False))),
            )


# ── Restart msg / ephemeral msgs ──────────────────────────────────────────────

async def save_restart_msg(channel_id: int, message_id: int):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO restart_msg (id, channel_id, message_id) VALUES (1,%s,%s)"
            " ON DUPLICATE KEY UPDATE channel_id=VALUES(channel_id), message_id=VALUES(message_id)",
            (channel_id, message_id),
        )


async def load_restart_msg() -> dict:
    async with with_cursor() as cur:
        await cur.execute("SELECT channel_id, message_id FROM restart_msg WHERE id=1")
        row = await cur.fetchone()
    if row:
        return {"channel_id": row[0], "message_id": row[1]}
    return {}


async def clear_restart_msg():
    async with with_cursor() as cur:
        await cur.execute("DELETE FROM restart_msg WHERE id=1")


async def add_ephemeral_msg(channel_id: int, message_id: int):
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO ephemeral_msgs (channel_id, message_id) VALUES (%s,%s)",
            (channel_id, message_id),
        )


async def load_and_clear_ephemeral_msgs() -> list:
    async with with_cursor() as cur:
        await cur.execute("SELECT channel_id, message_id FROM ephemeral_msgs")
        rows = await cur.fetchall()
        await cur.execute("DELETE FROM ephemeral_msgs")
    return [{"channel_id": r[0], "message_id": r[1]} for r in rows]


# ── init_db_state ─────────────────────────────────────────────────────────────

_init_db_state_done = False


async def init_db_state():
    """Load all persistent state from DB into src.state.

    on_ready fires on every gateway reconnect, so the second+ call is guarded
    out — otherwise a reconnect would clobber any in-memory mutations made
    since the last save (e.g. an in-progress chess game, an active ragebait).
    """
    global _init_db_state_done
    import logging
    import src.state as state

    if _init_db_state_done:
        logging.info("[init_db_state] skipping; already initialized")
        return
    _init_db_state_done = True

    async with with_cursor() as cur:

        def _safe(name):
            """Decorator that runs the load step and logs+swallows any DB error,
            so one broken section can't wipe out everything that comes after."""
            def wrapper(fn):
                return fn
            return wrapper

        # ── economy_users ─────────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT user_id, balance, last_daily, daily_date, scratch_used,"
                " scratch_date, jailbreak_used, jail_until, savings FROM economy_users"
            )
            for row in await cur.fetchall():
                uid, bal, last_daily, daily_date, scratch_used, scratch_date, jb_used, jail_until, savings_json = row
                state.economy["users"][str(uid)] = {
                    "balance": bal,
                    "last_daily": last_daily,
                    "daily_date": daily_date,
                    "scratch_used": scratch_used,
                    "scratch_date": scratch_date,
                    "jailbreak_used": bool(jb_used),
                    "jail_until": jail_until,
                    "savings": json.loads(savings_json) if savings_json else [],
                }
        except Exception as e:
            logging.error(f"[init_db_state] economy_users failed: {e}", exc_info=True)

        # ── economy_meta ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT value_text FROM economy_meta WHERE key_name='last_daily_reset'")
            row = await cur.fetchone()
            state.economy["last_daily_reset"] = row[0] if row and row[0] is not None else None
        except Exception as e:
            logging.error(f"[init_db_state] economy_meta failed: {e}", exc_info=True)

        # ── guild_house_balance ───────────────────────────────────────────
        try:
            await cur.execute("SELECT guild_id, balance FROM guild_house_balance")
            for guild_id, bal in await cur.fetchall():
                state.economy["guild_house"][str(guild_id)] = bal
        except Exception as e:
            logging.error(f"[init_db_state] guild_house_balance failed: {e}", exc_info=True)

        # ── guild_settings ────────────────────────────────────────────────
        try:
            await cur.execute("SELECT guild_id, settings_json FROM guild_settings")
            for guild_id, settings_json in await cur.fetchall():
                settings = json.loads(settings_json)
                state.guild_settings[str(guild_id)] = settings
                for k, v in settings.get("locked_channels", {}).items():
                    state.locked_channels[int(k)] = int(v)
                for k, v in settings.get("locked_roles", {}).items():
                    state.locked_roles[int(k)] = int(v)
        except Exception as e:
            logging.error(f"[init_db_state] guild_settings failed: {e}", exc_info=True)

        # ── slots_jackpot ─────────────────────────────────────────────────
        try:
            await cur.execute("SELECT jackpot FROM slots_jackpot WHERE id=1")
            row = await cur.fetchone()
            state.slot_jackpot = max(SLOT_JACKPOT_SEED, row[0] if row else SLOT_JACKPOT_SEED)
        except Exception as e:
            logging.error(f"[init_db_state] slots_jackpot failed: {e}", exc_info=True)

        # ── bot_roles ─────────────────────────────────────────────────────
        try:
            await cur.execute("SELECT role_id FROM bot_roles")
            state.bot_roles = {r[0] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] bot_roles failed: {e}", exc_info=True)

        # ── godmode_users ─────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id FROM godmode_users")
            state.godmode_users = {r[0] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] godmode_users failed: {e}", exc_info=True)

        # ── bot_settings ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT key_name, value_text FROM bot_settings")
            for k, v in await cur.fetchall():
                state.bot_settings[k] = v
        except Exception as e:
            logging.error(f"[init_db_state] bot_settings failed: {e}", exc_info=True)

        # ── shop_insurance ────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, expires_at, protected_from FROM shop_insurance")
            import time as _time
            now = _time.time()
            for uid, expires_at, protected_json in await cur.fetchall():
                if expires_at > now:
                    state.insurance[str(uid)] = {
                        "expires_at": expires_at,
                        "protected_from": json.loads(protected_json) if protected_json else [],
                    }
        except Exception as e:
            logging.error(f"[init_db_state] shop_insurance failed: {e}", exc_info=True)

        # ── shop_effects: ragebait ────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT user_id, remaining, started_by, history_json, channel_id"
                " FROM shop_effects WHERE effect_type='ragebait'"
            )
            for uid, remaining, started_by, history_json, channel_id in await cur.fetchall():
                state.active_ragebaits[uid] = {
                    "remaining": remaining,
                    "started_by": started_by,
                    "history": json.loads(history_json) if history_json else [],
                    "channel_id": channel_id,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.ragebait failed: {e}", exc_info=True)

        # ── shop_effects: mock ────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT user_id, remaining, started_by, channel_id"
                " FROM shop_effects WHERE effect_type='mock'"
            )
            for uid, remaining, started_by, channel_id in await cur.fetchall():
                state.active_mocks[uid] = {
                    "remaining": remaining,
                    "started_by": started_by,
                    "channel_id": channel_id,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.mock failed: {e}", exc_info=True)

        # ── shop_effects: curse ───────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT user_id, remaining, cursed_by, channel_id"
                " FROM shop_effects WHERE effect_type='curse'"
            )
            for uid, remaining, cursed_by, channel_id in await cur.fetchall():
                state.active_curses[uid] = {
                    "remaining": remaining,
                    "cursed_by": cursed_by,
                    "channel_id": channel_id,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.curse failed: {e}", exc_info=True)

        # ── shop_effects: tax ─────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT user_id, master_id, tax_type, tax_emoji, channel_id, activated_at"
                " FROM shop_effects WHERE effect_type='tax'"
            )
            for uid, master_id, tax_type, tax_emoji, channel_id, activated_at in await cur.fetchall():
                state.active_taxes[uid] = {
                    "master": master_id,
                    "type": tax_type,
                    "emoji": tax_emoji,
                    "channel_id": channel_id,
                    "activated_at": activated_at,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.tax failed: {e}", exc_info=True)

        # ── rigged_slots ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, symbol FROM rigged_slots")
            state.rigged_slots = {str(r[0]): r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] rigged_slots failed: {e}", exc_info=True)

        # ── rigged_flips ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, remaining_wins FROM rigged_flips")
            state.rigged_flips = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] rigged_flips failed: {e}", exc_info=True)

        # ── rigged_scratch ────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, symbols_count FROM rigged_scratch")
            state.rigged_scratch = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] rigged_scratch failed: {e}", exc_info=True)

        # ── rigged_steal ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, remaining_successes FROM rigged_steal")
            state.rigged_steal = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] rigged_steal failed: {e}", exc_info=True)

        # ── gambler_streak ────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, last_full_date, streak_count FROM gambler_streak")
            state.gambler_streak = {
                str(r[0]): {"date": r[1], "count": int(r[2])} for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] gambler_streak failed: {e}", exc_info=True)

        # ── chess_games ───────────────────────────────────────────────────
        try:
            await cur.execute("SELECT channel_id, game_json FROM chess_games")
            state.active_chess_games = {r[0]: json.loads(r[1]) for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] chess_games failed: {e}", exc_info=True)

        # ── ai_threads (ask, story, roleplay, rpg) ───────────────────────
        try:
            await cur.execute(
                "SELECT thread_id, kind, owner_id, guild_id, invited_ids_json, "
                "system_prompt, character_prompt, history_json FROM ai_threads"
            )
            for (
                tid, kind, owner_id, guild_id,
                invited_json, system_prompt, character_prompt, history_json,
            ) in await cur.fetchall():
                state.ai_threads[tid] = {
                    "kind": kind,
                    "owner_id": owner_id,
                    "guild_id": guild_id,
                    "invited_ids": set(json.loads(invited_json) if invited_json else []),
                    "system_prompt": system_prompt,
                    "character_prompt": character_prompt,
                    "history": json.loads(history_json) if history_json else [],
                }
        except Exception as e:
            logging.error(f"[init_db_state] ai_threads failed: {e}", exc_info=True)

        # ── quote_log ─────────────────────────────────────────────────────
        try:
            await cur.execute("SELECT content FROM quote_log ORDER BY id")
            state.quote_log = [r[0] for r in await cur.fetchall()]
        except Exception as e:
            logging.error(f"[init_db_state] quote_log failed: {e}", exc_info=True)

        # ── leveling — nested by guild_id ─────────────────────────────────
        try:
            await cur.execute("SELECT guild_id, user_id, data FROM leveling")
            for guild_id, uid, data_json in await cur.fetchall():
                rec = json.loads(data_json) if data_json else {}
                state.leveling.setdefault(str(guild_id), {})[str(uid)] = rec
        except Exception as e:
            logging.error(f"[init_db_state] leveling failed: {e}", exc_info=True)

        # ── channel_prompts ───────────────────────────────────────────────
        try:
            await cur.execute("SELECT channel_id, prompt_text FROM channel_prompts")
            state.channel_prompts = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] channel_prompts failed: {e}", exc_info=True)

        # ── command_perms ─────────────────────────────────────────────────
        try:
            json_perms = _load_json(COMMAND_PERMS_FILE, {})
            for cmd, data in json_perms.items():
                await cur.execute(
                    "INSERT IGNORE INTO command_perms (command_name, tier, hidden) VALUES (%s,%s,%s)",
                    (cmd, data.get("tier", "everyone"), bool(data.get("hidden", False))),
                )
            await cur.execute("SELECT command_name, tier, hidden FROM command_perms")
            state.command_perms = {
                r[0]: {"tier": r[1], "hidden": bool(r[2])}
                for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] command_perms failed: {e}", exc_info=True)

    # bot_admins: always from env, never DB
    state.bot_admins = set(INITIAL_BOT_ADMIN_IDS)
