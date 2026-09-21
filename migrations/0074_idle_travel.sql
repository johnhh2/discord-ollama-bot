-- 0074: !idle travel. The town a character is walking toward (a key of
-- idlerpg.TOWNS), NULL while it wanders.
ALTER TABLE idle_characters ADD COLUMN IF NOT EXISTS travel_to VARCHAR(32) NULL;
