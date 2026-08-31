import json

from src import state
from src.db import with_cursor, with_transaction


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
        u.get("jail_reason"),
        bool(u.get("crime_eligible", False)),
        int(u.get("bail_amount", 0) or 0),
        int(u.get("bot_chess_elo_max_today", 0) or 0),
        u.get("bot_chess_elo_max_date"),
        int(u.get("scratch_won_today", 0) or 0),
        float(u.get("property_paid_at", 0.0) or 0.0),
        int(u.get("property_revenue_total", 0) or 0),
        bool(u.get("daily_gamble_property", False)),
        int(u.get("ins_paid_since_claim", 0) or 0),
        int(u.get("ins_lapsed_since_claim", 0) or 0),
    )


_ECONOMY_UPSERT_SQL = """INSERT INTO economy_users
    (user_id, balance, last_daily, daily_date, scratch_used,
     scratch_date, jailbreak_used, jail_until, savings, jail_reason,
     crime_eligible, bail_amount, bot_chess_elo_max_today, bot_chess_elo_max_date,
     scratch_won_today,
     property_paid_at, property_revenue_total, daily_gamble_property,
     ins_paid_since_claim, ins_lapsed_since_claim)
   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
   ON DUPLICATE KEY UPDATE
     balance=VALUES(balance),
     last_daily=VALUES(last_daily),
     daily_date=VALUES(daily_date),
     scratch_used=VALUES(scratch_used),
     scratch_date=VALUES(scratch_date),
     jailbreak_used=VALUES(jailbreak_used),
     jail_until=VALUES(jail_until),
     savings=VALUES(savings),
     jail_reason=VALUES(jail_reason),
     crime_eligible=VALUES(crime_eligible),
     bail_amount=VALUES(bail_amount),
     bot_chess_elo_max_today=VALUES(bot_chess_elo_max_today),
     bot_chess_elo_max_date=VALUES(bot_chess_elo_max_date),
     scratch_won_today=VALUES(scratch_won_today),
     property_paid_at=VALUES(property_paid_at),
     property_revenue_total=VALUES(property_revenue_total),
     daily_gamble_property=VALUES(daily_gamble_property),
     ins_paid_since_claim=VALUES(ins_paid_since_claim),
     ins_lapsed_since_claim=VALUES(ins_lapsed_since_claim)"""


async def save_economy(uid: int = None):
    """Write economy state to DB.

    If `uid` is provided, only that user's row is written (plus economy_meta and
    that user's guild_house if relevant) — safe even if state.economy was never
    fully loaded. If `uid` is None, writes ALL rows in state (legacy bulk save,
    used by do_daily_reset).
    """
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
    bal = state.economy.get("guild_house", {}).get(str(guild_id), 0)
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO guild_house_balance (guild_id, balance) VALUES (%s,%s)"
            " ON DUPLICATE KEY UPDATE balance=VALUES(balance)",
            (int(guild_id), bal),
        )


async def save_insurance_sweep_day():
    """Persist the last_insurance_sweep gameplay-day marker (economy_meta) —
    the once-per-day gate for the 5am insurance-subscription sweep."""
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO economy_meta (key_name, value_text) VALUES ('last_insurance_sweep', %s)"
            " ON DUPLICATE KEY UPDATE value_text=VALUES(value_text)",
            (state.economy.get("last_insurance_sweep"),),
        )


async def save_insurance():
    """Persist insurance into the shop_effects table (effect_type='insurance').
    Insurance is bot-wide since migration 0055 — one policy per user, stored
    under the sentinel guild_id=0. protected_from is stored as history_json and
    the expiry as expires_at. The old standalone shop_insurance table was
    dropped in migration 0032."""
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='insurance'")
        for uid, entry in state.insurance.items():
            await cur.execute(
                "INSERT INTO shop_effects (guild_id, user_id, effect_type, expires_at, history_json)"
                " VALUES (0,%s,'insurance',%s,%s)",
                (int(uid), entry["expires_at"],
                 json.dumps(entry.get("protected_from", []))),
            )


async def save_insurance_subs():
    """Persist insurance subscriptions (shop_effects, effect_type='insurance_sub',
    bot-wide under the sentinel guild_id=0). A subscription row has no expiry —
    it lives until the user unsubscribes."""
    async with with_transaction() as cur:
        await cur.execute("DELETE FROM shop_effects WHERE effect_type='insurance_sub'")
        for uid in state.insurance_subs:
            await cur.execute(
                "INSERT INTO shop_effects (guild_id, user_id, effect_type)"
                " VALUES (0,%s,'insurance_sub')",
                (int(uid),),
            )


async def save_jackpot(value: int):
    state.slot_jackpot = value
    async with with_cursor() as cur:
        await cur.execute(
            "INSERT INTO slots_jackpot (id, jackpot) VALUES (1,%s)"
            " ON DUPLICATE KEY UPDATE jackpot=VALUES(jackpot)",
            (value,),
        )
