-- 0080: !idle gains hunts, a loot bag, titles, a boost, and the world
-- events and blessings the whole server lives through at once.
--
-- Existing characters start with an empty bag and no titles; the title
-- checks run on the next tick, so anyone already past a threshold is
-- awarded within the minute. hunt_at of 0 reads as "never hunted", which is
-- far enough in the past that a town offers one straight away.
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS loot_json TEXT NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS titles_json TEXT NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS title VARCHAR(32) NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS boost_pct INT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS boost_until BIGINT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS hunt_mob VARCHAR(32) NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS hunt_count INT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS hunt_killed INT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS hunt_x INT NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS hunt_y INT NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS hunt_at BIGINT NOT NULL DEFAULT 0;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS hunts_done INT NOT NULL DEFAULT 0;

-- No primary key: a guild's rows are rewritten whole (delete, then insert
-- the current list), which is what keeps a blessing from needing an id of
-- its own. `cast_by` is the caster of a blessing and NULL for a world event.
CREATE TABLE IF NOT EXISTS idle_guild_events (
    guild_id  BIGINT NOT NULL,
    kind      VARCHAR(32) NOT NULL,
    detail    VARCHAR(64) NOT NULL DEFAULT '',
    cast_by   BIGINT NULL,
    stage     TINYINT NOT NULL DEFAULT 0,
    starts_at BIGINT NOT NULL,
    ends_at   BIGINT NOT NULL,
    INDEX idx_idle_guild_events_guild (guild_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
