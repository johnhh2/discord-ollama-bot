-- 0047: make the "longest daily command streak" record consistent across
-- servers. Same bug and same fix as 0046 — see that file's header for the
-- leveling-row membership proxy and the raise-only guarantee.
--
-- The streak differs from the artifact count in one way: it is a high-water
-- mark over a value that can drop. `command_streak.streak_count` resets to 1
-- the day you miss, so the live column is NOT authoritative for the record.
-- The candidate is therefore the greatest of (every value already recorded
-- for that user in any guild, their current live streak) — the live streak is
-- in the mix only to catch a streak that was bumped outside a guild and so
-- never reached the records table.
--
-- Unlike 0046 this honours UID_TIEBREAK_CATEGORIES: `command_streak` breaks
-- ties on the lower user id, matching `_beats` in src/persistence/records.py,
-- so a tie can displace a higher-uid incumbent.

INSERT INTO records (guild_id, category, value, holder_id, holder_name)
SELECT guild_id, 'command_streak', value, holder_id, holder_name
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
                        SELECT holder_id, MAX(value) AS value
                          FROM (
                                SELECT holder_id, value
                                  FROM records
                                 WHERE category = 'command_streak'
                                 UNION ALL
                                SELECT user_id, streak_count
                                  FROM command_streak
                               ) u
                         GROUP BY holder_id
                       ) t
               ) c
          JOIN leveling l ON l.user_id = c.holder_id
         WHERE c.holder_name IS NOT NULL
       ) ranked
 WHERE rn = 1
    ON DUPLICATE KEY UPDATE
       holder_name = CASE WHEN VALUES(value) > records.value
                            OR (VALUES(value) = records.value
                                AND VALUES(holder_id) < records.holder_id)
                          THEN VALUES(holder_name) ELSE records.holder_name END,
       holder_id   = CASE WHEN VALUES(value) > records.value
                            OR (VALUES(value) = records.value
                                AND VALUES(holder_id) < records.holder_id)
                          THEN VALUES(holder_id)   ELSE records.holder_id   END,
       value       = GREATEST(records.value, VALUES(value));
