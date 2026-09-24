-- 0081: the Minecraft block shop (!mc shop): Discord ↔ gamertag links, the
-- 🟫 block purse, and a trade log.
--
-- Blocks are the shop's own currency, earned only by selling items to it,
-- and bot-wide like coins: one Minecraft server, one purse per user. The
-- balance is kept in tenths of a block so the guide's half-block prices
-- (2.5 for a coal) stay exact integers. A gamertag belongs to one Discord
-- user at a time; unlinking keeps the purse. The trade log is an audit
-- trail for the operator (every give/clear the bot ran on the console),
-- never read by the bot.
CREATE TABLE IF NOT EXISTS mc_players (
    user_id   BIGINT NOT NULL,
    gamertag  VARCHAR(32) NULL,
    blocks    BIGINT NOT NULL DEFAULT 0,
    linked_at BIGINT NULL,
    PRIMARY KEY (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE UNIQUE INDEX IF NOT EXISTS uq_mc_players_gamertag ON mc_players (gamertag);

CREATE TABLE IF NOT EXISTS mc_trades (
    id       BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ts       BIGINT NOT NULL,
    user_id  BIGINT NOT NULL,
    guild_id BIGINT NULL,
    gamertag VARCHAR(32) NOT NULL,
    kind     VARCHAR(4) NOT NULL,
    item_id  VARCHAR(64) NOT NULL,
    count    INT NOT NULL,
    blocks   BIGINT NOT NULL,
    INDEX idx_mc_trades_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
