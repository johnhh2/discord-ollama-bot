-- 0063: chess lottery tickets go monthly. The free chess-win ticket ceiling
-- (2 for beating a 600+ Elo bot, global per user) used to reset every ISO
-- week; it now resets with the lottery itself — the window opens at the
-- 1st-of-month 6pm CT draw and runs until the next one (lottery_period_key
-- in src/economy.py). chess_week becomes chess_period and holds the
-- "YYYY-MM" key of the month the lottery opened in, instead of a "YYYY-Www"
-- ISO week. CHANGE COLUMN IF EXISTS renames in place (data kept) and is a
-- no-op once the column is gone.
--
-- Data: free grants only started with the 9/1/2026 6pm CT relaunch
-- (TICKET_SALES_START_CT), so every existing week key belongs to the
-- September 2026 lottery — fold them into its period so tickets already
-- claimed keep counting against the ceiling. Deployed after the October
-- draw instead, those rows are simply stale (a fresh ceiling), which is what
-- an unconverted week key would have meant anyway. Re-running is a no-op:
-- no "YYYY-Www" keys remain after the first pass.
ALTER TABLE lottery_ticket_grants CHANGE COLUMN IF EXISTS chess_week chess_period VARCHAR(10) NULL;

UPDATE lottery_ticket_grants
   SET chess_period = '2026-09'
 WHERE chess_period LIKE '2026-W%';
