-- 0003: per-user daily activity tables powering the !graph crime,
-- !graph gambling, and !graph levels subcommands.
--
-- Bundled in one migration because all three landed in the same dev cycle
-- before any deploy — splitting them would just be migration-file noise.

-- Coins gained/lost via !steal and !mug.
CREATE TABLE IF NOT EXISTS crime_history (
    snapshot_date VARCHAR(10)     NOT NULL,
    user_id       BIGINT UNSIGNED NOT NULL,
    gained        BIGINT          NOT NULL DEFAULT 0,
    lost          BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Net P/L from gambling (slots, flip, blackjack, scratchoff) and games
-- with payouts/wagers (hangman, ttt, c4, race, lottery).
CREATE TABLE IF NOT EXISTS gambling_history (
    snapshot_date VARCHAR(10)     NOT NULL,
    user_id       BIGINT UNSIGNED NOT NULL,
    gained        BIGINT          NOT NULL DEFAULT 0,
    lost          BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Per-(guild, user) daily count of level-ups. A single XP grant can cross
-- multiple level thresholds; the count column records boundaries crossed,
-- not events triggered.
CREATE TABLE IF NOT EXISTS levelup_history (
    snapshot_date VARCHAR(10)     NOT NULL,
    guild_id      BIGINT UNSIGNED NOT NULL,
    user_id       BIGINT UNSIGNED NOT NULL,
    count         INT             NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, guild_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
