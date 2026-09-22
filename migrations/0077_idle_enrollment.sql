-- 0077: !idle automatic enrollment. An enrolled character is unclaimed until
-- its player runs `!idle join <class>`; every character from before this is
-- a player's own. idle_optouts lists who walked away from one, so the
-- enrollment sweep doesn't hand them another a minute later.
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS claimed TINYINT(1) NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS idle_optouts (
    guild_id BIGINT UNSIGNED NOT NULL,
    user_id  BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- A fresh start for everyone as enrollment opens: progress goes, identity
-- stays (class, feed thread, alignment, claimed, auto_trade, created_at).
-- The clock is left paused with a full level-0 wait rather than computed
-- from the current time — the tick resumes a paused character the moment its
-- player is seen online, so nobody's first level runs while they are away.
-- Positions are cleared and re-rolled on the next tick. The wait is spread
-- over 10-20 minutes by user id: given the same clock, everyone online at
-- the deploy would level — and roll their battles — in the same minute, for
-- good. (A modulo, not RAND(): it has to mean the same thing in the tests'
-- SQLite.)
UPDATE idle_characters SET
    level = 0, prestige = 0, next_level_at = NULL, remaining = 600 + (user_id % 600),
    items_json = '{}', gold = 0, penalty_total = 0, last_penalty_at = 0,
    x = NULL, y = NULL, travel_to = NULL,
    duel_day = NULL, rush_day = NULL, extra_duel_day = NULL, align_changed_at = 0, traded_at = 0,
    mob_kills = 0, mob_deaths = 0,
    gamble_town = NULL, gamble_visit_at = 0, gamble_budget = 0, gambles = 0, gamble_won = 0, gamble_lost = 0;

DELETE FROM idle_quests;
