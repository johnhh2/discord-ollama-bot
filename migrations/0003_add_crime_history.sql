-- 0003: per-user daily totals of coins gained/lost via !steal and !mug,
-- powering !graph crime. Updated by the 6h snapshot loop and the 5am
-- gameplay-day final snapshot.

CREATE TABLE IF NOT EXISTS crime_history (
    snapshot_date VARCHAR(10)     NOT NULL,
    user_id       BIGINT UNSIGNED NOT NULL,
    gained        BIGINT          NOT NULL DEFAULT 0,
    lost          BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
