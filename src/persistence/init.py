"""init_db_state: apply pending migrations and load all persistent state from DB.

The `_init_db_state_done` guard lives on the `src.persistence` package (not
this submodule) so test fixtures can flip it via `_persistence._init_db_state_done = False`.
"""
import json
import logging
import time as _time

from src.config import COMMAND_PERMS_FILE, INITIAL_BOT_ADMIN_IDS, SLOT_JACKPOT_SEED
from src.db import with_cursor
from src.persistence._helpers import _load_json
from src.persistence.history import (
    load_today_crime_row, load_today_gambling_row, load_today_levelups_row,
)


async def init_db_state():
    """Apply pending migrations, then load all persistent state from DB into src.state.

    on_ready fires on every gateway reconnect, so the second+ call is guarded
    out — otherwise a reconnect would clobber any in-memory mutations made
    since the last save (e.g. an in-progress chess game, an active ragebait).

    The guard is only set after a successful load: if migrations or the DB
    connection raise, the next reconnect retries the full load instead of
    skipping it and unblocking on_message against partially-loaded state.
    """
    import src.persistence as _pkg
    import src.state as state
    from src.migrations import run_migrations

    if _pkg._init_db_state_done:
        logging.info("[init_db_state] skipping; already initialized")
        _pkg.init_done.set()
        return

    # Only flip the guard after a successful load. If migrations or the DB
    # raise, the exception propagates and discord.py will retry on reconnect
    # against the same (still-False) guard, re-doing the full load.
    # init_done stays unset on failure: the guards in _ensure_user and
    # grant_xp blocking forever is preferable to writing zero-baselined
    # rows over real DB data from partially-loaded state.
    await _init_db_state_inner(state, run_migrations)
    _pkg._init_db_state_done = True
    _pkg.init_done.set()


