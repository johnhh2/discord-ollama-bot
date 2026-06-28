-- 0031: persistent honor-based bounties (!shop bounty / !bounty).
--
-- A bounty is an escrowed coin reward posted in a guild's configured
-- bounty channel. The author sets an amount (held in escrow, deducted at
-- creation) and a free-text condition. Another user reacts 🙋 to submit a
-- claim; the author is DM'd accept/reject reactions. The full lifecycle —
-- claim submitted, accepted, rejected, contested, polled, expired — is
-- persisted here so the on_raw_reaction_add handler resolves rows after a
-- reboot (Discord re-delivers reaction events but the message cache is empty
-- until a message is touched) and the expiry background loop can settle
-- timed-out claims/contests/polls.
--
-- status values:
--   open      — posted, no active claim; author can self-claim (refund) or a
--               user can submit a claim.
--   pending   — a claim is in flight; author has been DM'd accept/reject.
--               claim_expires_at bounds it (1 week).
--   contesting — author rejected; claimant was asked to contest. Bounded by
--               contest_expires_at (3 days).
--   polling   — claimant contested; an @everyone poll is live in the channel.
--               Bounded by poll_expires_at (3 days), then tallied.
--   accepted  — paid out (terminal).
--   rejected  — rejected & not contested / lost poll / expired (terminal,
--               escrow refunded to author unless a partial poll payout went out).
--   cancelled — author self-claimed before any claim (terminal, refunded).
--
-- Columns shared across the flow:
--   message_id        — the bounty embed in the bounty channel (lookup key).
--   dm_message_id     — the accept/reject DM sent to the author.
--   contest_message_id— the contest-offer DM sent to the rejected claimant.
--   poll_message_id   — the @everyone poll message in the channel.
--   claimant_id       — the user whose claim is in flight / was accepted.

CREATE TABLE IF NOT EXISTS bounties (
    id                  BIGINT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    guild_id            BIGINT          NOT NULL,
    channel_id          BIGINT          NOT NULL,
    message_id          BIGINT          NOT NULL UNIQUE,
    author_id           BIGINT          NOT NULL,
    amount              BIGINT          NOT NULL,
    condition           TEXT            NOT NULL,
    status              VARCHAR(16)     NOT NULL DEFAULT 'open',
    claimant_id         BIGINT,
    dm_message_id       BIGINT,
    contest_message_id  BIGINT,
    poll_message_id     BIGINT,
    poll_channel_id     BIGINT,
    claim_expires_at    DOUBLE,
    contest_expires_at  DOUBLE,
    poll_expires_at     DOUBLE,
    claim_log           JSON,
    created_at          DOUBLE          NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX IF NOT EXISTS idx_bounties_status ON bounties (status);
CREATE INDEX IF NOT EXISTS idx_bounties_dm_message ON bounties (dm_message_id);
CREATE INDEX IF NOT EXISTS idx_bounties_contest_message ON bounties (contest_message_id);
CREATE INDEX IF NOT EXISTS idx_bounties_poll_message ON bounties (poll_message_id);
