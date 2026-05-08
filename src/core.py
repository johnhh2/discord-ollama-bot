import asyncio
import logging
import signal
import time

import discord
from discord.ext import commands

from src.config import DISCORD_TOKEN

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
    "src.events",
]


class Bot(commands.Bot):
    _shutdown_done: bool = False

    async def process_commands(self, message: discord.Message) -> None:
        """Allow other bots to invoke commands (default implementation skips bots)."""
        if message.author == self.user:
            return
        ctx = await self.get_context(message)
        await self.invoke(ctx)  # type: ignore

    async def setup_hook(self) -> None:
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

    bot = create_bot()

    @bot.event
    async def on_connect():
        await _load_extensions(bot)

    @bot.event
    async def on_message(message):
        # Suppress the default on_message so cog listeners don't double-process commands.
        # EventsCog.on_message handles process_commands itself.
        pass

    bot.run(DISCORD_TOKEN)
