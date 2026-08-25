-- 0046: make the "most artifacts owned" record consistent across servers.
--
-- Artifacts are global per user (user_artifacts is keyed by user alone), but
-- the record was only ever written into the guild the purchase happened in.
-- A user in two servers who bought everything in server A left server B
-- showing a stale, lower total. src/persistence/records.py now mirrors the
-- write into the holder's other guilds; this backfills the rows that were
-- already written wrong.
--
-- SQL has no view of Discord membership, so "active in a guild" is proxied by
-- a `leveling` row — grant_xp materializes one on a user's first command or
-- message in a guild, before any rate-limit early-return. This is the same
-- proxy `_mirror_guilds` uses at runtime, so the two agree on who counts.
--
-- Names come from whatever holder_name that user already has on a records row
-- (MAX() picks one deterministically when they differ); a user who has never
-- held any record anywhere has no name to write and is skipped. The runtime
-- mirror covers them on their next purchase.
--
-- Idempotent and raise-only: every write is guarded on the new value being
-- strictly greater, so a re-run is a no-op and no record is ever lowered.

-- 1. Raise rows whose holder is already right but whose count went stale.
UPDATE records
   SET value = (SELECT COALESCE(SUM(ua.quantity), 0)
                  FROM user_artifacts ua
                 WHERE ua.user_id = records.holder_id)
 WHERE category = 'total_artifacts'
   AND value < (SELECT COALESCE(SUM(ua.quantity), 0)
                  FROM user_artifacts ua
                 WHERE ua.user_id = records.holder_id);

-- 2. Take the record in guilds where an active user's true count beats the
--    row sitting there (or where no row exists yet). ROW_NUMBER picks a
--    single candidate per guild — biggest count, lowest user id on a tie —
--    so the upsert can't depend on row order when two users both beat it.
INSERT INTO records (guild_id, category, value, holder_id, holder_name)
SELECT guild_id, 'total_artifacts', value, holder_id, holder_name
  FROM (
        SELECT l.guild_id    AS guild_id,
               c.value       AS value,
               c.holder_id   AS holder_id,
               c.holder_name AS holder_name,
               ROW_NUMBER() OVER (PARTITION BY l.guild_id
                                  ORDER BY c.value DESC, c.holder_id ASC) AS rn
          FROM (
                SELECT t.holder_id AS holder_id,
                       t.value     AS value,
                       (SELECT MAX(r.holder_name)
                          FROM records r
                         WHERE r.holder_id = t.holder_id
                           AND r.holder_name IS NOT NULL) AS holder_name
                  FROM (
                        SELECT user_id AS holder_id, SUM(quantity) AS value
                          FROM user_artifacts
                         GROUP BY user_id
                       ) t
               ) c
          JOIN leveling l ON l.user_id = c.holder_id
         WHERE c.holder_name IS NOT NULL
       ) ranked
 WHERE rn = 1
    ON DUPLICATE KEY UPDATE
       holder_name = CASE WHEN VALUES(value) > records.value
                          THEN VALUES(holder_name) ELSE records.holder_name END,
       holder_id   = CASE WHEN VALUES(value) > records.value
                          THEN VALUES(holder_id)   ELSE records.holder_id   END,
       value       = GREATEST(records.value, VALUES(value));
