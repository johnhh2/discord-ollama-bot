"""Dailies-channel keep list.

The dailies channel (src/cogs/dailies_cog.py) deletes every non-claim message
after 5 minutes. Big gambling results are the exception: any scratchoff, flip,
or slots result posted there where DAILIES_KEEP_MIN+ coins were won or lost is
recorded in the guild's cfg["dailies_keep_ids"] and survives until the 5am CT
reset repost purges the channel.

Lives in its own module so the gambling modules (scratchoff, flip, slots) and
the dailies cog can all import it without cycles — this module only touches
state and persistence.
"""
from src import state
from src.persistence import save_guild_settings

# Results at/above this net win/loss stay up until the reset.
DAILIES_KEEP_MIN = 10_000


async def keep_in_dailies_channel(guild, channel, message, delta: int) -> None:
    """Exempt a big gambling result from the dailies channel's 5-minute sweep.

    No-op unless `message` was posted in `guild`'s configured dailies channel
    and |delta| (the net coins won or lost) is at least DAILIES_KEEP_MIN.
    `message` may be None (callers pass through whatever channel.send returned).
    """
    if guild is None or message is None or abs(delta) < DAILIES_KEEP_MIN:
        return
    cfg = state.guild_settings.get(str(guild.id))
    if not cfg or channel.id != cfg.get("dailies_channel"):
        return
    cfg.setdefault("dailies_keep_ids", []).append(message.id)
    await save_guild_settings()