async def _init_db_state_inner(state, run_migrations):
    # Apply pending schema migrations BEFORE any SELECT — if the schema is
    # behind, we want to fail fast rather than blow up on a missing column.
    await run_migrations()

    async with with_cursor() as cur:

        # ── economy_users ─────────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT user_id, balance, last_daily, daily_date, scratch_used,"
                " scratch_date, jailbreak_used, jail_until, savings, jail_reason,"
                " crime_eligible, bail_amount, bot_chess_elo_max_today,"
                " bot_chess_elo_max_date, lottery_disc_used, lottery_disc_date"
                " FROM economy_users"
            )
            for row in await cur.fetchall():
                (uid, bal, last_daily, daily_date, scratch_used, scratch_date,
                 jb_used, jail_until, savings_json, jail_reason,
                 crime_eligible, bail_amount,
                 bot_chess_elo_max_today, bot_chess_elo_max_date,
                 lottery_disc_used, lottery_disc_date) = row
                state.economy["users"][str(uid)] = {
                    "balance": bal,
                    "last_daily": last_daily,
                    "daily_date": daily_date,
                    "scratch_used": scratch_used,
                    "scratch_date": scratch_date,
                    "jailbreak_used": bool(jb_used),
                    "jail_until": jail_until,
                    "savings": json.loads(savings_json) if savings_json else [],
                    "jail_reason": jail_reason,
                    "crime_eligible": bool(crime_eligible),
                    "bail_amount": int(bail_amount or 0),
                    "bot_chess_elo_max_today": int(bot_chess_elo_max_today or 0),
                    "bot_chess_elo_max_date": bot_chess_elo_max_date,
                    "lottery_disc_used": int(lottery_disc_used or 0),
                    "lottery_disc_date": lottery_disc_date,
                }
        except Exception as e:
            logging.error(f"[init_db_state] economy_users failed: {e}", exc_info=True)
            raise

        # ── economy_meta ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT value_text FROM economy_meta WHERE key_name='last_daily_reset'")
            row = await cur.fetchone()
            state.economy["last_daily_reset"] = row[0] if row and row[0] is not None else None
        except Exception as e:
            logging.error(f"[init_db_state] economy_meta failed: {e}", exc_info=True)
            raise

        # ── guild_house_balance ───────────────────────────────────────────
        try:
            await cur.execute("SELECT guild_id, balance FROM guild_house_balance")
            for guild_id, bal in await cur.fetchall():
                state.economy["guild_house"][str(guild_id)] = bal
        except Exception as e:
            logging.error(f"[init_db_state] guild_house_balance failed: {e}", exc_info=True)
            raise

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
            raise

        # ── slots_jackpot ─────────────────────────────────────────────────
        try:
            await cur.execute("SELECT jackpot FROM slots_jackpot WHERE id=1")
            row = await cur.fetchone()
            state.slot_jackpot = max(SLOT_JACKPOT_SEED, row[0] if row else SLOT_JACKPOT_SEED)
        except Exception as e:
            logging.error(f"[init_db_state] slots_jackpot failed: {e}", exc_info=True)
            raise

        # ── bot_roles ─────────────────────────────────────────────────────
        try:
            await cur.execute("SELECT role_id, guild_id, rank_pos FROM bot_roles")
            rows = await cur.fetchall()
            state.bot_roles = {r[0] for r in rows}
            state.bot_role_ranks = {(r[1], r[0]): r[2] for r in rows}
        except Exception as e:
            logging.error(f"[init_db_state] bot_roles failed: {e}", exc_info=True)
            raise

        # ── godmode_users ─────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id FROM godmode_users")
            state.godmode_users = {r[0] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] godmode_users failed: {e}", exc_info=True)
            raise

        # ── bot_settings ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT key_name, value_text FROM bot_settings")
            for k, v in await cur.fetchall():
                state.bot_settings[k] = v
        except Exception as e:
            logging.error(f"[init_db_state] bot_settings failed: {e}", exc_info=True)
            raise

        # ── shop_effects: insurance ───────────────────────────────────────
        # Insurance lives in shop_effects (effect_type='insurance') since
        # migration 0032: protected_from in history_json, expiry in expires_at.
        try:
            await cur.execute(
                "SELECT guild_id, user_id, expires_at, history_json"
                " FROM shop_effects WHERE effect_type='insurance'"
            )
            now = _time.time()
            for guild_id, uid, expires_at, protected_json in await cur.fetchall():
                if expires_at and expires_at > now:
                    state.insurance[(int(guild_id), int(uid))] = {
                        "expires_at": expires_at,
                        "protected_from": json.loads(protected_json) if protected_json else [],
                    }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.insurance failed: {e}", exc_info=True)
            raise

        # ── shop_effects: ragebait ────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT guild_id, user_id, remaining, started_by, history_json, channel_id"
                " FROM shop_effects WHERE effect_type='ragebait'"
            )
            for guild_id, uid, remaining, started_by, history_json, channel_id in await cur.fetchall():
                state.active_ragebaits[(int(guild_id), int(uid))] = {
                    "remaining": remaining,
                    "started_by": started_by,
                    "history": json.loads(history_json) if history_json else [],
                    "channel_id": channel_id,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.ragebait failed: {e}", exc_info=True)
            raise

        # ── shop_effects: mock ────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT guild_id, user_id, remaining, started_by, channel_id"
                " FROM shop_effects WHERE effect_type='mock'"
            )
            for guild_id, uid, remaining, started_by, channel_id in await cur.fetchall():
                state.active_mocks[(int(guild_id), int(uid))] = {
                    "remaining": remaining,
                    "started_by": started_by,
                    "channel_id": channel_id,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.mock failed: {e}", exc_info=True)
            raise

        # ── shop_effects: curse ───────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT guild_id, user_id, remaining, cursed_by, channel_id"
                " FROM shop_effects WHERE effect_type='curse'"
            )
            for guild_id, uid, remaining, cursed_by, channel_id in await cur.fetchall():
                state.active_curses[(int(guild_id), int(uid))] = {
                    "remaining": remaining,
                    "cursed_by": cursed_by,
                    "channel_id": channel_id,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.curse failed: {e}", exc_info=True)
            raise

        # ── shop_effects: tax ─────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT guild_id, user_id, master_id, tax_type, tax_emoji, channel_id, activated_at, expires_at"
                " FROM shop_effects WHERE effect_type='tax'"
            )
            for guild_id, uid, master_id, tax_type, tax_emoji, channel_id, activated_at, expires_at in await cur.fetchall():
                state.active_taxes[(int(guild_id), int(uid))] = {
                    "master": master_id,
                    "type": tax_type,
                    "emoji": tax_emoji,
                    "channel_id": channel_id,
                    "activated_at": activated_at,
                    "expires_at": expires_at,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.tax failed: {e}", exc_info=True)
            raise

        # ── shop_effects: spellcheck ──────────────────────────────────────
        try:
            await cur.execute(
                "SELECT guild_id, user_id, master_id, remaining, channel_id, activated_at, expires_at"
                " FROM shop_effects WHERE effect_type='spellcheck'"
            )
            for guild_id, uid, master_id, remaining, channel_id, activated_at, expires_at in await cur.fetchall():
                state.active_spellchecks[(int(guild_id), int(uid))] = {
                    "started_by": master_id,
                    "days": remaining,
                    "channel_id": channel_id,
                    "activated_at": activated_at,
                    "expires_at": expires_at,
                }
        except Exception as e:
            logging.error(f"[init_db_state] shop_effects.spellcheck failed: {e}", exc_info=True)
            raise

        # ── bounties (+ their in-flight claims) ───────────────────────────
        # Load every open bounty with its non-terminal claims attached so
        # reactions and the expiry loop keep working after a reboot. Keyed by
        # the bounty embed's message_id.
        try:
            from src.persistence.bounties import (
                _BOUNTY_COLS, _CLAIM_COLS, _row_to_bounty, _row_to_claim,
            )
            await cur.execute(
                f"SELECT {_BOUNTY_COLS} FROM bounties WHERE status='open'"  # nosec B608 - _BOUNTY_COLS is a literal
            )
            by_id = {}
            for row in await cur.fetchall():
                bounty = _row_to_bounty(row)
                state.active_bounties[bounty["message_id"]] = bounty
                by_id[bounty["id"]] = bounty
            if by_id:
                await cur.execute(
                    f"SELECT {_CLAIM_COLS} FROM bounty_claims"  # nosec B608 - _CLAIM_COLS is a literal
                    " WHERE status IN ('pending','contesting','polling')"
                )
                for crow in await cur.fetchall():
                    claim = _row_to_claim(crow)
                    parent = by_id.get(claim["bounty_id"])
                    if parent is not None:
                        parent["claims"].append(claim)
        except Exception as e:
            logging.error(f"[init_db_state] bounties failed: {e}", exc_info=True)
            raise

        # ── user_artifacts ────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, artifact_id, quantity FROM user_artifacts")
            state.user_artifacts = {}
            for r in await cur.fetchall():
                state.user_artifacts.setdefault(int(r[0]), {})[r[1]] = int(r[2])
        except Exception as e:
            logging.error(f"[init_db_state] user_artifacts failed: {e}", exc_info=True)
            raise

        # ── rigged_slots ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, symbol FROM rigged_slots")
            # int keys — runtime lookups use ctx.author.id (int); str keys made
            # pre-reboot rigs silently never fire and impossible to cancel.
            state.rigged_slots = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] rigged_slots failed: {e}", exc_info=True)
            raise

        # ── rigged_flips ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, remaining_wins FROM rigged_flips")
            state.rigged_flips = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] rigged_flips failed: {e}", exc_info=True)
            raise

        # ── rigged_scratch ────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, symbols_count FROM rigged_scratch")
            state.rigged_scratch = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] rigged_scratch failed: {e}", exc_info=True)
            raise

        # ── rigged_steal ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, remaining_successes FROM rigged_steal")
            state.rigged_steal = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] rigged_steal failed: {e}", exc_info=True)
            raise

        # ── gambler_streak ────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, last_full_date, streak_count FROM gambler_streak")
            state.gambler_streak = {
                str(r[0]): {"date": r[1], "count": int(r[2])} for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] gambler_streak failed: {e}", exc_info=True)
            raise

        # ── command_streak ────────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, last_date, streak_count FROM command_streak")
            state.command_streak = {
                str(r[0]): {"date": r[1], "count": int(r[2])} for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] command_streak failed: {e}", exc_info=True)
            raise

        # ── daily_counters: lottery tickets presence line ─────────────────
        try:
            # Local import — src.economy imports src.persistence at module scope.
            from src.economy import _ct_today
            today = _ct_today()
            await cur.execute(
                "SELECT value FROM daily_counters WHERE day=%s AND counter=%s",
                (today, "lottery_tickets"),
            )
            row = await cur.fetchone()
            state.lottery_tickets_today["date"] = today
            state.lottery_tickets_today["count"] = int(row[0]) if row else 0
        except Exception as e:
            logging.error(f"[init_db_state] daily_counters failed: {e}", exc_info=True)
            raise

        # ── recap_usage ───────────────────────────────────────────────────
        try:
            await cur.execute("SELECT guild_id, user_id, last_date FROM recap_usage")
            state.recap_usage = {
                (int(r[0]), int(r[1])): r[2] for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] recap_usage failed: {e}", exc_info=True)
            raise

        # ── voice_pings ───────────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT guild_id, channel_id, user_id, last_pinged_at FROM voice_pings"
            )
            state.voice_pings = {
                (int(r[1]), int(r[2])): {
                    "guild_id": int(r[0]),
                    "last_pinged_at": int(r[3]) if r[3] is not None else None,
                }
                for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] voice_pings failed: {e}", exc_info=True)
            raise

        # ── voice_ping_ignores ────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT guild_id, user_id, ignored_user_id FROM voice_ping_ignores"
            )
            ignores: dict = {}
            for r in await cur.fetchall():
                ignores.setdefault((int(r[0]), int(r[1])), set()).add(int(r[2]))
            state.voice_ping_ignores = ignores
        except Exception as e:
            logging.error(f"[init_db_state] voice_ping_ignores failed: {e}", exc_info=True)
            raise

        # ── chess_games ───────────────────────────────────────────────────
        try:
            await cur.execute(
                "SELECT channel_id, fen, pgn, white_id, black_id, current_id, "
                "amount, last_move, board_msg_id, elo, embed_msg_id, "
                "turn_started_at, white_seconds, black_seconds "
                "FROM chess_games"
            )
            rows = await cur.fetchall()
            state.active_chess_games = {}
            for r in rows:
                game = {
                    "fen": r[1],
                    "pgn": r[2],
                    "white_id": r[3],
                    "black_id": r[4],
                    "current_id": r[5],
                    "amount": r[6],
                    "last_move": r[7],
                    "board_msg_id": r[8],
                }
                if r[9] is not None:
                    game["elo"] = r[9]
                if r[10] is not None:
                    game["embed_msg_id"] = r[10]
                if r[11] is not None:
                    game["turn_started_at"] = r[11]
                game["white_seconds"] = int(r[12] or 0)
                game["black_seconds"] = int(r[13] or 0)
                state.active_chess_games[r[0]] = game
        except Exception as e:
            logging.error(f"[init_db_state] chess_games failed: {e}", exc_info=True)
            raise

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
            raise

        # ── quote_log ─────────────────────────────────────────────────────
        try:
            await cur.execute("SELECT content FROM quote_log ORDER BY id")
            state.quote_log = [r[0] for r in await cur.fetchall()]
        except Exception as e:
            logging.error(f"[init_db_state] quote_log failed: {e}", exc_info=True)
            raise

        # ── leveling — nested by guild_id ─────────────────────────────────
        try:
            await cur.execute("SELECT guild_id, user_id, data FROM leveling")
            for guild_id, uid, data_json in await cur.fetchall():
                rec = json.loads(data_json) if data_json else {}
                state.leveling.setdefault(str(guild_id), {})[str(uid)] = rec
        except Exception as e:
            logging.error(f"[init_db_state] leveling failed: {e}", exc_info=True)
            raise

        # ── channel_prompts ───────────────────────────────────────────────
        try:
            await cur.execute("SELECT channel_id, prompt_text FROM channel_prompts")
            state.channel_prompts = {r[0]: r[1] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] channel_prompts failed: {e}", exc_info=True)
            raise

        # ── command_perms ─────────────────────────────────────────────────
        # The JSON file is the source of truth: upsert every row from JSON,
        # then delete any DB rows whose command isn't in the JSON. Runtime
        # !setperm changes are intentionally transient — port them back to
        # command_perms.json to make them permanent.
        try:
            json_perms = _load_json(COMMAND_PERMS_FILE, None)
            if not json_perms:
                # Fail closed: reconciling from a missing/corrupt/empty JSON
                # would wipe the command_perms table and drop every restricted
                # command (godmode, restart, …) to the everyone tier.
                raise RuntimeError(
                    f"command_perms file missing, corrupt, or empty: {COMMAND_PERMS_FILE}"
                )
            for cmd, data in json_perms.items():
                await cur.execute(
                    "INSERT INTO command_perms (command_name, tier, hidden) VALUES (%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE tier=VALUES(tier), hidden=VALUES(hidden)",
                    (cmd, data.get("tier", "everyone"), bool(data.get("hidden", False))),
                )
            placeholders = ",".join(["%s"] * len(json_perms))
            await cur.execute(
                f"DELETE FROM command_perms WHERE command_name NOT IN ({placeholders})",  # nosec B608 - placeholders is only "%s,%s,...", values are bound
                tuple(json_perms.keys()),
            )
            await cur.execute("SELECT command_name, tier, hidden FROM command_perms")
            state.command_perms = {
                r[0]: {"tier": r[1], "hidden": bool(r[2])}
                for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] command_perms failed: {e}", exc_info=True)
            raise

        # ── user_perm_overrides ──────────────────────────────────────────
        try:
            await cur.execute("SELECT guild_id, user_id, tier FROM user_perm_overrides")
            state.user_perm_overrides = {
                (int(r[0]), int(r[1])): r[2] for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] user_perm_overrides failed: {e}", exc_info=True)
            raise

        # ── blocklist (per-guild) ────────────────────────────────────────
        try:
            await cur.execute("SELECT guild_id, user_id, reason, banned_by, banned_at FROM blocklist")
            state.blocklist = {
                (int(r[0]), int(r[1])): {
                    "reason": r[2],
                    "banned_by": int(r[3]),
                    "banned_at": r[4],
                }
                for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] blocklist failed: {e}", exc_info=True)
            raise

        # ── global_blocklist ─────────────────────────────────────────────
        try:
            await cur.execute("SELECT user_id, reason, banned_by, banned_at FROM global_blocklist")
            state.global_blocklist = {
                int(r[0]): {
                    "reason": r[1],
                    "banned_by": int(r[2]),
                    "banned_at": r[3],
                }
                for r in await cur.fetchall()
            }
        except Exception as e:
            logging.error(f"[init_db_state] global_blocklist failed: {e}", exc_info=True)
            raise

        # ── error_mutes ──────────────────────────────────────────────────
        try:
            await cur.execute("SELECT mute_key FROM error_mutes")
            state.error_mutes = {r[0] for r in await cur.fetchall()}
        except Exception as e:
            logging.error(f"[init_db_state] error_mutes failed: {e}", exc_info=True)
            raise

        # ── activity caches (crime / gambling / levelups) ─────────────────
        # Each *_history table is the source of truth (atomic UPSERT on every
        # event). The in-memory dicts are a fast-read cache for the graph cog's
        # live-today append; on boot they're empty, so re-hydrate today's row.
        try:
            from src.economy import _ct_now, _current_bucket_ct
            today = _ct_now().date().isoformat()
            bucket = _current_bucket_ct()
            state.crime_today_by_user = await load_today_crime_row(today, bucket)
            state.gambling_today_by_user = await load_today_gambling_row(today, bucket)
            state.levelups_today = await load_today_levelups_row(today, bucket)
            # Mark which bucket the freshly-hydrated dicts reflect, so the
            # first record_* call after boot doesn't see "old" bucket and
            # wipe the just-loaded data.
            state._crime_bucket = bucket
            state._gambling_bucket = bucket
            state._levelups_bucket = bucket
        except Exception as e:
            logging.error(f"[init_db_state] activity hydration failed: {e}", exc_info=True)
            raise

    # bot_admins: always from env, never DB
    state.bot_admins = set(INITIAL_BOT_ADMIN_IDS)
