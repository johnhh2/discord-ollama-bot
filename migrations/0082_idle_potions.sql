-- 0082: !idle health potions — a count on the character, bought at a
-- market and drunk by the fight loop where a camp would otherwise be made.
-- Existing characters start with none; the next town errand stocks up.
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS potions INT NOT NULL DEFAULT 0;
