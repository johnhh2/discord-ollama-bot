import glob
import json
import os

from discord.ext import commands

from src.helpers import emb, C_GREEN, C_RED
from src.permissions import check_command_permission
from src.persistence import get_guild_cfg
from src.db import get_pool


def _load_json(filepath, default):
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


class MigrateCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="migratedata")
    async def cmd_migratedata(self, ctx: commands.Context):
        if not await check_command_permission(ctx):
            return

        pool = await get_pool()
        counts = {}

        async with pool.acquire() as conn:
            async with conn.cursor() as cur:

                # economy
                economy = _load_json("data/economy.json", {"users": {}, "last_daily_reset": None, "guild_house": {}})
                n = 0
                for uid_str, u in economy.get("users", {}).items():
                    savings = json.dumps(u.get("savings", []))
                    await cur.execute(
                        """INSERT INTO economy_users
                            (user_id, balance, last_daily, daily_date, scratch_used,
                             scratch_date, jailbreak_used, jail_until, savings)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON DUPLICATE KEY UPDATE
                             balance=VALUES(balance), last_daily=VALUES(last_daily),
                             daily_date=VALUES(daily_date), scratch_used=VALUES(scratch_used),
                             scratch_date=VALUES(scratch_date), jailbreak_used=VALUES(jailbreak_used),
                             jail_until=VALUES(jail_until), savings=VALUES(savings)""",
                        (
                            int(uid_str),
                            u.get("balance", 0),
                            u.get("last_daily", 0.0),
                            u.get("daily_date"),
                            u.get("scratch_used", 0),
                            u.get("scratch_date"),
                            bool(u.get("jailbreak_used", False)),
                            u.get("jail_until", 0.0),
                            savings,
                        ),
                    )
                    n += 1
                counts["economy_users"] = n
                await cur.execute(
                    "INSERT INTO economy_meta (key_name, value_text) VALUES ('last_daily_reset', %s)"
                    " ON DUPLICATE KEY UPDATE value_text=VALUES(value_text)",
                    (economy.get("last_daily_reset"),),
                )
                n = 0
                for gid_str, bal in economy.get("guild_house", {}).items():
                    await cur.execute(
                        "INSERT INTO guild_house_balance (guild_id, balance) VALUES (%s,%s)"
                        " ON DUPLICATE KEY UPDATE balance=VALUES(balance)",
                        (int(gid_str), bal),
                    )
                    n += 1
                counts["guild_house"] = n

                # guild_settings
                guild_settings = _load_json("data/guild_settings.json", {})
                n = 0
                for gid_str, settings in guild_settings.items():
                    await cur.execute(
                        "INSERT INTO guild_settings (guild_id, settings_json) VALUES (%s,%s)"
                        " ON DUPLICATE KEY UPDATE settings_json=VALUES(settings_json)",
                        (int(gid_str), json.dumps(settings)),
                    )
                    n += 1
                counts["guild_settings"] = n

                # leveling — JSON shape: {guild_id_str: {uid_str: {...}}}
                leveling = _load_json("data/leveling.json", {})
                n = 0
                for gid_str, users in leveling.items():
                    if not isinstance(users, dict):
                        continue
                    for uid_str, rec in users.items():
                        await cur.execute(
                            "INSERT INTO leveling (guild_id, user_id, data) VALUES (%s,%s,%s)"
                            " ON DUPLICATE KEY UPDATE data=VALUES(data)",
                            (int(gid_str), int(uid_str), json.dumps(rec)),
                        )
                        n += 1
                counts["leveling"] = n

                # records (per-guild files)
                n = 0
                for filepath in glob.glob("data/records_*.json"):
                    guild_id = int(os.path.basename(filepath).replace("records_", "").replace(".json", ""))
                    records = _load_json(filepath, {})
                    for cat, data in records.items():
                        known = {"value", "holder_id", "holder_name"}
                        extra = {k: v for k, v in data.items() if k not in known}
                        await cur.execute(
                            "INSERT INTO records (guild_id, category, value, holder_id, holder_name, extra_json)"
                            " VALUES (%s,%s,%s,%s,%s,%s)"
                            " ON DUPLICATE KEY UPDATE value=VALUES(value), holder_id=VALUES(holder_id),"
                            " holder_name=VALUES(holder_name), extra_json=VALUES(extra_json)",
                            (guild_id, cat, data["value"], data["holder_id"], data["holder_name"],
                             json.dumps(extra) if extra else None),
                        )
                        n += 1
                counts["records"] = n

                # lottery (per-guild files)
                n = 0
                for filepath in glob.glob("data/lottery_*.json"):
                    guild_id = int(os.path.basename(filepath).replace("lottery_", "").replace(".json", ""))
                    lottery = _load_json(filepath, {"prize_pool": 0, "players": {}, "last_posted_week": 0})
                    await cur.execute(
                        "INSERT INTO lottery (guild_id, prize_pool, last_posted_week) VALUES (%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE prize_pool=VALUES(prize_pool), last_posted_week=VALUES(last_posted_week)",
                        (guild_id, lottery.get("prize_pool", 0), lottery.get("last_posted_week", 0)),
                    )
                    await cur.execute("DELETE FROM lottery_players WHERE guild_id=%s", (guild_id,))
                    for uid_str, tickets in lottery.get("players", {}).items():
                        await cur.execute(
                            "INSERT INTO lottery_players (guild_id, user_id, tickets) VALUES (%s,%s,%s)",
                            (guild_id, int(uid_str), tickets),
                        )
                    n += 1
                counts["lottery_guilds"] = n

                # saved quotes
                saved_quotes = _load_json("data/saved_quotes.json", {})
                n = 0
                await cur.execute("DELETE FROM saved_quotes")
                for gid_str, quotes in saved_quotes.items():
                    for q in quotes:
                        await cur.execute(
                            "INSERT INTO saved_quotes (guild_id, quote_json) VALUES (%s,%s)",
                            (str(gid_str), json.dumps(q)),
                        )
                        n += 1
                counts["saved_quotes"] = n

                # channel_prompts
                raw = _load_json("data/channel_prompts.json", {})
                n = 0
                for ch_id_str, prompt in raw.items():
                    await cur.execute(
                        "INSERT INTO channel_prompts (channel_id, prompt_text) VALUES (%s,%s)"
                        " ON DUPLICATE KEY UPDATE prompt_text=VALUES(prompt_text)",
                        (int(ch_id_str), prompt),
                    )
                    n += 1
                counts["channel_prompts"] = n

                # slots_jackpot
                jp = _load_json("data/slots_jackpot.json", {})
                if "jackpot" in jp:
                    await cur.execute(
                        "INSERT INTO slots_jackpot (id, jackpot) VALUES (1,%s)"
                        " ON DUPLICATE KEY UPDATE jackpot=VALUES(jackpot)",
                        (jp["jackpot"],),
                    )
                    counts["slots_jackpot"] = 1

                # bot_roles
                bot_roles = _load_json("data/bot_roles.json", [])
                n = 0
                for role_id in bot_roles:
                    await cur.execute("INSERT IGNORE INTO bot_roles (role_id) VALUES (%s)", (role_id,))
                    n += 1
                counts["bot_roles"] = n

                # godmode_users
                godmode = _load_json("data/godmode_users.json", [])
                n = 0
                for uid in godmode:
                    await cur.execute("INSERT IGNORE INTO godmode_users (user_id) VALUES (%s)", (uid,))
                    n += 1
                counts["godmode_users"] = n

                # bot_settings
                bot_settings = _load_json("data/bot_settings.json", {})
                n = 0
                for k, v in bot_settings.items():
                    await cur.execute(
                        "INSERT INTO bot_settings (key_name, value_text) VALUES (%s,%s)"
                        " ON DUPLICATE KEY UPDATE value_text=VALUES(value_text)",
                        (k, str(v)),
                    )
                    n += 1
                counts["bot_settings"] = n

                # chess_games
                chess = _load_json("data/chess_games.json", {})
                n = 0
                for ch_id_str, game in chess.items():
                    await cur.execute(
                        "INSERT INTO chess_games (channel_id, game_json) VALUES (%s,%s)"
                        " ON DUPLICATE KEY UPDATE game_json=VALUES(game_json)",
                        (int(ch_id_str), json.dumps(game)),
                    )
                    n += 1
                counts["chess_games"] = n

                # roleplay_state
                rp_raw = _load_json("data/roleplay_state.json", {"roleplays": {}, "histories": {}})
                n = 0
                for uid_str, rp in rp_raw.get("roleplays", {}).items():
                    ch_id = rp.get("channel_id", int(uid_str))
                    rp_s = {**rp, "participants": list(rp.get("participants", []))}
                    history = rp_raw.get("histories", {}).get(uid_str, [])
                    await cur.execute(
                        "INSERT INTO roleplay_state (channel_id, state_json, history_json) VALUES (%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE state_json=VALUES(state_json), history_json=VALUES(history_json)",
                        (int(ch_id), json.dumps(rp_s), json.dumps(history)),
                    )
                    n += 1
                counts["roleplay_state"] = n

                # fanfic
                fanfic_owners = _load_json("data/fanfic_owners.json", {})
                fanfic_histories = _load_json("data/fanfic_histories.json", {})
                n = 0
                for tid_str, data in fanfic_owners.items():
                    await cur.execute(
                        "INSERT INTO fanfic_owners (thread_id, owner_id, invited_ids_json) VALUES (%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE owner_id=VALUES(owner_id), invited_ids_json=VALUES(invited_ids_json)",
                        (int(tid_str), data["owner_id"], json.dumps(list(data["invited_ids"]))),
                    )
                    n += 1
                counts["fanfic_owners"] = n
                n = 0
                for ch_id_str, history in fanfic_histories.items():
                    await cur.execute(
                        "INSERT INTO fanfic_histories (channel_id, history_json) VALUES (%s,%s)"
                        " ON DUPLICATE KEY UPDATE history_json=VALUES(history_json)",
                        (int(ch_id_str), json.dumps(history)),
                    )
                    n += 1
                counts["fanfic_histories"] = n

                # balance_history
                bh = _load_json("data/balance_history.json", {})
                n = 0
                for date_str, users in bh.items():
                    for uid_str, vals in users.items():
                        await cur.execute(
                            "INSERT INTO balance_history (snapshot_date, user_id, wallet, savings)"
                            " VALUES (%s,%s,%s,%s)"
                            " ON DUPLICATE KEY UPDATE wallet=VALUES(wallet), savings=VALUES(savings)",
                            (date_str, int(uid_str), vals.get("wallet", 0), vals.get("savings", 0)),
                        )
                        n += 1
                counts["balance_history"] = n

                # bot_stats_history
                bsh = _load_json("data/bot_stats_history.json", {})
                n = 0
                for date_str, vals in bsh.items():
                    await cur.execute(
                        "INSERT INTO bot_stats_history"
                        " (snapshot_date, messages, commands, ai_responses, ai_up, memory_mb)"
                        " VALUES (%s,%s,%s,%s,%s,%s)"
                        " ON DUPLICATE KEY UPDATE messages=VALUES(messages), commands=VALUES(commands),"
                        " ai_responses=VALUES(ai_responses), ai_up=VALUES(ai_up), memory_mb=VALUES(memory_mb)",
                        (date_str, vals.get("messages", 0), vals.get("commands", 0),
                         vals.get("ai_responses", 0), vals.get("ai_up", False), vals.get("memory_mb", 0.0)),
                    )
                    n += 1
                counts["bot_stats_history"] = n

        lines = [f"**{k}**: {v}" for k, v in sorted(counts.items())]
        await ctx.send(embed=emb(
            "✅ Migration Complete",
            "Migrated from JSON files:\n" + "\n".join(lines),
            C_GREEN,
        ))


async def setup(bot):
    await bot.add_cog(MigrateCog(bot))
