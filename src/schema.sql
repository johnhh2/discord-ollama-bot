-- discord-ollama-bot schema
-- Run once against MariaDB to create all tables.

CREATE TABLE IF NOT EXISTS economy_users (
    user_id        BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    balance        BIGINT          NOT NULL DEFAULT 0,
    last_daily     DOUBLE          NOT NULL DEFAULT 0,
    daily_date     VARCHAR(10)     NULL,
    scratch_used   TINYINT         NOT NULL DEFAULT 0,
    scratch_date   VARCHAR(10)     NULL,
    jailbreak_used BOOLEAN         NOT NULL DEFAULT FALSE,
    jail_until     DOUBLE          NOT NULL DEFAULT 0,
    savings        JSON            NOT NULL DEFAULT (JSON_ARRAY())
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS economy_meta (
    key_name   VARCHAR(64) NOT NULL PRIMARY KEY,
    value_text TEXT        NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS guild_house_balance (
    guild_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    balance  BIGINT          NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id      BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    settings_json JSON            NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS bot_roles (
    role_id BIGINT UNSIGNED NOT NULL PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS godmode_users (
    user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS bot_settings (
    key_name   VARCHAR(64) NOT NULL PRIMARY KEY,
    value_text TEXT        NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shop_insurance (
    user_id        BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    expires_at     DOUBLE          NOT NULL,
    protected_from JSON            NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS shop_effects (
    user_id      BIGINT UNSIGNED                           NOT NULL,
    effect_type  ENUM('ragebait','mock','curse','tax')     NOT NULL,
    remaining    INT          NULL,
    started_by   BIGINT UNSIGNED NULL,
    cursed_by    BIGINT UNSIGNED NULL,
    history_json JSON         NULL,
    master_id    BIGINT UNSIGNED NULL,
    tax_type     VARCHAR(64)  NULL,
    tax_emoji    VARCHAR(16)  NULL,
    channel_id   BIGINT UNSIGNED NULL,
    activated_at DOUBLE       NULL,
    PRIMARY KEY (user_id, effect_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rigged_slots (
    user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    symbol  VARCHAR(16)     NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rigged_flips (
    user_id        BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    remaining_wins INT             NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rigged_scratch (
    user_id       BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    symbols_count INT             NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rigged_steal (
    user_id              BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    remaining_successes  INT             NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS slots_jackpot (
    id      TINYINT NOT NULL PRIMARY KEY DEFAULT 1,
    jackpot BIGINT  NOT NULL DEFAULT 5000
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT IGNORE INTO slots_jackpot (id, jackpot) VALUES (1, 5000);

CREATE TABLE IF NOT EXISTS lottery (
    guild_id         BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    prize_pool       BIGINT          NOT NULL DEFAULT 0,
    last_posted_week INT             NOT NULL DEFAULT 0,
    last_drawn_week  INT             NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS lottery_players (
    guild_id BIGINT UNSIGNED NOT NULL,
    user_id  BIGINT UNSIGNED NOT NULL,
    tickets  INT             NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS records (
    guild_id     BIGINT UNSIGNED NOT NULL,
    category     VARCHAR(64)     NOT NULL,
    value        BIGINT          NOT NULL,
    holder_id    BIGINT UNSIGNED NOT NULL,
    holder_name  VARCHAR(255)    NOT NULL,
    extra_json   JSON            NULL,
    PRIMARY KEY (guild_id, category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS channel_prompts (
    channel_id  BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    prompt_text TEXT            NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chess_games (
    channel_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    game_json  JSON            NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ai_threads (
    thread_id         BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    kind              ENUM('ask','fanfic','roleplay','rpg') NOT NULL,
    owner_id          BIGINT UNSIGNED NOT NULL,
    guild_id          BIGINT UNSIGNED NULL,
    invited_ids_json  JSON            NOT NULL,
    system_prompt     TEXT            NULL,
    character_prompt  TEXT            NULL,
    history_json      JSON            NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS leveling (
    guild_id BIGINT UNSIGNED NOT NULL,
    user_id  BIGINT UNSIGNED NOT NULL,
    data     JSON            NOT NULL,
    PRIMARY KEY (guild_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gambler_streak (
    user_id        BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    last_full_date VARCHAR(10)     NOT NULL,
    streak_count   INT             NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS quote_log (
    id         INT UNSIGNED    NOT NULL AUTO_INCREMENT PRIMARY KEY,
    content    TEXT            NOT NULL,
    created_at TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS saved_quotes (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    guild_id   VARCHAR(20)     NOT NULL,
    quote_json JSON            NOT NULL,
    INDEX idx_guild (guild_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS balance_history (
    snapshot_date VARCHAR(10)     NOT NULL,
    user_id       BIGINT UNSIGNED NOT NULL,
    wallet        BIGINT          NOT NULL DEFAULT 0,
    savings       BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS bot_stats_history (
    snapshot_date VARCHAR(10) NOT NULL PRIMARY KEY,
    messages      INT         NOT NULL DEFAULT 0,
    commands      INT         NOT NULL DEFAULT 0,
    ai_responses  INT         NOT NULL DEFAULT 0,
    ai_up         BOOLEAN     NOT NULL DEFAULT FALSE,
    memory_mb     FLOAT       NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS restart_msg (
    id         TINYINT         NOT NULL PRIMARY KEY DEFAULT 1,
    channel_id BIGINT UNSIGNED NOT NULL,
    message_id BIGINT UNSIGNED NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ephemeral_msgs (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    channel_id BIGINT UNSIGNED NOT NULL,
    message_id BIGINT UNSIGNED NOT NULL,
    INDEX idx_channel (channel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS command_perms (
    command_name VARCHAR(64)                                         NOT NULL PRIMARY KEY,
    tier         ENUM('everyone','server_admin','bot_admin')         NOT NULL DEFAULT 'everyone',
    hidden       BOOLEAN                                             NOT NULL DEFAULT FALSE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
