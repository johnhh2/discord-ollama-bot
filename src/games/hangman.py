import asyncio
import logging
import os
import random
import time

import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_ORANGE, _delete_after, _edit_board, announce_record,
)
from src.economy import (
    add_balance, get_balance, get_total_balance, record_gambling_event,
)
from src.permissions import (
    check_game_channel,
)
from src.persistence import (
    try_set_record, load_records,
)
from src.invites import _wait_for_confirmations
from src.games.game_threads import (
    _refuse_in_thread, _try_create_game_thread, _add_thread_members,
    _close_game_thread, _join_names,
)
from src.config import (
    HANGMAN_MAX_WRONG, HANGMAN_BASE_REWARD, HANGMAN_LENGTH_OFFSET,
    HANGMAN_LENGTH_MULT, HANGMAN_UNIQUE_MULT, HANGMAN_RARE_MULT, HANGMAN_ULTRA_RARE_MULT,
)
from src import state


# ── Hangman helpers ───────────────────────────────────────────────────────────

_hangman_words_path = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "hangman_words.txt")
# File format: word,category,bonus  (category is "name" or "word"; bonus is added to the
# computed reward when this word is guessed)
HANGMAN_WORDS: list[str] = []
HANGMAN_WORD_BONUS: dict[str, int] = {}
with open(_hangman_words_path) as _f:
    for _line in _f:
        _line = _line.strip()
        if not _line:
            continue
        _parts = _line.split(",")
        _w = _parts[0].strip()
        _bonus = int(_parts[2].strip()) if len(_parts) >= 3 and _parts[2].strip() else 0
        HANGMAN_WORDS.append(_w)
        if _bonus:
            HANGMAN_WORD_BONUS[_w] = _bonus

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
    # Clamp: a guess racing the terminal edit can push wrong_guesses past the
    # art range; rendering must never IndexError (it would jam the channel).
    wrong = min(game["wrong_guesses"], len(HANGMAN_ART) - 1)
    blanks = " ".join(c if c in guessed else "_" for c in word)
    guessed_str = ", ".join(sorted(guessed)) if guessed else "none"
    lives_left = max(0, 6 - wrong)
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

    word_bonus = HANGMAN_WORD_BONUS.get(word_lower, 0)
    total = base + length_bonus + unique_bonus + rare_bonus + word_bonus
    return total


async def _distribute_hangman_rewards(cid: int, game: dict) -> tuple[str, list[tuple[str, str, int, int]]]:
    """Distributes win rewards, deletes the game, and returns (reward_message, pending_records)
    where pending_records is a list of (category, holder_name, value, holder_id) for records that should be
    announced AFTER the caller sends the result embed."""
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
    pending: list[tuple[str, str, int, int]] = []
    for i, pid in enumerate(active_players):
        bonus = 1 if i < remainder else 0
        reward = per_player + bonus
        name = names.get(pid, f"<@{pid}>")
        new_bal_record = await add_balance(pid, reward, guild_id=gid if gid else None, holder_name=name)
        if reward > 0:
            await record_gambling_event(gid, pid, gained=reward)
        new_bal = await get_balance(pid)
        msg += f"**{name}**: +{reward:,} 🪙 | Balance: {new_bal:,} 🪙\n"
        if new_bal_record:
            pending.append(("highest_balance", name, await get_total_balance(pid), pid))
        # Per-player wins row, but announce only when it beats the guild leader's
        # row — otherwise each win trivially beats the player's own prior count.
        if gid:
            wins_key = f"hangman_wins_{pid}"
            records = await load_records(gid)
            current_wins = records.get(wins_key, {}).get("value", 0)
            new_wins = current_wins + 1
            guild_leader_wins = max(
                (v.get("value", 0) for k, v in records.items() if k.startswith("hangman_wins_")),
                default=0,
            )
            updated = await try_set_record(gid, wins_key, new_wins, pid, name)
            if updated and new_wins > guild_leader_wins:
                pending.append((wins_key, name, new_wins, pid))
    # Track biggest hangman payout (use total for multiplayer, per-player for solo)
    payout_value = total_reward if len(active_players) == 1 else per_player
    first_pid = active_players[0]
    first_name = names.get(first_pid, str(first_pid))
    if await try_set_record(gid, "hangman_payout", payout_value, first_pid, first_name, word=word):
        pending.append(("hangman_payout", first_name, payout_value, first_pid))
    return msg.strip(), pending


def _hangman_thread_name(game: dict, *, won: bool) -> str:
    """'🎉 A, B solved python' / '💀 A lost — the word was python'. Names
    the players who actually guessed (the host alone if nobody did)."""
    names = game.get("player_names", {})
    active = game.get("active_players", set())
    who = [names.get(pid, str(pid)) for pid in names if pid in active]
    if not who:
        host = game.get("user_id")
        who = [names.get(host, str(host))]
    word = game["word"]
    if won:
        return f"🎉 {_join_names(who)} solved {word}"
    return f"💀 {_join_names(who)} lost — the word was {word}"


def _open_board_embed(game: dict, color: int) -> discord.Embed:
    return emb(
        "🔤 Hangman",
        build_hangman_display(game)
        + "\n\nJust type a letter or use `!guess`/`!g` to guess the full word!"
        + f"\n\n**Last move:** {game['last_move']}",
        color,
    )


