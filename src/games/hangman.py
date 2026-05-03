import asyncio
import os
import random
import time

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_ORANGE, _delete_after, _edit_board,
)
from src.economy import (
    add_balance, get_balance,
)
from src.permissions import (
    check_game_channel,
)
from src.persistence import (
    try_set_record, load_records,
)
from src.invites import _wait_for_confirmations
from src.config import (
    HANGMAN_MAX_WRONG, HANGMAN_BASE_REWARD, HANGMAN_LENGTH_OFFSET,
    HANGMAN_LENGTH_MULT, HANGMAN_UNIQUE_MULT, HANGMAN_RARE_MULT, HANGMAN_ULTRA_RARE_MULT,
)
from src import state


# ── Hangman helpers ───────────────────────────────────────────────────────────

_hangman_words_path = os.path.join(os.path.dirname(__file__), "..", "..", "hangman_words.txt")
with open(_hangman_words_path) as _f:
    HANGMAN_WORDS = [w.strip() for w in _f if w.strip()]

HANGMAN_ART = [
    "```\n  +---+\n  |   |\n      |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n=========```",
    "```\n  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n=========```",
]


def build_hangman_display(game: dict) -> str:
    word = game["word"]
    guessed = game["guessed_letters"]
    wrong = game["wrong_guesses"]
    blanks = " ".join(c if c in guessed else "_" for c in word)
    guessed_str = ", ".join(sorted(guessed)) if guessed else "none"
    lives_left = 6 - wrong
    return (
        f"{HANGMAN_ART[wrong]}\n"
        f"Word: `{blanks}`\n"
        f"Guessed: {guessed_str}\n"
        f"Lives left: {lives_left}"
    )


def hangman_pot_msg(word: str, player_count: int) -> str:
    """Return a human-readable 'you would've won/split X' string for hangman game-over."""
    total = calculate_hangman_reward(word)
    per = total // player_count
    if player_count == 1:
        return f"💰 You would've won **{total:,} 🪙**"
    return f"💰 You would've split **{total:,} 🪙** ({per:,} each)"


def calculate_hangman_reward(word: str) -> int:
    """Calculate hangman reward based on word difficulty.

    Formula (AI-derived):
    - Base: 10 coins
    - Length Bonus: (word_length - 3) × 6
    - Unique Letters Bonus: unique_count × 3
    - Rare Letters Bonus: rare_count × 15

    Examples:
    - 5-letter average word (APPLE): ~25 coins
    - 10-letter hard word with rare letters: ~150 coins
    """
    ULTRA_RARE_LETTERS = {'q', 'x', 'z'}
    RARE_LETTERS = {'y', 'j', 'k', 'w', 'v'}

    word_lower = word.lower()
    base = HANGMAN_BASE_REWARD
    length_bonus = max(0, (len(word) - HANGMAN_LENGTH_OFFSET)) * HANGMAN_LENGTH_MULT
    unique_count = len(set(word_lower))
    unique_bonus = unique_count * HANGMAN_UNIQUE_MULT
    rare_count = sum(1 for c in word_lower if c in RARE_LETTERS)
    ultra_rare_count = sum(1 for c in word_lower if c in ULTRA_RARE_LETTERS)
    rare_bonus = (rare_count * HANGMAN_RARE_MULT) + (ultra_rare_count * HANGMAN_ULTRA_RARE_MULT)

    total = base + length_bonus + unique_bonus + rare_bonus
    return total


async def _distribute_hangman_rewards(cid: int, game: dict) -> str:
    """Distributes win rewards, deletes the game, and returns the reward message."""
    word = game["word"]
    gid = game.get("guild_id")
    total_reward = calculate_hangman_reward(word)
    active_players = list(game["active_players"])
    per_player = total_reward // len(active_players)
    remainder = total_reward % len(active_players)
    del state.active_hangman_games[cid]
    if len(active_players) == 1:
        msg = f"The word was `{word}`!\n\n"
    else:
        msg = f"The word was `{word}`!\n\n**Total: {total_reward:,} 🪙** split among {len(active_players)} players\n"
    names = game.get("player_names", {})
    for i, pid in enumerate(active_players):
        bonus = 1 if i < remainder else 0
        reward = per_player + bonus
        name = names.get(pid, f"<@{pid}>")
        await add_balance(pid, reward, guild_id=gid if gid else None, holder_name=name)
        msg += f"**{name}**: +{reward:,} 🪙 | Balance: {await get_balance(pid):,} 🪙\n"
        # Track most hangman wins per player
        if gid:
            wins_key = f"hangman_wins_{pid}"
            records = await load_records(gid)
            current_wins = records.get(wins_key, {}).get("value", 0)
            await try_set_record(gid, wins_key, current_wins + 1, pid, name)
    # Track biggest hangman payout (use total for multiplayer, per-player for solo)
    payout_value = total_reward if len(active_players) == 1 else per_player
    first_pid = active_players[0]
    first_name = names.get(first_pid, str(first_pid))
    await try_set_record(gid, "hangman_payout", payout_value, first_pid, first_name, word=word)
    return msg.strip()


