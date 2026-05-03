import discord
from discord.ext import commands

from src.config import DISCORD_TOKEN

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
    async def process_commands(self, message: discord.Message) -> None:
        """Allow other bots to invoke commands (default implementation skips bots)."""
        if message.author == self.user:
            return
        ctx = await self.get_context(message)
        await self.invoke(ctx)  # type: ignore


def create_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    bot = Bot(command_prefix="!", intents=intents, help_command=None, case_insensitive=True)

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
    import logging

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

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
