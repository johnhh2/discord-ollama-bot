-- 0064: !session gambling threads. One row per open thread (the public
-- thread `!session` opens off a game channel), so a reboot still knows which
-- threads are gambling-only: the command allowlist gate and `!stop`'s
-- thread close both read src.state.gambling_threads, loaded from here at
-- boot. The row goes when the owner closes the table with `!stop`, or when
-- the thread is archived or deleted. tally_json is the table's running
-- P/L per player ({"<uid>": {"net": int, "name": str|null}}) — the thread
-- names itself after the biggest winner (or loser), and the tally survives
-- a reboot so the name doesn't restart from zero mid-session. Written by
-- src/persistence/gambling_threads.py; loaded by src/persistence/init.py.
CREATE TABLE IF NOT EXISTS gambling_threads (
    thread_id  BIGINT UNSIGNED NOT NULL PRIMARY KEY,
    owner_id   BIGINT UNSIGNED NOT NULL,
    guild_id   BIGINT UNSIGNED NOT NULL,
    parent_id  BIGINT UNSIGNED NOT NULL,
    created_at BIGINT          NOT NULL,
    tally_json JSON            NOT NULL
);
