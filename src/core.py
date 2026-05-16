import asyncio
import logging
import signal
import time

import discord
from discord.ext import commands

from src.config import DISCORD_TOKEN

# Login-time 429 backoff. discord.py's internal 5-retry-then-raise is fine for
# normal usage but turns container restart policies into a tight crash-loop
# during Discord-wide outages: every fresh process resets the backoff clock,
# so a `restart: unless-stopped` policy keeps slamming /users/@me. Sleeping
# in-process keeps the container alive and lets the throttle clear naturally.
LOGIN_429_BACKOFFS_SECS = (60, 300, 900, 1800, 3600)  # 1m, 5m, 15m, 30m, 1h

# Wall-clock budget to drain in-flight AI streams before closing the DB pool.
# Kept under most container SIGKILL timeouts (Docker default 10s, k8s 30s).
SHUTDOWN_DRAIN_SECONDS = 8.0

EXTENSIONS = [
    "src.games.blackjack",
    "src.games.hangman",
    "src.games.ttt_c4",
    "src.games.chess",
    "src.games.race",
    "src.gambling.flip",
    "src.gambling.scratchoff",
    "src.gambling.slots",
    "src.cogs.economy_cog",
    "src.cogs.shop_cog",
    "src.cogs.settings_cog",
    "src.cogs.moderation_cog",
    "src.cogs.admin_cog",
    "src.cogs.ai_cog",
    "src.cogs.utility_cog",
    "src.cogs.fun_cog",
    "src.cogs.lottery_cog",
    "src.cogs.leveling_cog",
    "src.cogs.graph_cog",
    "src.cogs.voice_cog",
    "src.events",
]


class SilentContext(commands.Context):
    """ctx.send defaults to silent=True so command replies don't push-notify
    the invoker. The user just typed the command — they're already watching
    the channel. Callers that genuinely need to ping (game-turn boards,
    error embeds the user shouldn't miss) pass silent=False explicitly.

    channel.send / member.send / user.send are unaffected (different code
    paths), so proactive announcements and DMs naturally stay loud.
    """
    async def send(self, content=None, **kwargs):
        kwargs.setdefault("silent", True)
        return await super().send(content, **kwargs)


