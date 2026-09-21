-- 0072: one-off wipe. When this shipped the only !idle data anywhere was the
-- owner's test character, made before the map and the quieter level
-- announcements landed; the game starts clean. Runs once (checksummed like
-- every migration) and is a no-op on a fresh database.
DELETE FROM idle_characters;
DELETE FROM idle_quests;
