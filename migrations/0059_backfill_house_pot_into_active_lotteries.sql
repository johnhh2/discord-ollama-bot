-- 0059: backfill guild house pots into the active September 2026 lotteries.
--
-- Every fresh lottery is supposed to start at LOTTERY_SEED_POOL + that
-- guild's house pot (drain_bot_balance_into_lottery), but the lottery
-- scheduler had no before_loop gate on init_db_state: a tick landing after
-- the gateway was ready but before the DB load finished saw an empty
-- in-memory guild_house and drained 0, while the DB row kept its coins.
-- The 9/1/2026 6pm CT draws hit that window, so the September pools started
-- at the bare seed. The scheduler is gated now (src/cogs/lottery_cog.py);
-- this moves the stranded pots into the lotteries that started without them.
--
-- Scope: only lotteries running the September 2026 cycle — drawn and/or
-- announced under month key 202609. Guilds whose lottery is disabled or
-- still on an older cycle keep their house pot; the (fixed) drain moves it
-- when their next lottery actually starts. On any DB with no 202609 rows
-- (fresh installs, or a deploy after the October draw) this is a no-op.
--
-- Re-running the whole file is a no-op: after the first pass the in-scope
-- house balances are 0, so the add contributes nothing. The one unsafe
-- retry is a crash exactly between the two statements (the pool is
-- autocommit) — a rerun then adds the pot twice, so verify prize_pool
-- before rebooting a half-applied 0059.

UPDATE lottery
   SET prize_pool = prize_pool + COALESCE((SELECT g.balance
                                             FROM guild_house_balance g
                                            WHERE g.guild_id = lottery.guild_id), 0)
 WHERE last_drawn_week = 202609
    OR last_posted_week = 202609;

UPDATE guild_house_balance
   SET balance = 0
 WHERE guild_id IN (SELECT guild_id
                      FROM lottery
                     WHERE last_drawn_week = 202609
                        OR last_posted_week = 202609);
