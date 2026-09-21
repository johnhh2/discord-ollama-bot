-- 0071: the !idle map. Characters wander a 0..500 square grid; a NULL
-- position is a character from before the map, placed at random on the next
-- tick (a migration has no per-row random that the test translator shares).
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS x INT NULL;
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS y INT NULL;

-- kind is 'vigil' (timed, ends_at set) or 'journey' (walk to p1, then p2;
-- ends_at NULL), and NULL while no quest runs. A pre-0071 row with ends_at
-- set is a vigil; the loader reads it that way.
ALTER TABLE idle_quests ADD COLUMN IF NOT EXISTS kind VARCHAR(8) NULL;
ALTER TABLE idle_quests ADD COLUMN IF NOT EXISTS stage INT NOT NULL DEFAULT 1;
ALTER TABLE idle_quests ADD COLUMN IF NOT EXISTS p1x INT NULL;
ALTER TABLE idle_quests ADD COLUMN IF NOT EXISTS p1y INT NULL;
ALTER TABLE idle_quests ADD COLUMN IF NOT EXISTS p2x INT NULL;
ALTER TABLE idle_quests ADD COLUMN IF NOT EXISTS p2y INT NULL;
