-- 0004: per-user daily totals of coins gained/lost via gambling commands
-- (slots, flip, blackjack, scratchoff) and games with payouts/wagers
-- (hangman, ttt, c4, race, lottery). Powers !graph gambling.

CREATE TABLE IF NOT EXISTS gambling_history (
    snapshot_date VARCHAR(10)     NOT NULL,
    user_id       BIGINT UNSIGNED NOT NULL,
    gained        BIGINT          NOT NULL DEFAULT 0,
    lost          BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_date, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
