"""What the sidecar may read, table by table and column by column.

This is the single allowlist. The DB grants (`scripts/mcp_db_user.sql`,
generated from here by `python -m botmcp.grants`) and the query builder
(botmcp/query.py) both read it, and `tests/test_catalog.py` fails when a
table or column in the live schema has no entry — so a new migration can't
expose anything until someone has classified it. See CLAUDE.md: Read-only
MCP sidecar for the tiers.

Three kinds of entry:
  - `Table(columns=…)`: readable. Every column is listed; the grant is
    column-level, so the account can't SELECT anything else even by hand.
  - `hidden={col: why}`: the columns of a readable table that stay out
    (free text a user typed, display names, moderation flags).
  - `Table(never="why")`: no grant at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...] = ()
    hidden: dict[str, str] = field(default_factory=dict)
    never: str | None = None
    note: str | None = None

    @property
    def readable(self) -> bool:
        return self.never is None


_USER_TEXT = "free text a user typed"
_NAME = "a display name"

TABLES: dict[str, Table] = {
    # ── never ────────────────────────────────────────────────────────────
    "ai_threads": Table(never="AI transcripts and the prompts users wrote"),
    "blocklist": Table(never="moderation: who the bot ignores, and why"),
    "global_blocklist": Table(never="moderation: who the bot ignores, and why"),
    "bot_settings": Table(never="the admin / error log channel ids"),
    "channel_prompts": Table(never="admin-written system prompts"),
    "counter_perms": Table(never="permission grants"),
    "ephemeral_msgs": Table(never="bot plumbing, nothing to learn"),
    "error_mutes": Table(never="mute keys carry exception text"),
    "godmode_users": Table(never="an admin cheat list"),
    "mc_trades": Table(never="operator-only audit trail keyed by gamertag"),
    "quote_log": Table(never="raw message text"),
    "restart_msg": Table(never="bot plumbing, nothing to learn"),
    "rigged_flips": Table(never="an admin cheat"),
    "rigged_scratch": Table(never="an admin cheat"),
    "rigged_slots": Table(never="an admin cheat"),
    "rigged_steal": Table(never="an admin cheat"),
    "saved_quotes": Table(never="message text and author names"),
    "user_perm_overrides": Table(never="permission grants"),
    "voice_ping_ignores": Table(never="who ignores whom"),
    "voice_pings": Table(never="presence subscriptions"),
    # ── readable ─────────────────────────────────────────────────────────
    "balance_history": Table(("snapshot_date", "user_id", "wallet", "savings", "bucket", "assets", "asset_revenue")),
    "bot_command_usage_history": Table(("snapshot_date", "cog_name", "count", "bucket")),
    "bot_roles": Table(("role_id", "guild_id", "rank_pos")),
    "bot_stats_history": Table(("snapshot_date", "messages", "commands", "ai_responses", "ai_up", "memory_mb", "bucket", "ping_ms", "mc_up", "mc_ping_ms")),
    "bounties": Table(
        ("id", "guild_id", "channel_id", "message_id", "author_id", "amount", "status", "claimant_id",
         "dm_message_id", "contest_message_id", "poll_message_id", "poll_channel_id", "claim_expires_at",
         "contest_expires_at", "poll_expires_at", "created_at", "expires_at"),
        hidden={"condition": _USER_TEXT, "claim_log": "a log of user-written claims"},
    ),
    "bounty_claims": Table(
        ("id", "bounty_id", "claimant_id", "status", "dm_message_id", "contest_message_id", "poll_message_id",
         "poll_channel_id", "claim_expires_at", "contest_expires_at", "poll_expires_at", "created_at"),
        hidden={"poll_votes": "who voted which way"},
    ),
    "chess_equipped": Table(("user_id", "piece_set", "board_theme")),
    "chess_games": Table(("channel_id", "fen", "pgn", "white_id", "black_id", "current_id", "amount", "last_move",
                          "board_msg_id", "started_at", "elo", "embed_msg_id", "turn_started_at", "white_seconds", "black_seconds")),
    "chess_reports": Table(
        ("report_id", "guild_id", "channel_id", "white_id", "black_id", "winner_id", "result", "pgn", "final_fen",
         "finished_at", "elo", "analysis_json"),
        hidden={"flag_user_id": "a cheat-suspect flag for admin review"},
    ),
    "chess_unlocks": Table(("user_id", "item_id")),
    "chess_user_stats": Table(("user_id", "max_elo_defeated", "total_elo_defeated", "bonus_bins", "elo_spent")),
    "command_perms": Table(("command_name", "tier", "hidden"), note="mirrors the committed src/command_perms.json"),
    "command_streak": Table(("user_id", "last_date", "streak_count")),
    "counter_values": Table(("guild_id", "name", "user_id", "value")),
    "counters": Table(("guild_id", "name", "user_required", "kind", "created_by", "created_at"),
                      hidden={"description": _USER_TEXT}),
    "crime_history": Table(("snapshot_date", "user_id", "gained", "lost", "bucket", "guild_id")),
    "daily_counters": Table(("day", "counter", "value")),
    "economy_meta": Table(("key_name", "value_text")),
    "economy_users": Table(
        ("user_id", "balance", "last_daily", "daily_date", "scratch_used", "scratch_date", "jailbreak_used",
         "jail_until", "savings", "crime_eligible", "bail_amount", "bot_chess_elo_max_today",
         "bot_chess_elo_max_date", "scratch_won_today", "property_paid_at", "property_revenue_total",
         "daily_gamble_property", "ins_paid_since_claim", "ins_lapsed_since_claim", "heist_success_day",
         "property_theft_day", "bail_price", "auto_repair", "robbed_since_claim", "investigations_since_claim",
         "arrests_since_claim", "auto_repaired_since_claim", "targeted_buy_days",
         "insurance_claims"),
        hidden={"jail_reason": "free text, sometimes admin-written"},
    ),
    "feature_request_watchers": Table(("message_id", "user_id", "created_at")),
    "feature_requests": Table(
        ("id", "guild_id", "channel_id", "message_id", "reporter_id", "status", "feature_issue_id", "created_at",
         "resolved_by", "resolved_at"),
        hidden={"description": _USER_TEXT},
    ),
    "gambler_streak": Table(("user_id", "last_full_date", "streak_count")),
    "gambling_history": Table(("snapshot_date", "user_id", "gained", "lost", "bucket", "guild_id")),
    "gambling_threads": Table(("thread_id", "owner_id", "guild_id", "parent_id", "created_at"),
                              hidden={"tally_json": "carries player display names"}),
    "guild_house_balance": Table(("guild_id", "balance")),
    "guild_settings": Table(("guild_id", "settings_json"),
                            note="the blob is projected through GUILD_SETTINGS_KEYS before it leaves"),
    "idle_characters": Table(
        ("guild_id", "user_id", "class_name", "level", "next_level_at", "remaining", "law", "moral", "prestige",
         "penalty_total", "last_seen", "last_penalty_at", "thread_id", "created_at", "align_changed_at", "duel_day",
         "items_json", "x", "y", "gold", "rush_day", "extra_duel_day", "auto_trade", "traded_at", "travel_to",
         "mob_kills", "mob_deaths", "gamble_town", "gamble_visit_at", "gamble_budget", "gambles", "gamble_won",
         "gamble_lost", "claimed", "hp", "loot_json", "titles_json", "title", "boost_pct", "boost_until", "hunt_mob",
         "hunt_count", "hunt_killed", "hunt_x", "hunt_y", "hunt_at", "hunts_done", "potions"),
    ),
    "idle_guild_events": Table(("guild_id", "kind", "detail", "cast_by", "stage", "starts_at", "ends_at")),
    "idle_optouts": Table(("guild_id", "user_id")),
    "idle_quests": Table(("guild_id", "members_json", "description", "ends_at", "not_before", "kind", "stage",
                          "p1x", "p1y", "p2x", "p2y"), note="description is the game's own quest text"),
    "issues": Table(
        ("id", "guild_id", "channel_id", "message_id", "reporter_id", "status", "created_at", "resolved_by",
         "resolved_at", "kind", "deleted", "source_channel_id", "source_message_id"),
        hidden={"report": "free text, and auto-filed errors carry exception text", "mute_key": "carries exception text"},
    ),
    "leveling": Table(("guild_id", "user_id", "data"), note="data is projected to xp and level before it leaves"),
    "levelup_history": Table(("snapshot_date", "guild_id", "user_id", "count", "bucket")),
    "lottery": Table(("guild_id", "prize_pool", "last_posted_week", "last_drawn_week")),
    "lottery_players": Table(("guild_id", "user_id", "tickets")),
    "lottery_ticket_grants": Table(("guild_id", "user_id", "daily_day", "chess_period", "chess_tickets")),
    "mc_daily_ping_stats": Table(("stat_date", "avg_ping", "min_ping", "max_ping")),
    "mc_daily_player_stats": Table(("stat_date", "max_concurrent", "total_joins", "player_seconds")),
    "mc_ping_samples": Table(("ts", "online", "latency_ms")),
    "mc_player_events": Table(("ts", "delta", "players")),
    "mc_players": Table(("user_id", "blocks", "linked_at"), hidden={"gamertag": "links a Discord identity to an Xbox one"}),
    "mc_server_versions": Table(("ts", "version")),
    "notable_events": Table(("id", "guild_id", "event_day", "kind", "category", "value", "created_at"),
                            hidden={"holder_name": _NAME}),
    "property_owners": Table(("property_id", "owner_id", "acquired_at", "list_price", "listed_at", "upgraded", "secured",
                              "damaged_at", "repair_cost", "repair_cover_pct", "repair_cover_cap"),
                             hidden={"custom_name": _USER_TEXT}),
    "property_releases": Table(("property_id", "released_at")),
    "recap_usage": Table(("guild_id", "user_id", "last_date")),
    "records": Table(("guild_id", "category", "value", "holder_id", "extra_json"), hidden={"holder_name": _NAME}),
    "schema_migrations": Table(("version", "name", "applied_at"), hidden={"checksum": "not useful, and hex-shaped like a secret"}),
    "shop_effects": Table(
        ("user_id", "effect_type", "remaining", "started_by", "cursed_by", "master_id", "tax_type", "tax_emoji",
         "channel_id", "activated_at", "guild_id", "expires_at", "insurance_tier"),
        hidden={"history_json": "the ragebait AI transcript"},
    ),
    "slots_jackpot": Table(("id", "jackpot")),
    "user_artifacts": Table(("user_id", "artifact_id", "quantity", "acquired_at")),
}

# The keys of guild_settings.settings_json that `guild_settings` returns.
# Positive list: a key the bot adds later stays hidden until it is added here.
# Out by design: nsfw_* (what a server allows and bans), story_aliases
# (admin-written system prompts), dailies_keep_ids (plumbing).
GUILD_SETTINGS_KEYS: tuple[str, ...] = (
    "features", "ai_enabled", "ai_channels", "bot_channels", "game_channels", "chess_channels",
    "lottery_channel", "dailies_channel", "records_channel", "levelup_channel", "game_servers_channel",
    "idle_channel", "bounty_channel",
    "feature_request_channel", "command_whitelist", "command_blacklist", "gambler_role_enabled",
    "idle_enroll", "idle_pace", "leaderboard_default_scope", "dailies_reset_day",
    "soundboard_ratelimit", "event_budget", "shop_items", "mc_shop", "mc_join_notices",
    "vrising_join_notices", "ask_model", "roleplay_model", "coding_model", "locked_channels",
    "locked_roles", "tax_aliases", "quote_bypass_restrictions", "dailies_message_id",
    "idle_board_message",
)

# leveling.data holds xp, level and rate-limit timestamps; only these leave.
LEVELING_DATA_KEYS: tuple[str, ...] = ("xp", "level")


def readable() -> dict[str, Table]:
    return {name: t for name, t in TABLES.items() if t.readable}


def columns_of(table: str) -> tuple[str, ...]:
    t = TABLES.get(table)
    if t is None or not t.readable:
        raise KeyError(table)
    return t.columns
