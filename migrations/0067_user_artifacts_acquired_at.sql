-- 0067: when each artifact was bought.
--
-- The savings-rate artifact (src/artifacts.py) raises a piggy bank's daily
-- interest from the moment it's bought — never retroactively, or a 200k
-- purchase would re-price every day of interest already earned. That needs
-- the acquisition time, which the table never stored. Written by
-- save_user_artifact on the first insert only; rows from before this
-- migration keep NULL (unknown), and no shipped artifact from that era reads
-- it.
ALTER TABLE user_artifacts ADD COLUMN IF NOT EXISTS acquired_at DOUBLE NULL;
