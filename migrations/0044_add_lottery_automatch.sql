-- 0044: lottery automatch. Users opt in with `!lottery automatch <max>`;
-- whenever another player's ticket total passes theirs, the bot auto-buys
-- tickets on their behalf to tie that total, never raising their own count
-- above max_tickets. Rows last one lottery: cleared with
-- `!lottery automatch off`, or wiped for the whole guild when the monthly
-- draw resets the pool.
CREATE TABLE IF NOT EXISTS lottery_automatch (
    guild_id    BIGINT UNSIGNED NOT NULL,
    user_id     BIGINT UNSIGNED NOT NULL,
    max_tickets INT             NOT NULL,
    PRIMARY KEY (guild_id, user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