async def _hangman_won(channel, cid: int, game: dict, title: str) -> None:
    """Pay out, post the final board, announce records, then close the
    game's thread (records must land before the archive)."""
    reward_msg, pending_records = await _distribute_hangman_rewards(cid, game)
    await _edit_board(channel, game, emb(title, build_hangman_display(game) + "\n\n" + reward_msg + f"\n\n**Last move:** {game['last_move']}", C_GREEN))
    for cat, holder, val, hid in pending_records:
        await announce_record(channel, cat, holder, val, holder_id=hid)
    await _close_game_thread(channel, _hangman_thread_name(game, won=True))


async def _hangman_lost(channel, cid: int, game: dict, name: str, guess: str) -> None:
    """Out of lives. Settle synchronously before the board edit: a concurrent
    guess during the await must find the game already gone — and if the
    edit raises, the game must not stay stuck at max wrong guesses forever
    (the win path already does this)."""
    del state.active_hangman_games[cid]
    word = game["word"]
    game["last_move"] = f"{name} guessed `{guess}` — Game over! The word was `{word}`"
    pot_msg = hangman_pot_msg(word, len(game["active_players"]))
    await _edit_board(channel, game, emb("💀 Game Over", build_hangman_display(game) + f"\n\nThe word was `{word}`.\n\n{pot_msg}\n\n**Last move:** {game['last_move']}", C_RED))
    await _close_game_thread(channel, _hangman_thread_name(game, won=False))


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
            await _hangman_won(channel, cid, game, "🎉 Correct!")
        elif guess in game["guessed_words"]:
            game["last_move"] = f"{name} guessed `{guess}` ❌ (already tried)"
            await _edit_board(channel, game, _open_board_embed(game, C_ORANGE))
        else:
            game["guessed_words"].add(guess)
            game["wrong_guesses"] += 1
            if game["wrong_guesses"] >= HANGMAN_MAX_WRONG:
                await _hangman_lost(channel, cid, game, name, guess)
            else:
                game["last_move"] = f"{name} guessed `{guess}` ❌"
                await _edit_board(channel, game, _open_board_embed(game, C_RED))
        return

    # Single letter guess
    if guess in game["guessed_letters"]:
        game["last_move"] = f"{name} guessed `{guess}` (already tried)"
        await _edit_board(channel, game, _open_board_embed(game, C_ORANGE))
        return
    game["guessed_letters"].add(guess)
    if guess in game["word"]:
        if all(c in game["guessed_letters"] for c in game["word"]):
            game["last_move"] = f"{name} guessed `{guess}` ✅ — word complete! 🎉"
            await _hangman_won(channel, cid, game, "🎉 You Got It!")
        else:
            game["last_move"] = f"{name} guessed `{guess}` ✅"
            await _edit_board(channel, game, _open_board_embed(game, C_GREEN))
    else:
        game["wrong_guesses"] += 1
        if game["wrong_guesses"] >= HANGMAN_MAX_WRONG:
            await _hangman_lost(channel, cid, game, name, guess)
        else:
            game["last_move"] = f"{name} guessed `{guess}` ❌"
            await _edit_board(channel, game, _open_board_embed(game, C_ORANGE))


class HangmanCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # epoch of last bot-initiated hangman per uid, for the 6h cooldown.
        self._last_hangman_by_uid: dict[int, float] = {}

    @commands.command(name="hangman", aliases=["hang", "hm"])
    async def cmd_hangman(self, ctx: commands.Context, *args):
        if await check_game_channel(ctx):
            return
        if await _refuse_in_thread(ctx):
            return
        uid = ctx.author.id
        _HANGMAN_COOLDOWN = 6 * 3600
        if ctx.author.bot:
            now = time.time()
            last = self._last_hangman_by_uid.get(uid, 0)
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
            self._last_hangman_by_uid[uid] = time.time()
        word = random.choice(HANGMAN_WORDS)
        game = {
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
        # Claim the parent channel synchronously for the invite window; the
        # game moves into its own thread once the lobby is settled.
        state.active_hangman_games[cid] = game
        # Invite flow for mentioned users
        invited_users = [m for m in ctx.message.mentions if m.id != ctx.author.id]
        if invited_users:
            confirmed = await _wait_for_confirmations(ctx, invited_users, title="📨 Hangman Invite")
            if state.active_hangman_games.get(cid) is not game:
                return  # host !stop'd while the invite was pending
            game["invited_players"].update(confirmed)
        players = [ctx.author] + [m for m in invited_users if m.id in game["invited_players"]]
        # The lobby gets its own thread and the game is keyed by the thread's
        # id, so the parent channel is free for the next game immediately.
        # Falls back to the invoking channel when a thread can't be created.
        thread = await _try_create_game_thread(
            ctx, f"🔤 Hangman: {_join_names([p.display_name for p in players])}",
        )
        if state.active_hangman_games.get(cid) is not game:
            await _close_game_thread(thread)  # !stop'd mid-creation; don't orphan it
            return
        dest = ctx.channel
        if thread is not None:
            del state.active_hangman_games[cid]
            state.active_hangman_games[thread.id] = game
            dest = thread
            await _add_thread_members(thread, *players)
        board_msg = await dest.send(embed=_open_board_embed(game, C_ORANGE), silent=True)
        game["board_msg_id"] = board_msg.id

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        """A mod deleting a game thread would strand the game forever (no
        channel left to guess or `!stop` in) — drop it. Nothing to refund:
        hangman has no stake."""
        if state.active_hangman_games.pop(thread.id, None) is not None:
            logging.info(f"hangman: cancelled game in deleted thread {thread.id}")

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
