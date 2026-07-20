"""Dailies channel: a self-cleaning channel with a single 'Claim your dailies'
embed. Reacting 🪙 to the embed immediately runs the player's dailies (daily
coin reward + all remaining scratchoffs) right there in the channel.

Configured per-guild with `!settings dailies-channel #channel` — the channel id,
claim-message id, and last-reset day all live in the guild_settings JSON blob
(no dedicated table/migration needed).

Cleanliness rules:
- On boot (and whenever the setting is applied) the channel is purged of
  everything except the claim embed.
- Any other message posted in the channel — user chatter, command invocations,
  claim results, this bot's own sends — is deleted after 5 minutes.
- At the 5am CT daily reset the claim embed is reposted, which clears all
  claim reactions so players can click again. The repost doubles as a purge.
"""
import asyncio
import logging

import discord
from discord.ext import commands, tasks

from src.helpers import emb, C_GOLD, fetch_member, _delete_after
from src.economy import _ct_today, next_daily_reset_ts
from src.persistence import save_guild_settings
from src.guild_config import get_guild_cfg
from src.gambling.scratchoff import play_scratchoffs
from src.artifacts import scratchoff_daily_cap
from src.events import _auto_daily
from src import state


DAILIES_EMOJI = "🪙"
DAILIES_TITLE = "🪙 Claim your dailies"
# How long non-claim messages (results, user chatter) survive in the channel.
DAILIES_MESSAGE_TTL = 300.0


def _dailies_body() -> str:
    return (
        f"React with {DAILIES_EMOJI} below to claim your daily reward and "
        "instantly use all of your daily scratchoffs.\n\n"
        "Results (and any other messages here) are cleared after 5 minutes.\n"
        f"Dailies reset <t:{next_daily_reset_ts()}:R>."
    )


async def refresh_dailies_channel(bot, guild_id: int):
    """Bring a guild's dailies channel to its canonical state.

    Purges everything except the current claim embed; if the claim embed is
    missing or stale (a new gameplay-day has started since it was posted), the
    old one is purged too and a fresh embed + 🪙 reaction is posted — which is
    how claim reactions get reset at 5am CT. Idempotent and safe to call from
    boot, the minute loop, and the settings command. No-op when the guild has
    no dailies channel configured.
    """
    cfg = get_guild_cfg(guild_id)
    ch_id = cfg.get("dailies_channel")
    if not ch_id:
        return
    try:
        channel = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logging.warning("[dailies] guild=%s channel %s unavailable", guild_id, ch_id)
        return

    today = _ct_today()
    claim_msg = None
    msg_id = cfg.get("dailies_message_id")
    if msg_id and cfg.get("dailies_reset_day") == today:
        try:
            claim_msg = await channel.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            claim_msg = None

    # Purge everything except the claim embed we're keeping. A stale/missing
    # claim embed (keep_id None) means the whole channel is cleared.
    keep_id = claim_msg.id if claim_msg is not None else None
    try:
        await channel.purge(limit=None, check=lambda m: m.id != keep_id, bulk=True)
    except (discord.Forbidden, discord.HTTPException):
        logging.warning("[dailies] guild=%s purge failed (missing Manage Messages?)", guild_id)

    if claim_msg is None:
        try:
            claim_msg = await channel.send(embed=emb(DAILIES_TITLE, _dailies_body(), C_GOLD))
            await claim_msg.add_reaction(DAILIES_EMOJI)
        except (discord.Forbidden, discord.HTTPException):
            logging.warning("[dailies] guild=%s failed to post claim embed", guild_id)
            return
        cfg["dailies_message_id"] = claim_msg.id

    cfg["dailies_reset_day"] = today
    await save_guild_settings()


class DailiesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Serializes refreshes so a boot sweep and the minute loop can't both
        # repost the claim embed for the same guild.
        self._refresh_lock = asyncio.Lock()
        self._reset_task.start()

    def cog_unload(self):
        self._reset_task.cancel()

    async def _refresh_all(self):
        async with self._refresh_lock:
            for gid_str, cfg in list(state.guild_settings.items()):
                if not cfg.get("dailies_channel"):
                    continue
                try:
                    await refresh_dailies_channel(self.bot, int(gid_str))
                except Exception:
                    logging.exception("[dailies] refresh failed for guild %s", gid_str)

    @commands.Cog.listener()
    async def on_ready(self):
        # Boot sweep: clears anything that accumulated while offline and
        # reposts the claim embed if a reset was missed. on_ready re-fires on
        # gateway reconnects; the refresh is idempotent so that's fine.
        import src.persistence as _pkg
        await _pkg.init_done.wait()
        await self._refresh_all()

    @tasks.loop(minutes=1)
    async def _reset_task(self):
        """Repost the claim embed when the 5am CT gameplay-day rolls over,
        clearing claim reactions so everyone can click again. Guarded — a
        failed tick must not stop the loop for good."""
        try:
            today = _ct_today()
            stale = [
                int(gid_str)
                for gid_str, cfg in state.guild_settings.items()
                if cfg.get("dailies_channel") and cfg.get("dailies_reset_day") != today
            ]
            if not stale:
                return
            async with self._refresh_lock:
                for gid in stale:
                    try:
                        await refresh_dailies_channel(self.bot, gid)
                    except Exception:
                        logging.exception("[dailies] reset failed for guild %s", gid)
        except Exception:
            logging.exception("[dailies] reset tick failed")

    @_reset_task.before_loop
    async def _before_reset_task(self):
        await self.bot.wait_until_ready()
        import src.persistence as _pkg
        await _pkg.init_done.wait()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        if payload.guild_id is None:
            return
        cfg = state.guild_settings.get(str(payload.guild_id))
        if not cfg or payload.message_id != cfg.get("dailies_message_id"):
            return
        if str(payload.emoji) != DAILIES_EMOJI:
            return
        # Mirror the on_message blocklist silence for banned users.
        if payload.user_id in state.global_blocklist:
            return
        if (payload.guild_id, payload.user_id) in state.blocklist:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member or await fetch_member(guild, payload.user_id)
        if member is None or member.bot:
            return
        try:
            channel = self.bot.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        await self._run_dailies(member, channel, guild)

    async def _run_dailies(self, member, channel, guild):
        """Run all of the member's dailies in `channel`.

        Extension point: future daily claims go here. Every step is already
        idempotent per gameplay-day (each has its own daily gate), so a second
        click just reports the daily limit. Result messages need no explicit
        cleanup — on_message below schedules deletion for everything posted in
        the dailies channel.
        """
        await _auto_daily(member, channel)
        await play_scratchoffs(self.bot, member, channel, guild, count=scratchoff_daily_cap(member.id))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Schedule deletion of every non-claim message in a dailies channel —
        claim results, normal user chatter, command replies, and this bot's
        own sends alike."""
        if message.guild is None:
            return
        cfg = state.guild_settings.get(str(message.guild.id))
        if not cfg or message.channel.id != cfg.get("dailies_channel"):
            return
        if message.id == cfg.get("dailies_message_id"):
            return
        asyncio.create_task(_delete_after(message, DAILIES_MESSAGE_TTL))


async def setup(bot):
    await bot.add_cog(DailiesCog(bot))
