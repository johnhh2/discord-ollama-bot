-- Per-user all-time bot-chess stats: the two chess-only ranks shown in
-- !profile and the match embeds (max single Elo defeated, cumulative Elo
-- defeated) plus the set of 1100+ Elo bins whose one-time first-defeat
-- bonus has been claimed (JSON int array).
CREATE TABLE IF NOT EXISTS chess_user_stats (
    user_id            BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    max_elo_defeated   INT             NOT NULL DEFAULT 0,
    total_elo_defeated BIGINT          NOT NULL DEFAULT 0,
    bonus_bins         TEXT            NOT NULL
);

-- Backfill both ranks from game history. In bot games the human always plays
-- White and chess_reports.elo is non-NULL, so a human bot-defeat is exactly
-- (elo IS NOT NULL AND winner_id = white_id). bonus_bins starts empty even
-- for past wins: first-defeat bonuses are earnable going forward only.
-- NOT EXISTS keeps a partial re-run from doubling total_elo_defeated.
INSERT INTO chess_user_stats (user_id, max_elo_defeated, total_elo_defeated, bonus_bins)
SELECT r.white_id, MAX(r.elo), SUM(r.elo), '[]'
FROM chess_reports r
WHERE r.elo IS NOT NULL AND r.winner_id = r.white_id
  AND NOT EXISTS (SELECT 1 FROM chess_user_stats s WHERE s.user_id = r.white_id)
GROUP BY r.white_id;
