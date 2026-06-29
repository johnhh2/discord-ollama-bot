-- 0033: optional bounty expiration + concurrent per-claim tracking.
--
-- Two changes that go together (see src/cogs/bounty_cog.py):
--
-- 1. bounties.expires_at — an OPTIONAL overall deadline for an open bounty,
--    set via `!bounty <coins> <duration> <condition>` (e.g. `!bounty 5k 7d …`).
--    NULL means the bounty never times out. When the deadline passes while the
--    bounty is still open, it auto-closes and refunds the author 90% (the same
--    10% house cut taken on an author self-cancel). A bounty WITH a deadline
--    cannot be self-cancelled by the author — only a real claim or the deadline
--    resolves it.
--
-- 2. bounty_claims — claims are now first-class rows so a single bounty can
--    carry several concurrent claims (each its own author-DM, contest offer,
--    and @everyone poll). The bounty stays 'open' through all of them; the
--    FIRST accepted claim pays out, flips the bounty to 'accepted', and voids
--    every sibling claim (pending DMs ignored, any live poll edited to "void"
--    and skipped at tally). A failed claim (rejected, contest dropped/expired,
--    poll voted no) leaves the bounty open for others. A user gets at most one
--    claim per bounty, ever — enforced by the UNIQUE (bounty_id, claimant_id).
--
-- The legacy per-claim columns on `bounties` (claimant_id, dm_message_id,
-- contest_message_id, poll_message_id, poll_channel_id, claim_expires_at,
-- contest_expires_at, poll_expires_at) from 0031 are left in place but unused
-- by the new code; they're harmless nullable columns. The new `bounties.status`
-- vocabulary is open / accepted / cancelled / expired.

ALTER TABLE bounties
    ADD COLUMN IF NOT EXISTS expires_at DOUBLE NULL;

CREATE TABLE IF NOT EXISTS bounty_claims (
    id                  BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    bounty_id           BIGINT          NOT NULL,
    claimant_id         BIGINT          NOT NULL,
    status              VARCHAR(16)     NOT NULL DEFAULT 'pending',
    dm_message_id       BIGINT,
    contest_message_id  BIGINT,
    poll_message_id     BIGINT,
    poll_channel_id     BIGINT,
    claim_expires_at    DOUBLE,
    contest_expires_at  DOUBLE,
    poll_expires_at     DOUBLE,
    created_at          DOUBLE          NOT NULL,
    -- Portable inline UNIQUE (MariaDB + SQLite test translator both accept this
    -- form; MariaDB's `UNIQUE KEY name (...)` is not SQLite-compatible). One
    -- claim per (bounty, user), ever — a rejected row stays and blocks reclaims.
    UNIQUE (bounty_id, claimant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX IF NOT EXISTS idx_bounty_claims_bounty ON bounty_claims (bounty_id);
CREATE INDEX IF NOT EXISTS idx_bounty_claims_dm ON bounty_claims (dm_message_id);
CREATE INDEX IF NOT EXISTS idx_bounty_claims_contest ON bounty_claims (contest_message_id);
CREATE INDEX IF NOT EXISTS idx_bounty_claims_poll ON bounty_claims (poll_message_id);
