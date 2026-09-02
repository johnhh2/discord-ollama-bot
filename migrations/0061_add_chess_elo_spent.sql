-- Spendable-Elo accounting for the chess shop: a user's spendable balance is
-- total_elo_defeated - elo_spent. total_elo_defeated stays a monotonic
-- lifetime stat (profile ranks, records) and is never decremented; unlock
-- purchases only ever raise elo_spent.
ALTER TABLE chess_user_stats
    ADD COLUMN IF NOT EXISTS elo_spent BIGINT NOT NULL DEFAULT 0;
