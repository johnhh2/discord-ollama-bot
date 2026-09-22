-- 0078: !idle hit points. A character carries its wounds between monster
-- fights now, so a fight costs health rather than clock. Existing characters
-- start whole; the true maximum also counts their gear, and the tick's
-- regeneration tops them up within the minute either way.
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS hp INT NOT NULL DEFAULT 100;

UPDATE idle_characters SET hp = 100 + 5 * level;
