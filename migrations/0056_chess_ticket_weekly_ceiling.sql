-- 0056: chess lottery tickets switch from two per-tier weekly gates
-- (chess_week_500/chess_week_1100) to a cumulative weekly ceiling: any chess
-- win (PvP included) is worth 1 ticket, beating a 600+ Elo bot 2, a 1100+
-- bot 3 — each win tops the winner up to its ceiling, never past 3/week.
-- chess_week holds the ISO week; chess_tickets counts what's been granted in
-- it. The old columns are dropped without a data copy: grants are still
-- closed by the TICKET_SALES_START_CT launch gate (9/1/2026), so no live
-- claims exist to carry over.
ALTER TABLE lottery_ticket_grants ADD COLUMN IF NOT EXISTS chess_week VARCHAR(10) NULL;
ALTER TABLE lottery_ticket_grants ADD COLUMN IF NOT EXISTS chess_tickets INT NOT NULL DEFAULT 0;
ALTER TABLE lottery_ticket_grants DROP COLUMN IF EXISTS chess_week_500;
ALTER TABLE lottery_ticket_grants DROP COLUMN IF EXISTS chess_week_1100;