async def _process_hangman_guess(channel: discord.abc.Messageable, author_id: int, cid: int, guess: str, author_name: str):
    """Shared hangman guess logic used by both `!guess`/`!g` command and free-text intercept."""
    game = state.active_hangman_games[cid]

    if author_id not in game["invited_players"]:
        return

    if not guess.isalpha():
        return  # silently ignore non-alpha free-text; cmd_guess shows an error

    name = author_name
    game["player_names"][author_id] = author_name

    # Track this player as active
    game["active_players"].add(author_id)

    # Full word guess
    if len(guess) > 1:
        if guess == game["word"]:
            game["last_move"] = f"{name} guessed the word! 🎉"
            game["guessed_letters"].update(game["word"])  # reveal full word for display
            reward_msg = await _distribute_hangman_rewards(cid, game)
            await _edit_board(channel, game, emb("🎉 Correct!", build_hangman_display(game) + "\n\n" + reward_msg + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
        elif guess in game["guessed_words"]:
            game["last_move"] = f"{name} guessed `{guess}` ❌ (already tried)"
            await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_ORANGE))
        else:
            game["guessed_words"].add(guess)
            game["wrong_guesses"] += 1
            if game["wrong_guesses"] >= HANGMAN_MAX_WRONG:
                word = game["word"]
                game["last_move"] = f"{name} guessed `{guess}` — Game over! The word was `{word}`"
                pot_msg = hangman_pot_msg(word, len(game["active_players"]))
                await _edit_board(channel, game, emb("💀 Game Over", build_hangman_display(game) + f"\n\nThe word was `{word}`.\n\n{pot_msg}\n\n**Last move:** {game['last_move']}", C_RED))
                del state.active_hangman_games[cid]
            else:
                game["last_move"] = f"{name} guessed `{guess}` ❌"
                await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_RED))
        return

    # Single letter guess
    if guess in game["guessed_letters"]:
        game["last_move"] = f"{name} guessed `{guess}` (already tried)"
        await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_ORANGE))
        return
    game["guessed_letters"].add(guess)
    if guess in game["word"]:
        if all(c in game["guessed_letters"] for c in game["word"]):
            game["last_move"] = f"{name} guessed `{guess}` ✅ — word complete! 🎉"
            reward_msg = await _distribute_hangman_rewards(cid, game)
            await _edit_board(channel, game, emb("🎉 You Got It!", build_hangman_display(game) + "\n\n" + reward_msg + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
        else:
            game["last_move"] = f"{name} guessed `{guess}` ✅"
            await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_GREEN))
    else:
        game["wrong_guesses"] += 1
        if game["wrong_guesses"] >= HANGMAN_MAX_WRONG:
            word = game["word"]
            game["last_move"] = f"{name} guessed `{guess}` — Game over! The word was `{word}`"
            pot_msg = hangman_pot_msg(word, len(game["active_players"]))
            await _edit_board(channel, game, emb("💀 Game Over", build_hangman_display(game) + f"\n\nThe word was `{word}`.\n\n{pot_msg}\n\n**Last move:** {game['last_move']}", C_RED))
            del state.active_hangman_games[cid]
        else:
            game["last_move"] = f"{name} guessed `{guess}` ❌"
            await _edit_board(channel, game, emb("🔤 Hangman", build_hangman_display(game) + f"\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** {game['last_move']}", C_ORANGE))


class HangmanCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="hangman", aliases=["hang", "hm"])
    async def cmd_hangman(self, ctx: commands.Context, *args):
        if await check_game_channel(ctx):
            return
        uid = ctx.author.id
        _HANGMAN_COOLDOWN = 6 * 3600
        if ctx.author.bot:
            now = time.time()
            last = state.user_last_hangman.get(uid, 0)
            if now - last < _HANGMAN_COOLDOWN:
                remaining = int(_HANGMAN_COOLDOWN - (now - last))
                h, m = divmod(remaining // 60, 60)
                await ctx.send(embed=emb("🔤 Cooldown", f"You can start another hangman in **{h}h {m}m**.", C_ORANGE))
                return
        cid = ctx.channel.id
        if cid in state.active_hangman_games:
            await ctx.send(embed=emb("🔤 Already Playing", "Just type your guess directly!", C_ORANGE))
            return
        if ctx.author.bot:
            state.user_last_hangman[uid] = time.time()
        word = random.choice(HANGMAN_WORDS)
        state.active_hangman_games[cid] = {
            "word": word,
            "guessed_letters": set(),
            "guessed_words": set(),  # Track full word guesses to prevent repeats
            "wrong_guesses": 0,
            "user_id": ctx.author.id,
            "guild_id": ctx.guild.id if ctx.guild else None,
            "active_players": {ctx.author.id},  # Track who's actively guessing (for rewards)
            "invited_players": {ctx.author.id},  # Only these users may guess
            "player_names": {ctx.author.id: ctx.author.display_name},
            "board_msg_id": None,
            "last_move": "Game started!",
        }
        # Invite flow for mentioned users
        invited_users = [m for m in ctx.message.mentions if m.id != ctx.author.id]
        if invited_users:
            confirmed = await _wait_for_confirmations(ctx, invited_users, title="📨 Hangman Invite")
            state.active_hangman_games[cid]["invited_players"].update(confirmed)
        game = state.active_hangman_games[cid]
        board_msg = await ctx.send(embed=emb("🔤 Hangman", build_hangman_display(game) + "\n\nJust type a letter or use `!guess`/`!g` to guess the full word!\n\n**Last move:** Game started!", C_ORANGE))
        game["board_msg_id"] = board_msg.id

    @commands.command(name="guess", aliases=["g"])
    async def cmd_guess(self, ctx: commands.Context, *, guess: str = None):
        cid = ctx.channel.id
        asyncio.create_task(_delete_after(ctx.message))
        if cid not in state.active_hangman_games:
            err = await ctx.send(embed=emb("🔤 No Game", "No active hangman game. Start one with `!hangman`.", C_ORANGE))
            asyncio.create_task(_delete_after(err))
            return
        if guess is None:
            err = await ctx.send(embed=emb("🔤 Hangman", "Usage: `!guess <letter or word>`", C_ORANGE))
            asyncio.create_task(_delete_after(err))
            return
        await _process_hangman_guess(ctx.channel, ctx.author.id, cid, guess.lower().strip(), ctx.author.display_name)


async def setup(bot):
    await bot.add_cog(HangmanCog(bot))
