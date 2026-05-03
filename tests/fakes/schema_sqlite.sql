-- SQLite translation of src/schema.sql for tests.
-- Hand-translated; keep in sync if src/schema.sql changes.
-- Translation rules:
--   BIGINT UNSIGNED / TINYINT / BOOLEAN / INT  -> INTEGER
--   DOUBLE / FLOAT                              -> REAL
--   JSON / VARCHAR(N) / ENUM(...) / TEXT        -> TEXT
--   DEFAULT (JSON_ARRAY())                      -> DEFAULT '[]'
--   INSERT IGNORE                               -> INSERT OR IGNORE
--   ENGINE=InnoDB DEFAULT CHARSET=utf8mb4       -> dropped
--   inline INDEX idx_x(col)                     -> separate CREATE INDEX

CREATE TABLE IF NOT EXISTS economy_users (
    user_id        INTEGER NOT NULL PRIMARY KEY,
    balance        INTEGER NOT NULL DEFAULT 0,
    last_daily     REAL    NOT NULL DEFAULT 0,
    daily_date     TEXT    NULL,
    scratch_used   INTEGER NOT NULL DEFAULT 0,
    scratch_date   TEXT    NULL,
    jailbreak_used INTEGER NOT NULL DEFAULT 0,
    jail_until     REAL    NOT NULL DEFAULT 0,
    savings        TEXT    NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS economy_meta (
    key_name   TEXT NOT NULL PRIMARY KEY,
    value_text TEXT NULL
);

CREATE TABLE IF NOT EXISTS guild_house_balance (
    guild_id INTEGER NOT NULL PRIMARY KEY,
    balance  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id      INTEGER NOT NULL PRIMARY KEY,
    settings_json TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_roles (
    role_id INTEGER NOT NULL PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS godmode_users (
    user_id INTEGER NOT NULL PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key_name   TEXT NOT NULL PRIMARY KEY,
    value_text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shop_insurance (
    user_id        INTEGER NOT NULL PRIMARY KEY,
    expires_at     REAL    NOT NULL,
    protected_from TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS shop_effects (
    user_id      INTEGER NOT NULL,
    effect_type  TEXT    NOT NULL,
    remaining    INTEGER NULL,
    started_by   INTEGER NULL,
    cursed_by    INTEGER NULL,
    history_json TEXT    NULL,
    master_id    INTEGER NULL,
    tax_type     TEXT    NULL,
    tax_emoji    TEXT    NULL,
    channel_id   INTEGER NULL,
    activated_at REAL    NULL,
    PRIMARY KEY (user_id, effect_type)
);

CREATE TABLE IF NOT EXISTS rigged_slots (
    user_id INTEGER NOT NULL PRIMARY KEY,
    symbol  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS rigged_flips (
    user_id        INTEGER NOT NULL PRIMARY KEY,
    remaining_wins INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rigged_scratch (
    user_id       INTEGER NOT NULL PRIMARY KEY,
    symbols_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rigged_steal (
    user_id             INTEGER NOT NULL PRIMARY KEY,
    remaining_successes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS slots_jackpot (
    id      INTEGER NOT NULL PRIMARY KEY DEFAULT 1,
    jackpot INTEGER NOT NULL DEFAULT 5000
);
INSERT OR IGNORE INTO slots_jackpot (id, jackpot) VALUES (1, 5000);

CREATE TABLE IF NOT EXISTS lottery (
    guild_id         INTEGER NOT NULL PRIMARY KEY,
    prize_pool       INTEGER NOT NULL DEFAULT 0,
    last_posted_week INTEGER NOT NULL DEFAULT 0,
    last_drawn_week  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lottery_players (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    tickets  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS records (
    guild_id    INTEGER NOT NULL,
    category    TEXT    NOT NULL,
    value       INTEGER NOT NULL,
    holder_id   INTEGER NOT NULL,
    holder_name TEXT    NOT NULL,
    extra_json  TEXT    NULL,
    PRIMARY KEY (guild_id, category)
);

CREATE TABLE IF NOT EXISTS channel_prompts (
    channel_id  INTEGER NOT NULL PRIMARY KEY,
    prompt_text TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS chess_games (
    channel_id INTEGER NOT NULL PRIMARY KEY,
    game_json  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_threads (
    thread_id        INTEGER NOT NULL PRIMARY KEY,
    kind             TEXT    NOT NULL,
    owner_id         INTEGER NOT NULL,
    guild_id         INTEGER,
    invited_ids_json TEXT    NOT NULL,
    system_prompt    TEXT,
    character_prompt TEXT,
    history_json     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS leveling (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    data     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS gambler_streak (
    user_id        INTEGER NOT NULL PRIMARY KEY,
    last_full_date TEXT    NOT NULL,
    streak_count   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS quote_log (
    id         INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    content    TEXT    NOT NULL,
    created_at TEXT    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS saved_quotes (
    id         INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    guild_id   TEXT    NOT NULL,
    quote_json TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_quotes_guild ON saved_quotes(guild_id);

CREATE TABLE IF NOT EXISTS balance_history (
    snapshot_date TEXT    NOT NULL,
    user_id       INTEGER NOT NULL,
    wallet        INTEGER NOT NULL DEFAULT 0,
    savings       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, user_id)
);

CREATE TABLE IF NOT EXISTS bot_stats_history (
    snapshot_date TEXT    NOT NULL PRIMARY KEY,
    messages      INTEGER NOT NULL DEFAULT 0,
    commands      INTEGER NOT NULL DEFAULT 0,
    ai_responses  INTEGER NOT NULL DEFAULT 0,
    ai_up         INTEGER NOT NULL DEFAULT 0,
    memory_mb     REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS restart_msg (
    id         INTEGER NOT NULL PRIMARY KEY DEFAULT 1,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ephemeral_msgs (
    id         INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ephemeral_channel ON ephemeral_msgs(channel_id);

CREATE TABLE IF NOT EXISTS command_perms (
    command_name TEXT    NOT NULL PRIMARY KEY,
    tier         TEXT    NOT NULL DEFAULT 'everyone',
    hidden       INTEGER NOT NULL DEFAULT 0
);