class Bot(commands.Bot):
    _shutdown_done: bool = False

    async def get_context(self, message, *, cls=SilentContext):
        return await super().get_context(message, cls=cls)

    async def process_commands(self, message: discord.Message) -> None:
        """Allow other bots to invoke commands (default implementation skips bots)."""
        if message.author == self.user:
            return
        ctx = await self.get_context(message)
        await self.invoke(ctx)  # type: ignore

    async def setup_hook(self) -> None:
        # Load cogs here, not in on_connect. on_connect fires on every gateway
        # reconnect, so loading there raises ExtensionAlreadyLoaded on resume.
        # setup_hook runs exactly once, before the gateway connects.
        await _load_extensions(self)

        # Start the localhost-only /healthz server before login so Docker's
        # HEALTHCHECK has something to talk to during the start-period window.
        from src.health import start_health_server
        self._health_runner = await start_health_server(self)

        # SIGTERM/SIGINT route through the same drain path as !restart's
        # bot.close(). Windows asyncio doesn't support add_signal_handler;
        # fall back to letting KeyboardInterrupt propagate there.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._signal_close(s)))
            except (NotImplementedError, RuntimeError):
                pass

    async def _signal_close(self, sig: signal.Signals) -> None:
        logging.info("shutdown_signal_received signal=%s", sig.name)
        await self.close()

    async def close(self) -> None:
        """Graceful shutdown: drain in-flight AI streams, close health server,
        close DB pool, then defer to discord.py's normal close."""
        if self._shutdown_done:
            return await super().close()
        self._shutdown_done = True

        from src import ai
        from src.db import close_pool

        t0 = time.monotonic()
        logging.info("shutdown_started in_flight_ai=%d", len(ai._in_flight))
        drained, remaining = await ai.drain_in_flight(SHUTDOWN_DRAIN_SECONDS)
        if drained:
            logging.info(
                "shutdown_ai_drain_complete drained=%d remaining=%d elapsed_ms=%d",
                drained - remaining, remaining, int((time.monotonic() - t0) * 1000),
            )

        runner = getattr(self, "_health_runner", None)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception as e:
                logging.warning("shutdown_health_server_error error=%s", type(e).__name__)

        try:
            await close_pool()
        except Exception as e:
            logging.warning("shutdown_db_pool_error error=%s", type(e).__name__)

        await super().close()
        logging.info("shutdown_complete elapsed_ms=%d", int((time.monotonic() - t0) * 1000))


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True  # needed by !ping voice-subscription listener
    allowed_mentions = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=True)
    bot = Bot(
        command_prefix="!",
        intents=intents,
        help_command=None,
        case_insensitive=True,
        allowed_mentions=allowed_mentions,
    )

    # Per-command latency timing. on_command_completion / on_command_error
    # in EventsCog handle the invocation counter; this hook only owns the
    # histogram observation so the two concerns don't fight over outcome.
    @bot.before_invoke
    async def _record_invoke_start(ctx: commands.Context) -> None:
        ctx._metrics_t0 = time.monotonic()  # type: ignore[attr-defined]

    @bot.after_invoke
    async def _record_invoke_latency(ctx: commands.Context) -> None:
        from src.metrics import command_latency
        t0 = getattr(ctx, "_metrics_t0", None)
        if t0 is None or ctx.command is None:
            return
        command_latency.labels(command=ctx.command.qualified_name).observe(
            time.monotonic() - t0,
        )

    @bot.check
    async def _level_gate(ctx: commands.Context) -> bool:
        """Block commands the author hasn't unlocked yet."""
        if ctx.guild is None or ctx.command is None:
            return True
        from src.level_unlocks import is_locked_for, LevelLocked
        from src.helpers import emb, C_GREY
        required = is_locked_for(ctx.command.qualified_name, ctx.author.id, ctx.guild.id)
        if required is None:
            return True
        await ctx.send(embed=emb(
            "🔒 Locked",
            f"`!{ctx.command.qualified_name}` unlocks at **Level {required}**. Check `!level` for next unlocks.",
            C_GREY,
        ))
        raise LevelLocked()

    return bot


async def _load_extensions(bot: commands.Bot):
    for ext in EXTENSIONS:
        await bot.load_extension(ext)


def run():
    from src.logging_setup import configure_logging

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

    configure_logging()
    _run_with_login_backoff()


def _build_bot() -> commands.Bot:
    """Build a fresh Bot with all event handlers attached.

    Called once per login attempt: discord.py's bot.run() closes the bot's
    aiohttp session on shutdown, so retrying with the same instance fails
    with `RuntimeError: Session is closed`. A fresh Bot per attempt sidesteps
    that entirely.
    """
    bot = create_bot()

    @bot.event
    async def on_message(message):
        # Suppress the default on_message so cog listeners don't double-process commands.
        # EventsCog.on_message handles process_commands itself.
        pass

    return bot


def _run_with_login_backoff() -> None:
    """Run the bot, sleeping in-process on login-time 429 instead of exiting.

    discord.py raises HTTPException out of bot.run() if all 5 of its internal
    /users/@me retries hit 429. Letting that propagate exits the process,
    Docker restarts immediately, and the cycle repeats every ~20s — which
    digs the rate-limit hole deeper. Sleeping here keeps the container alive
    and the backoff state intact across attempts.
    """
    attempt = 0
    while True:
        bot = _build_bot()
        try:
            bot.run(DISCORD_TOKEN)
            return
        except discord.HTTPException as e:
            if e.status != 429:
                raise
            delay = LOGIN_429_BACKOFFS_SECS[min(attempt, len(LOGIN_429_BACKOFFS_SECS) - 1)]
            logging.warning(
                "login_rate_limited attempt=%d status=%d code=%s sleep_s=%d",
                attempt + 1, e.status, getattr(e, "code", None), delay,
            )
            time.sleep(delay)
            attempt += 1
