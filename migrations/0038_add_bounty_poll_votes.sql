-- 0038: track bounty contest-poll votes as they are cast.
--
-- bounty_claims.poll_votes — JSON `{"yes": [user ids], "no": [user ids]}`
-- maintained by on_raw_reaction_add/remove while a claim is `polling`. The
-- poll embed renders a live tally from this column, and the close-time tally
-- reads it instead of trusting the message's reactions to still be accurate
-- (reactions can be cleared by a moderator or lost with a deleted message;
-- a reaction scan at close remains only as catch-up for votes cast while the
-- bot was offline). NULL for claims that never reached a poll.

ALTER TABLE bounty_claims
    ADD COLUMN IF NOT EXISTS poll_votes TEXT NULL;
