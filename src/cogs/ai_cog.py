import asyncio
import datetime
import io
import logging
from zoneinfo import ZoneInfo

import aiohttp
import chess
import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, C_GREY,
    _edit_board, _delete_after, _log_audit, log_bot_permission_error,
)
from src.economy import (
    add_balance, get_guild_ask_model, get_guild_roleplay_model,
    record_gambling_event, _ct_today, next_daily_reset_ts,
)
from src.permissions import (
    is_admin,
    check_ai_channel,
    requires_perm,
)
from src.persistence import (
    delete_chess_game, save_chess_report, save_ai_threads, save_recap_usage,
)
from src.guild_config import get_guild_cfg
from src.ai import (
    enforce_cost, refund_cost, keep_typing,
    stream_ollama, finalize, respond,
    check_token_budget_or_notify,
    ASK_SYSTEM_PROMPT, STORY_SYSTEM_PROMPT,
)
from src.config import (
    OLLAMA_MODEL,
)
from src import state
from src.invites import _wait_for_confirmations, _send_invite
# Forfeit-board renderers used in cmd_stop. NOTE: these were referenced
# inline since the original bot.py split but never actually imported —
# !stop in a ttt/c4/chess/hangman game would NameError. Tests in
# test_cmd_stop.py now cover this. Don't drop these without adjusting
# the cmd_stop coverage.
from src.games.ttt_c4 import build_ttt_display, build_c4_display
from src.games.hangman import build_hangman_display
from src.games.blackjack import retire_blackjack_buttons
from src.games import chess_engine, chess_render
from src.chess_shop import equipped_cosmetics
from src.games.chess import (
    BOARD_IMG_FILENAME, _bump_board as _bump_chess_board, _game_result_name,
)
from src.games.game_threads import _close_game_thread
from src.games.ttt_c4 import _player_name as _pvp_player_name


log = logging.getLogger(__name__)


async def _try_create_thread(ctx, name: str):
    """Open a thread on the invoking message, or None to fall back to the channel.

    Every caller runs `enforce_cost` first, so an unguarded raise here charged
    the user and delivered nothing — `create_thread` fails routinely (no
    Create Public Threads permission, the message already has a thread, the
    channel hit its active-thread cap). Degrading to an in-channel session
    keeps the purchase honoured.
    """
    if not (ctx.guild and isinstance(ctx.channel, discord.TextChannel)):
        return None
    try:
        return await ctx.message.create_thread(name=name)
    except discord.HTTPException as e:
        logging.warning(
            "[ai] create_thread failed (%s); continuing in channel", type(e).__name__,
        )
        return None


class AICog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="ask")
    async def cmd_ask(self, ctx: commands.Context, *, question: str = None):
        if await check_ai_channel(ctx):
            return
        if question is None:
            await ctx.send("Usage: `!ask <question>`")
            return

        if not await check_token_budget_or_notify(ctx, question):
            return

        # Check cost
        if not await enforce_cost(ctx, "ask"):
            return

        # Gather last 10 channel messages (excluding the !ask command itself)
        history_lines = []
        async for msg in ctx.channel.history(limit=11):
            if msg.id == ctx.message.id:
                continue
            if len(history_lines) >= 10:
                break
            history_lines.append(f"[{msg.author.display_name}]: {msg.content[:200]}")
        history_lines.reverse()

        if history_lines:
            context_block = "\n".join(history_lines)
            system_prompt = (
                ASK_SYSTEM_PROMPT
                + "\n\nThe following is recent channel conversation. "
                "Only use it as context if it is directly relevant to the question — ignore it otherwise:\n"
                f"<context>\n{context_block}\n</context>"
            )
        else:
            system_prompt = ASK_SYSTEM_PROMPT

        guild_id = ctx.guild.id if ctx.guild else None
        thread = await _try_create_thread(ctx, f"ask: {question[:80]}")
        if thread is not None:
            state.ai_threads[thread.id] = {
                "kind": "ask",
                "owner_id": ctx.author.id,
                "guild_id": guild_id,
                "invited_ids": {ctx.author.id},
                "system_prompt": ASK_SYSTEM_PROMPT,
                "character_prompt": None,
                "history": [],
            }
            await respond(thread, ctx.author.id, question, ctx.message, system_prompt=system_prompt, guild_id=guild_id, author_name=ctx.author.display_name, refund_feature="ask")
            await thread.send(embed=emb("💬 Ask Thread", "Keep talking — I'll remember the conversation.\n`!invite @user` — let someone else join · `!stop` — end the thread", C_BLUE))
        else:
            await respond(ctx.channel, ctx.author.id, question, ctx.message, system_prompt=system_prompt, guild_id=guild_id, author_name=ctx.author.display_name, refund_feature="ask")


    @commands.command(name="story")
    async def cmd_story(self, ctx: commands.Context, *, prompt: str = None):
        await self._story_with_prompt(ctx, prompt=prompt, system_prompt=STORY_SYSTEM_PROMPT, alias_name=None)

    def _story_alias_hint(self, ctx: commands.Context, alias_name: str | None) -> str:
        """Suffix listing this guild's story aliases, shown on canonical !story usage only."""
        if alias_name is not None or not ctx.guild:
            return ""
        aliases = get_guild_cfg(ctx.guild.id).get("story_aliases", {})
        if not aliases:
            return ""
        return "\nAliases: " + ", ".join(f"`!{k}`" for k in aliases)

    async def _story_with_prompt(
        self,
        ctx: commands.Context,
        *,
        prompt: str,
        system_prompt: str,
        alias_name: str | None,
    ):
        """Shared body for !story and any guild-defined story aliases.

        When invoked via an alias listener, alias_name is the alias word
        (used in usage hints and thread titles) and system_prompt is the
        custom prompt registered for that alias.
        """
        cmd_label = alias_name or "story"
        usage = f"Usage: `!{cmd_label} <prompt> [@user1 @user2 ...]` — e.g. `!{cmd_label} Batman and Superman stuck in an elevator`"

        if await check_ai_channel(ctx):
            return
        if prompt is None:
            await ctx.send(usage + self._story_alias_hint(ctx, alias_name))
            return

        uid = ctx.author.id
        invited_users = [m for m in ctx.message.mentions if m.id != uid]
        clean_prompt = prompt
        for m in ctx.message.mentions:
            clean_prompt = clean_prompt.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        clean_prompt = clean_prompt.strip()
        if not clean_prompt:
            await ctx.send(usage + self._story_alias_hint(ctx, alias_name))
            return

        if not await check_token_budget_or_notify(ctx, clean_prompt):
            return

        if not await enforce_cost(ctx, "story"):
            return

        thread_label = alias_name.capitalize() if alias_name else "Story"
        guild_id = ctx.guild.id if ctx.guild else None
        thread = await _try_create_thread(ctx, f"{thread_label}: {clean_prompt[:75]}")
        if thread is not None:
            state.ai_threads[thread.id] = {
                "kind": "story",
                "owner_id": uid,
                "guild_id": guild_id,
                "invited_ids": {uid},
                "system_prompt": system_prompt,
                "character_prompt": None,
                "history": [],
            }

            async def _story_join(user, _thread=thread, _thread_id=thread.id):
                member = ctx.guild.get_member(user.id) if ctx.guild else None
                if member:
                    try:
                        await _thread.add_user(member)
                    except Exception:
                        pass
                t = state.ai_threads.get(_thread_id)
                if t is not None:
                    t["invited_ids"].add(user.id)
                    await save_ai_threads()
                await _thread.send(embed=emb("✅ Joined", f"{user.mention} joined the {thread_label.lower()}!", C_GREEN))

            if invited_users:
                await _send_invite(ctx, invited_users, title=f"📨 {thread_label} Invite", dest=thread, on_join=_story_join)

            await respond(thread, uid, clean_prompt, ctx.message, system_prompt=system_prompt, guild_id=guild_id, refund_feature="story")
            await thread.send(embed=emb(f"📖 {thread_label} Started", "`!continue` — next chapter · `!reverse` — undo last response · `!invite @user` — add a co-author · `!stop` — end the story", C_BLUE))
        else:
            await respond(ctx.channel, uid, clean_prompt, ctx.message, system_prompt=system_prompt, guild_id=guild_id, refund_feature="story")


    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Route !<alias> to cmd_story when the alias is a guild-configured story alias."""
        if not isinstance(error, commands.CommandNotFound):
            return
        if not ctx.guild:
            return
        parts = ctx.message.content.strip().split(None, 1)
        if not parts:
            return
        word = parts[0][1:].lower()
        story_aliases = get_guild_cfg(ctx.guild.id).get("story_aliases", {})
        if word not in story_aliases:
            return
        custom_prompt = story_aliases[word]
        rest = parts[1] if len(parts) > 1 else None
        ctx.invoked_with = word
        # Set ctx.command so any downstream code keyed on ctx.command (rate
        # limits, level-unlock gate in core.py, future @requires_perm) treats
        # the alias dispatch as if !story were invoked.
        ctx.command = self.cmd_story
        await self._story_with_prompt(ctx, prompt=rest, system_prompt=custom_prompt, alias_name=word)


    @commands.command(name="continue")
    async def cmd_continue(self, ctx: commands.Context):
        if not isinstance(ctx.channel, discord.Thread):
            await ctx.send(embed=emb("❌ Threads Only", "`!continue` only works inside an AI thread.", C_RED))
            return

        uid = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        t = state.ai_threads.get(ctx.channel.id)
        history = t["history"] if t else state.channel_histories[ctx.channel.id]

        if not history:
            await ctx.send(embed=emb("❌ Nothing to Continue", "No story to continue in this thread.", C_RED))
            return

        if not await check_token_budget_or_notify(ctx, "Continue the story."):
            return

        if not await enforce_cost(ctx, "continue"):
            return

        sp = t.get("system_prompt") if t else STORY_SYSTEM_PROMPT
        await respond(ctx.channel, uid, "Continue the story.", ctx.message, system_prompt=sp, guild_id=guild_id, refund_feature="continue")


    @commands.command(name="tldr")
    async def cmd_tldr(self, ctx: commands.Context):
        if not isinstance(ctx.channel, discord.Thread):
            await ctx.send(embed=emb("❌ Threads Only", "`!tldr` only works inside an AI thread.", C_RED))
            return

        guild_id = ctx.guild.id if ctx.guild else None
        last_text = None

        t = state.ai_threads.get(ctx.channel.id)
        history = t["history"] if t else state.channel_histories[ctx.channel.id]
        for entry in reversed(history):
            if entry["role"] == "assistant":
                last_text = entry["content"]
                break

        if not last_text:
            await ctx.send(embed=emb("❌ Nothing to Summarize", "No AI response found in this thread yet.", C_RED))
            return

        if not await check_token_budget_or_notify(ctx, last_text):
            return

        tldr_prompt = [
            {"role": "system", "content": "You are a concise summarizer. Summarize the following story excerpt in 2-3 sentences, capturing the key events and mood. Do not editorialize or add commentary — just summarize."},
            {"role": "user", "content": f"Summarize this:\n\n{last_text}"},
        ]

        placeholder = await ctx.channel.send("📝 Summarizing...")
        typing_task = asyncio.create_task(keep_typing(ctx.channel))
        try:
            async with aiohttp.ClientSession() as session:
                model = get_guild_ask_model(guild_id) if guild_id else OLLAMA_MODEL
                summary = await stream_ollama(session, tldr_prompt, placeholder, guild_id=guild_id, model=model, user_id=ctx.author.id)
            if not summary:
                return
            await finalize(placeholder, ctx.channel, f"**TL;DR:** {summary}")
        except aiohttp.ClientError:
            await placeholder.edit(content="", embed=emb("", "The AI is currently offline.", C_RED))
        except Exception as e:
            await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
        finally:
            typing_task.cancel()


    @commands.command(name="roleplay")
    async def cmd_roleplay(self, ctx: commands.Context, *, character_prompt: str = None):
        if await check_ai_channel(ctx):
            return
        uid = ctx.author.id
        if character_prompt is None:
            await ctx.send("Usage: `!roleplay <character prompt> [@user1 @user2 ...]`")
            return

        # Parse mentions and clean prompt
        invited_users = [m for m in ctx.message.mentions if m.id != uid]
        clean_prompt = character_prompt
        for m in ctx.message.mentions:
            clean_prompt = clean_prompt.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        clean_prompt = clean_prompt.strip()
        if not clean_prompt:
            await ctx.send("Usage: `!roleplay <character prompt> [@user1 @user2 ...]`")
            return

        if not await check_token_budget_or_notify(ctx, clean_prompt):
            return

        if not await enforce_cost(ctx, "roleplay"):
            return

        # Create a thread to contain the roleplay
        guild_id = ctx.guild.id if ctx.guild else None
        thread = await _try_create_thread(ctx, f"roleplay: {clean_prompt[:70]}")
        rp_channel_id = thread.id if thread is not None else ctx.channel.id

        rp_system_prompt = (
            f"You are roleplaying as the following character and must stay in character "
            f"for every response, no matter what: {clean_prompt}. "
            f"Never break character or acknowledge that you are an AI."
        )
        state.ai_threads[rp_channel_id] = {
            "kind": "roleplay",
            "owner_id": uid,
            "guild_id": guild_id,
            "invited_ids": {uid},
            "system_prompt": rp_system_prompt,
            "character_prompt": clean_prompt,
            "history": [],
        }
        await save_ai_threads()

        async def _rp_join(user, _rp_channel_id=rp_channel_id, _dest=thread or ctx.channel):
            t = state.ai_threads.get(_rp_channel_id)
            if t is not None:
                t["invited_ids"].add(user.id)
                await save_ai_threads()
            await _dest.send(embed=emb("✅ Joined", f"{user.mention} joined the roleplay!", C_GREEN))

        if invited_users:
            await _send_invite(ctx, invited_users, title="📨 Roleplay Invite", dest=thread or ctx, on_join=_rp_join)

        preview = clean_prompt[:100] + ("..." if len(clean_prompt) > 100 else "")
        dest = thread or ctx.channel
        await dest.send(embed=emb(
            "🎭 Roleplay Started",
            f"Responding as: *{preview}*\nType freely — no @mention needed.\n`!reverse` — undo last response · `!invite @user` — add a participant · `!stop` — end the roleplay",
            C_BLUE,
        ))


    @commands.command(name="rpg")
    async def cmd_rpg(self, ctx: commands.Context):
        if await check_ai_channel(ctx):
            return
        uid = ctx.author.id

        # Parse mentions for multiplayer
        invited_users = [m for m in ctx.message.mentions if m.id != uid]

        if not await check_token_budget_or_notify(ctx, "Begin the adventure."):
            return

        if not await enforce_cost(ctx, "rpg"):
            return

        # Register host with participants set
        rpg_system_prompt = (
            "Purpose:\n"
            "To create an immersive, text-based role-playing game.\n"
            "To guide the player through a narrative driven by their choices.\n\n"
            "Function:\n"
            "Out-of-Game Communication: Respond to the player as \"GAL,\" which stands for \"Game AI Liaison.\" This helps distinguish between in-game and out-of-game communication.\n"
            "In-Game Communication: When interacting with NPCs, respond in character, maintaining their personality, motivations, and knowledge of the world. Simulate a natural conversation, responding to the player's input and driving the narrative forward.\n"
            "Worldbuilding: Construct a detailed and consistent game world, including lore, locations, and NPCs. There should be an engaging overarching main story that guides the player through the world.\n"
            "Character Development: Assist the player in creating and developing their character, providing opportunities for growth and customization.\n"
            "Narrative Progression: Present choices and challenges, advancing the story based on the player's decisions.\n"
            "Rule Enforcement: Adhere to the established rules and guidelines to maintain consistency.\n"
            "Sheet Management: Maintain and update character sheets, party sheets, and quest logs, and present them to the player upon request.\n"
            "Player Engagement: Incorporate elements such as puzzles, riddles, and mini-games to keep the player interested and challenged.\n"
            "Reward System: Implement a system of rewards, such as experience points, treasure, or special abilities, to motivate players and encourage exploration.\n\n"
            "Starting the Game:\n"
            "Must start with character creation.\n"
            "Genre Selection: Ask the player to choose the genre of the game (e.g., Fantasy, Sci-Fi, Historical).\n"
            "Character Naming: Ask the player to name their character.\n"
            "Character Details: Guide the player through a step-by-step process of creating their character, including:\n"
            "- Race: Selecting a race for the character, which will determine their abilities, limitations, and physical appearance.\n"
            "- Class: Choosing a class for the character, which will define their role, skills, and abilities.\n"
            "- Attributes: Assigning attribute scores (Strength, Dexterity, Constitution, Intelligence, Wisdom, and Charisma). Ask if the player would prefer to have scores chosen for them or to choose from a buy system.\n"
            "- Backstory: Developing a brief backstory for the character, which can be used to inform their motivations, relationships, and overall personality.\n"
            "- Starting Spells or Skills: List out potential starting spells or skills and let the player decide what they begin with.\n\n"
            "Game Sheets:\n"
            "Rule Sheet: A comprehensive document outlining the core rules and mechanics of the game.\n"
            "Character Sheet: Includes Character Name, Race, Class, Level, Experience (shown as Current XP/XP Needed), Ability Scores, and Inventory.\n"
            "Party Sheet: Lists all party members with Name, Gender, Race, Class, Level, Experience, and Inventory.\n"
            "Inventory Sheet: Lists Currently Equipped Items and all other items in inventory.\n"
            "Spell Sheet: Shows spell slots available and a list of spells/cantrips the character can cast.\n"
            "Skill Sheet: A list of skills and abilities the character possesses.\n"
            "Quest Sheets:\n"
            "- Main Quest: The overarching storyline, updated as the story progresses.\n"
            "- Current Mission: The specific task or goal the player is currently focused on (could be a sub-task, side quest, or current activity).\n"
            "- Current Location: The player's current location within the game world.\n"
            "Lore Sheets:\n"
            "- Lore Sheet - Characters: A compendium of significant NPCs encountered, including party members and pivotal characters, updated as the player interacts with new individuals.\n"
            "- Lore Sheet - World: An evolving catalog of locations visited or heard of, including geographical features, landmarks, and historical/cultural significance.\n"
            "- Lore Sheet - Races: An exhaustive enumeration of all known races within the game's universe, including unique characteristics, customs, and societal structures.\n\n"
            "Rule Adherence:\n"
            "At any time, the player may ask to see one of the Game Sheets, Quest Sheets or Lore Sheets. Search, update, and show the player the current updated sheet.\n"
            "Reference the Rule Sheet to ensure consistency in gameplay and world-building.\n"
            "Use the rules to guide decisions and resolve conflicts.\n"
            "Be prepared to adapt and modify the rules as needed to accommodate the evolving narrative.\n\n"
            "RULE SHEET:\n\n"
            "Core Rules:\n"
            "1. Character Creation: Six primary attributes (Strength, Dexterity, Constitution, Intelligence, Wisdom, Charisma) determine the character's abilities and limitations. Characters also have a race and class defining abilities and roleplaying potential. Characters begin at level 1 and gain XP through quests, defeating enemies, and overcoming challenges.\n"
            "2. Skill Progression: Characters have skills (Stealth, Perception, Persuasion, etc.) used to perform actions and overcome challenges. Skill checks use a d20 + skill modifier vs. a GM-set Difficulty Class (DC). Skill proficiency increases with experience and practice.\n"
            "3. Immersive Conversations: Conversations between players and NPCs are role-played, with the GM acting as NPCs. The GM responds directly to the player's input without repeating player statements.\n"
            "4. Player Agency: Players have significant control over their character's actions and decisions. Choices have consequences, both positive and negative.\n"
            "5. Open-Ended Prompts: The GM uses open-ended prompts to guide the narrative and provide opportunities for player choice. These are used to initiate new actions or scenarios, not during NPC conversations.\n"
            "6. Game Setting: The world is grounded in the specific genre chosen, rich and detailed with a variety of cultures, civilizations, and landscapes.\n"
            "7. Challenges and Consequences: The game presents challenges (combat, puzzles, moral dilemmas). Failure may result in negative consequences such as character death or loss of resources.\n"
            "8. Character Limitations: Characters have finite resources (health points, spell slots, inventory space) and must make strategic decisions about resource usage.\n"
            "9. Dice Rolls: Dice rolls determine outcomes of actions, attacks, skill checks, and ability checks. The GM handles all dice rolls internally and announces the result.\n"
            "10. Internal Dice Rolls: All dice rolls are handled internally by the GM using a random number generator. Players do not have direct control over outcomes.\n"
            "11. Inventory and Resources: Players have a limited inventory and must manage resources carefully. New items can be acquired through quests, exploration, and purchases.\n"
            "12. Health and Damage: Characters have health that decreases when taking damage. When health reaches zero, they are incapacitated or killed. Health recovers through rest, potions, or magical abilities. Different damage types (physical, magical, poison) affect characters differently.\n"
            "13. Mature Themes: The game may contain mature themes such as violence, death, and morally ambiguous choices.\n"
            "14. Day/Night Cycle: The game has a day/night cycle affecting gameplay and NPC behavior. Certain actions may be more difficult or dangerous at night.\n"
            "15. World Detailing: The GM provides detailed descriptions of settings, characters, and events. Players can explore the world and uncover secrets.\n"
            "16. NPC Reactions: NPCs react to the player's actions and choices, influenced by their personality, motivations, and the current situation. Players can build relationships with NPCs.\n"
            "17. Multiple Quest Lines: The game features multiple quest lines (main and side quests). Players can choose which quests to pursue. Completing quests rewards XP, treasure, and reputation.\n"
            "18. Consistent NPCs: NPCs have consistent personalities, motivations, and backstories. The GM tracks NPC information for a cohesive world. NPCs may change behavior based on player actions. Different types of relationships can develop (friendly to antagonistic to romantic), each developed organically.\n"
            "19. Character Leveling: As players gain XP, characters level up, granting new abilities, spells, and features.\n"
            "20. Diverse NPCs: The world is populated with a diverse cast of NPCs with unique names, personalities, motivations, and backstories.\n"
            "21. Combat System: Combat is turn-based with characters acting in initiative order. Attacks use a d20 + attack modifier vs. target's armor class. Damage is calculated based on weapon and armor class.\n"
            "22. Magic System: Spellcasters have a limited number of spell slots. Spell effects vary by spell level and caster ability.\n"
            "23. Skill Challenges: Skill challenges resolve non-combat situations (persuasion, stealth, investigation, crafting) using a d20 + skill modifier vs. a difficulty target.\n"
            "24. Main Story and Side Quests: There is a Main Overarching Story as the backbone of the adventure. Each party member that joins should have their own personal story that can be completed with the player."
        )

        # Create a thread to contain the RPG session
        guild_id = ctx.guild.id if ctx.guild else None
        thread = await _try_create_thread(ctx, f"rpg: {ctx.author.display_name}'s adventure")
        rpg_channel_id = thread.id if thread is not None else ctx.channel.id

        state.ai_threads[rpg_channel_id] = {
            "kind": "rpg",
            "owner_id": uid,
            "guild_id": guild_id,
            "invited_ids": {uid},
            "system_prompt": rpg_system_prompt,
            "character_prompt": "RPG Adventure",
            "history": [],
        }
        await save_ai_threads()

        async def _rpg_join(user, _rpg_channel_id=rpg_channel_id, _dest=thread or ctx.channel):
            t = state.ai_threads.get(_rpg_channel_id)
            if t is not None:
                t["invited_ids"].add(user.id)
                await save_ai_threads()
            await _dest.send(embed=emb("✅ Joined", f"{user.mention} joined the adventure!", C_GREEN))

        if invited_users:
            await _send_invite(ctx, invited_users, title="📨 RPG Adventure Invite", dest=thread or ctx, on_join=_rpg_join)

        # Send initial AI message asking for character configuration
        dest = thread or ctx.channel
        placeholder = await dest.send("🗺️ Starting your adventure...")
        typing_task = asyncio.create_task(keep_typing(dest))

        try:
            async with aiohttp.ClientSession() as session:
                model = get_guild_roleplay_model(guild_id) if guild_id else OLLAMA_MODEL
                messages = [{"role": "system", "content": rpg_system_prompt}]
                full_response = await stream_ollama(session, messages, placeholder, model=model, user_id=uid)
            if not full_response:
                # Rate limited or AI disabled — placeholder explains why.
                state.ai_threads.pop(rpg_channel_id, None)
                await save_ai_threads()
                await refund_cost(uid, "rpg")
                return
            # Add to history with a synthetic user turn so the conversation structure is valid
            t = state.ai_threads.get(rpg_channel_id)
            if t is not None:
                t["history"].append({"role": "user", "content": "Begin the adventure."})
                t["history"].append({"role": "assistant", "content": full_response})
                await save_ai_threads()
            await finalize(placeholder, dest, full_response)
            await dest.send(embed=emb("🗺️ RPG Adventure", "`!reverse` — undo last response · `!invite @user` — add a party member · `!stop` — end the adventure", C_BLUE))
        except aiohttp.ClientError as e:
            state.ai_threads.pop(rpg_channel_id, None)
            await save_ai_threads()
            await refund_cost(uid, "rpg")
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Ollama offline: {e}")
            await placeholder.edit(content="", embed=emb("", "The AI is currently offline", C_RED))
        except Exception as e:
            state.ai_threads.pop(rpg_channel_id, None)
            await save_ai_threads()
            await refund_cost(uid, "rpg")
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"{type(e).__name__}: {e}")
            await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
        finally:
            typing_task.cancel()

    @commands.command(name="invite")
    async def cmd_invite_activity(self, ctx: commands.Context):
        uid = ctx.author.id
        cid = ctx.channel.id

        invited_users = [m for m in ctx.message.mentions if m.id != uid]
        if not invited_users:
            await ctx.send(embed=emb("❌ Usage", "Usage: `!invite @user1 [@user2 ...]`", C_RED))
            return

        # Determine activity type for this channel
        ai_thread = state.ai_threads.get(cid)
        is_ai_host = ai_thread is not None and ai_thread["owner_id"] == uid
        is_puzzle_host = cid in state.active_puzzles and state.active_puzzles[cid].get("user_id") == uid

        if not (is_ai_host or is_puzzle_host):
            await ctx.send(embed=emb(
                "❌ No Active Activity",
                "You must be the host of an active roleplay, RPG, story, ask thread, or puzzle in this channel to invite others.",
                C_RED,
            ))
            return

        if is_ai_host:
            activity_label = {
                "ask": "Ask", "story": "Story",
                "roleplay": "Roleplay", "rpg": "RPG",
            }.get(ai_thread["kind"], ai_thread["kind"].title())
        else:
            activity_label = "Puzzle"

        confirmed_ids = await _wait_for_confirmations(ctx, invited_users, title=f"📨 {activity_label} Invite")
        if not confirmed_ids:
            await ctx.send(embed=emb("📨 No Response", "No one accepted the invite.", C_BLUE))
            return

        joined_names = []
        skipped_names = []
        for inv_uid in confirmed_ids:
            member = ctx.guild.get_member(inv_uid) if ctx.guild else None
            # Check if user is already in an activity in this channel/thread
            already_in_ai = ai_thread is not None and inv_uid in ai_thread["invited_ids"]
            already_in_puzzle = cid in state.active_puzzles and inv_uid in state.active_puzzles[cid].get("invited_ids", set())
            if already_in_ai or already_in_puzzle:
                if member:
                    skipped_names.append(member.display_name)
                continue
            if is_ai_host:
                ai_thread["invited_ids"].add(inv_uid)
                if member and isinstance(ctx.channel, discord.Thread):
                    try:
                        await ctx.channel.add_user(member)
                    except Exception:
                        pass
                await save_ai_threads()
            elif is_puzzle_host:
                state.active_puzzles[cid].setdefault("invited_ids", set()).add(inv_uid)

            if member:
                joined_names.append(member.display_name)

        if skipped_names:
            await ctx.send(embed=emb(
                "⚠️ Already Active",
                f"{', '.join(skipped_names)} already {'has' if len(skipped_names) == 1 else 'have'} an active activity in this channel.",
                C_GOLD,
            ))
        if joined_names:
            await ctx.send(embed=emb("✅ Joined", f"{', '.join(joined_names)} joined the {activity_label.lower()}!", C_GREEN))
        elif not skipped_names:
            await ctx.send(embed=emb("📨 No Response", "No one accepted the invite.", C_BLUE))


    async def _stop_pvp_game(
        self,
        ctx: commands.Context,
        registry: dict,
        emoji_label: str,
        build_display,
        display_kwargs_for=lambda game, uid: {},
        board_edits: list | None = None,
    ) -> str | None:
        """Forfeit a 2-player PvP game (ttt/c4) in this channel.

        Returns the human-readable line to append to the !stop summary, or
        None if there's no matching game / the user isn't in it. Pays out
        the wager pot to the opponent, tags game["last_move"], edits the
        board to a 🏳️ embed asynchronously (the task lands in `board_edits`
        so cmd_stop can wait for it before archiving a game thread), stamps
        the outcome on the game's thread, and removes the game.
        """
        cid = ctx.channel.id
        uid = ctx.author.id
        # `.get`: during the invite window the slot holds a {"pending": True}
        # placeholder with no players yet.
        if cid not in registry or uid not in registry[cid].get("players", ()):
            return None
        game = registry[cid]
        amount = game.get("amount", 0)
        opponent_uid = [p for p in game["players"] if p != uid][0]
        if amount > 0:
            winnings = amount * 2
            await add_balance(opponent_uid, winnings)
            game["last_move"] = f"{ctx.author.display_name} forfeited — opponent wins {winnings:,} 🪙"
            line = f"{emoji_label} (forfeited, opponent wins {winnings:,} 🪙)"
        else:
            game["last_move"] = f"{ctx.author.display_name} forfeited"
            line = f"{emoji_label} (forfeited)"
        display = build_display(game, **display_kwargs_for(game, uid))
        title = f"🏳️ {emoji_label.split(' ', 1)[1]} Forfeited"
        edit = asyncio.create_task(_edit_board(
            ctx.channel, game,
            emb(title, display + f"\n\n**Last move:** {game['last_move']}", C_RED),
        ))
        if board_edits is not None:
            board_edits.append(edit)
        del registry[cid]
        # Stamp the outcome on the thread now; cmd_stop archives it after the
        # ⏹️ Stopped summary goes out (can't send into an archived thread).
        winner_name = _pvp_player_name(game, ctx.guild, opponent_uid)
        await _close_game_thread(
            ctx.channel,
            f"👑 {winner_name} won against {ctx.author.display_name}",
            archive=False,
        )
        return line

    async def _stop_chess_game(self, ctx: commands.Context) -> str | None:
        cid = ctx.channel.id
        uid = ctx.author.id
        game = state.active_chess_games.get(cid)
        if game is None or uid not in (game.get("white_id"), game.get("black_id")):
            return None

        is_white = uid == game["white_id"]
        opponent_id = game["black_id"] if is_white else game["white_id"]
        result = "0-1" if is_white else "1-0"
        amount = int(game.get("amount", 0))
        guild = ctx.guild

        if amount > 0:
            winnings = amount * 2
            await add_balance(opponent_id, winnings)
            await record_gambling_event(guild.id if guild else None, opponent_id, gained=amount)
            await record_gambling_event(guild.id if guild else None, uid, lost=amount)
            line = f"♟️ Chess (forfeited, opponent wins {winnings:,} 🪙)"
        else:
            line = "♟️ Chess (forfeited)"

        final_pgn = game["pgn"].replace('[Result "*"]', f'[Result "{result}"]')

        try:
            board = chess_engine.board_from_fen(game["fen"])
            final_fen = board.fen()
        except Exception:
            board = chess_engine.new_board()
            final_fen = game.get("fen", board.fen())

        report_id: int | None = None
        try:
            report_id = await save_chess_report(
                guild_id=guild.id if guild else None,
                channel_id=cid,
                white_id=game["white_id"],
                black_id=game["black_id"],
                winner_id=opponent_id,
                result=result,
                pgn=final_pgn,
                final_fen=final_fen,
            )
        except Exception as e:
            logging.error(f"chess save_chess_report (forfeit) failed: {e}", exc_info=True)

        try:
            await delete_chess_game(cid)
        except Exception as e:
            logging.error(f"chess delete_chess_game (forfeit) failed: {e}", exc_info=True)
        state.active_chess_games.pop(cid, None)

        view_line = f" View: `!chess view {report_id}`" if report_id is not None else ""
        desc = f"{ctx.author.display_name} forfeited.{view_line}"
        file = None
        try:
            ps, th = equipped_cosmetics(ctx.author.id)
            png = chess_render.render_board_png(
                board, orientation=chess.WHITE, piece_set=ps, theme=th,
            )
            file = discord.File(io.BytesIO(png), filename=BOARD_IMG_FILENAME)
        except RuntimeError as e:
            logging.warning(f"chess render unavailable in forfeit: {e}")

        forfeit_embed = emb("🏳️ Chess Forfeited", desc, C_RED)
        # Awaited (not fire-and-forget): the board must land before cmd_stop
        # archives the game thread — nothing can post into it afterwards.
        if file is not None:
            forfeit_embed.set_image(url=f"attachment://{BOARD_IMG_FILENAME}")
            await _bump_chess_board(ctx.channel, game, forfeit_embed, file=file)
        else:
            await _bump_chess_board(ctx.channel, game, forfeit_embed)

        # Stamp the outcome on the thread now; cmd_stop archives it after the
        # ⏹️ Stopped summary goes out (can't send into an archived thread).
        bot_user = self.bot.user if self.bot is not None else None
        winner_name = _game_result_name(game, opponent_id, guild, bot_user)
        await _close_game_thread(
            ctx.channel,
            f"👑 {winner_name} won against {ctx.author.display_name}",
            archive=False,
        )

        return line

    @commands.command(name="stop", aliases=["quit", "forfeit", "q", "close"])
    async def cmd_stop(self, ctx: commands.Context):
        uid = ctx.author.id
        cid = ctx.channel.id
        stopped: list[str] = []
        close_thread = False
        # Fire-and-forget board edits that must land before a game thread
        # is archived (nothing can be edited in an archived thread).
        board_edits: list[asyncio.Task] = []

        # AI thread (ask/story/roleplay/rpg) — owner closes the thread; an
        # invited user just leaves the group.
        ai_thread = state.ai_threads.get(cid)
        if ai_thread is not None:
            label = {
                "ask": "💬 Ask thread", "story": "📖 Story",
                "roleplay": "🎭 Roleplay", "rpg": "🗺️ RPG",
            }.get(ai_thread["kind"], ai_thread["kind"].title())
            if ai_thread["owner_id"] == uid or is_admin(ctx):
                state.ai_threads.pop(cid, None)
                await save_ai_threads()
                stopped.append(label)
                close_thread = True
            elif uid in ai_thread["invited_ids"]:
                ai_thread["invited_ids"].discard(uid)
                await save_ai_threads()
                stopped.append(f"{label} (left group)")

        # Puzzle: only the host (or an admin) can cancel.
        if cid in state.active_puzzles and (state.active_puzzles[cid]["user_id"] == uid or is_admin(ctx)):
            puzzle = state.active_puzzles.pop(cid)
            if puzzle.get("generating"):
                stopped.append("🧩 Puzzle (cancelled during generation)")
            else:
                stopped.append(f"🧩 Puzzle (answer was `{puzzle['answer']}`)")

        # Blackjack: forfeit drops the wager.
        if uid in state.active_blackjack_games:
            game = state.active_blackjack_games.pop(uid)
            await retire_blackjack_buttons(game)  # the hand's Hit/Stand set is dead now
            stopped.append(f"🃏 Blackjack (forfeited {game['amount']:,} 🪙)")

        # Hangman: only the host stops it; reveal the word.
        if cid in state.active_hangman_games and state.active_hangman_games[cid]["user_id"] == uid:
            game = state.active_hangman_games[cid]
            word = game["word"]
            game["last_move"] = f"{ctx.author.display_name} forfeited. The word was `{word}`"
            board_edits.append(asyncio.create_task(_edit_board(
                ctx.channel, game,
                emb(
                    "🏳️ Hangman Forfeited",
                    build_hangman_display(game) + f"\n\nThe word was `{word}`.\n\n**Last move:** {game['last_move']}",
                    C_RED,
                ),
            )))
            del state.active_hangman_games[cid]
            stopped.append(f"🔤 Hangman (the word was `{word}`)")
            # Rename now, archive with the summary below.
            await _close_game_thread(
                ctx.channel,
                f"🏳️ {ctx.author.display_name} forfeited — the word was {word}",
                archive=False,
            )
            close_thread = True

        # 2-player PvP games — same shape, factored into _stop_pvp_game.
        for line in (
            await self._stop_pvp_game(ctx, state.active_ttt_games, "🎮 Tic-Tac-Toe", build_ttt_display, board_edits=board_edits),
            await self._stop_pvp_game(ctx, state.active_c4_games,  "🟡 Connect 4",   build_c4_display, board_edits=board_edits),
        ):
            if line:
                stopped.append(line)
                close_thread = True

        # Chess has its own state shape (white_id/black_id/fen/pgn) and needs to
        # produce a chess_reports row on forfeit so `!chess view <id>` works.
        # A forfeited game's thread closes with the summary below (the rename
        # already happened inside _stop_chess_game).
        chess_line = await self._stop_chess_game(ctx)
        if chess_line:
            stopped.append(chess_line)
            close_thread = close_thread or isinstance(ctx.channel, discord.Thread)

        # Race: multi-player; pot splits across opponents instead of paying one.
        if cid in state.active_race_games and uid in state.active_race_games[cid]["players"]:
            game = state.active_race_games[cid]
            amount = game.get("amount", 0)
            opponents = [p for p in game["players"] if p != uid]
            del state.active_race_games[cid]
            if amount > 0 and opponents:
                share = amount * len(game["players"]) // len(opponents)
                for opp in opponents:
                    await add_balance(opp, share)
                stopped.append(f"🏇 Race (forfeited, opponent(s) win {share:,} 🪙 each)")
            else:
                stopped.append("🏇 Race (forfeited)")

        if not stopped:
            await ctx.send(embed=emb("⏹️ Nothing to Stop", "No active game or roleplay.", C_GREY))
            return

        await ctx.send(embed=emb("⏹️ Stopped", "\n".join(stopped), C_GREY))

        if close_thread and isinstance(ctx.channel, discord.Thread):
            if board_edits:
                await asyncio.gather(*board_edits, return_exceptions=True)
            try:
                await ctx.channel.edit(archived=True, locked=True)
            except discord.Forbidden:
                # Locking needs Manage Threads; archiving our own thread
                # doesn't — retry without the lock before giving up.
                try:
                    await ctx.channel.edit(archived=True)
                except Exception:
                    log_bot_permission_error(ctx, "Manage Threads")
            except Exception:
                pass

    @commands.command(name="closeall")
    @requires_perm
    async def cmd_closeall(self, ctx: commands.Context):
        """Close every AI thread you own (ask/story/roleplay/rpg) in this guild."""
        if await check_ai_channel(ctx):
            return
        if ctx.guild is None:
            return

        guild_id = ctx.guild.id
        uid = ctx.author.id
        target_ids = [
            tid for tid, t in state.ai_threads.items()
            if t.get("guild_id") == guild_id and t.get("owner_id") == uid
        ]

        if not target_ids:
            await ctx.send(embed=emb(
                "⏹️ Nothing to Close",
                "You have no active AI threads in this server.",
                C_GREY,
            ))
            return

        counts = {"ask": 0, "story": 0, "roleplay": 0, "rpg": 0}
        for tid in target_ids:
            kind = state.ai_threads[tid].get("kind", "")
            counts[kind] = counts.get(kind, 0) + 1
            state.ai_threads.pop(tid, None)
        await save_ai_threads()

        failed = 0
        for tid in target_ids:
            ch = ctx.guild.get_thread(tid)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(tid)
                except Exception:
                    ch = None
            if isinstance(ch, discord.Thread):
                try:
                    await ch.edit(archived=True, locked=True)
                except discord.Forbidden:
                    log_bot_permission_error(ctx, "Manage Threads")
                    failed += 1
                except Exception:
                    failed += 1

        label_map = {"ask": "💬 Ask", "story": "📖 Story", "roleplay": "🎭 Roleplay", "rpg": "🗺️ RPG"}
        breakdown = "\n".join(
            f"{label_map[k]}: **{counts[k]}**"
            for k in ("ask", "story", "roleplay", "rpg") if counts[k]
        )
        summary = f"Closed **{len(target_ids)}** of your AI thread(s)."
        if failed:
            summary += f" ({failed} could not be archived — check bot permissions.)"
        await ctx.send(embed=emb("⏹️ Closed Your AI Threads", f"{summary}\n\n{breakdown}", C_GREEN))


    @commands.command(name="reverse")
    async def cmd_reverse(self, ctx: commands.Context):
        """Pop the last assistant + user message pair from the AI thread's
        history and delete them from the channel. Available to anyone in
        the thread (no @requires_perm gate by design)."""
        t = state.ai_threads.get(ctx.channel.id)
        if t is not None:
            history = t["history"]
        else:
            history = state.channel_histories[ctx.channel.id]
        if not history:
            await ctx.reply(embed=emb("", "No AI response to reverse.", C_RED))
            return
        if history[-1]["role"] == "assistant":
            history.pop()
        if history and history[-1]["role"] == "user":
            history.pop()
        else:
            await ctx.reply(embed=emb("", "No AI response to reverse.", C_RED))
            return
        if t is not None:
            await save_ai_threads()
        # Scan recent messages and delete the bot's last response and the
        # user message that preceded it.
        recent = [m async for m in ctx.channel.history(limit=100)]
        bot_msg = None
        user_msg = None
        for i, msg in enumerate(recent):
            if msg.author == self.bot.user and msg.id != ctx.message.id:
                bot_msg = msg
                for msg2 in recent[i + 1:]:
                    if msg2.author.id == ctx.author.id:
                        user_msg = msg2
                        break
                break
        if bot_msg:
            try:
                await bot_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        if user_msg:
            try:
                await user_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        confirm = await ctx.reply(embed=emb("", "Last AI response removed from history.", C_GREEN))
        asyncio.create_task(_delete_after(confirm, delay=10.0))


    # ── !recap ────────────────────────────────────────────────────────────
    # Daily server recap: pulls today's messages from every channel the
    # @everyone role can read (and that isn't NSFW-flagged) and asks Ollama
    # to summarize the day as a list of short, dry quips. `everyone` tier,
    # but capped at one run per user per guild per 5am-CT day.

    # Total budget fed to the model. ~400 messages OR ~60k chars of context,
    # whichever comes first; oldest messages are dropped when over budget.
    RECAP_MAX_MESSAGES = 400
    RECAP_MAX_CHARS = 60_000
    # Per-channel ceiling so one spammy channel can't eat the whole budget.
    RECAP_PER_CHANNEL_LIMIT = 300

    # Game / gambling / crime command roots (canonical names + aliases) that
    # ARE recap-worthy. A `!`-prefixed message whose first word is in this
    # set is kept; every other `!command` (e.g. !pay, !balance, !daily) is
    # dropped from the transcript before the model sees it — those one-offs
    # aren't recap material and the model would otherwise quip about them.
    # Non-command chat is never touched by this filter.
    RECAP_GAME_COMMANDS = frozenset({
        "blackjack", "bj", "blackj",
        "flip", "coinflip",
        "slots", "slot",
        "scratchoff", "scratch", "scratches", "scratchoffs",
        "steal", "mug", "bankheist",
        "race", "hangman", "hang", "hm", "chess", "ttt", "c4",
        "guess", "g", "lottery", "puzzle",
    })

    RECAP_SYSTEM_PROMPT = (
        "You are writing a daily recap of a Discord server's activity. "
        "You are given the day's chat logs from several channels, in the form "
        "`[#channel] Name: message`. Summarize the day as a bulleted list of "
        "short, dry, deadpan quips — each one or two sentences. Each quip "
        "describes one conversation, exchange, or thing that happened.\n\n"
        "Rules:\n"
        "- Be specific (quote people, name names) ONLY when little was said in "
        "an exchange, or when the literal exchange is funny on its own. "
        "Example: 'Joseph asked if anyone wanted to play RV There Yet in "
        "general. Nick replied \"fuck off.\"'\n"
        "- For longer or substantive conversations, write a looser one-line "
        "summary instead of quoting — e.g. 'A long argument broke out over "
        "whether a hot dog is a sandwich; no conclusion was reached.'\n"
        "- Group by channel only if it reads naturally; otherwise just list "
        "the quips. Use as many quips as the day needs.\n"
        "- Some logged messages are game/gambling commands (`!bj`, "
        "`!scratch`, `!flip`, `!slots`, `!steal`, ...) or game-turn words "
        "(`hit`, `stand`). The logs do NOT show how any game went — no "
        "cards, no dealer, no winner, no payout. So for games, only say "
        "THAT someone played, rolled up per person: 'Xeph played a few "
        "hands of blackjack' or 'cleanmeanbean spent the afternoon on "
        "scratchoffs and flips'. NEVER describe a hit, a hand, a card, who "
        "won, or how much — you do not have that information and would be "
        "making it up. (Real outcomes that matter come from the "
        "<notable_events> block below, not from these commands.)\n"
        "- You may also be given a <notable_events> block of economy/game "
        "facts (lottery wins, broken records, big gambling/crime hauls). "
        "Fold the interesting ones in as their own quips; ignore dull ones. "
        "Do not double-report: if someone's haul is already in "
        "<notable_events>, don't restate the coin amount when quipping "
        "about their gambling — mention the activity, not the number "
        "again.\n"
        "- Do not invent anything not in the logs or notable events. Do not "
        "add a preamble, intro, or closing remark — output only the list.\n"
        "- Keep the tone dry and a little amused. Never enthusiastic."
    )

    # Notable-event trimming for gambling wins / successful crimes. The
    # single biggest of each always shows (even on a quiet day); beyond
    # that, only hauls clearing the floor, up to RECAP_EVENT_MAX total.
    RECAP_EVENT_MAX = 3
    RECAP_EVENT_FLOOR = 25_000

    def _recap_keep_message(self, content: str) -> bool:
        """Decide whether a message belongs in the recap transcript.

        Non-command chat is always kept. A `!`-prefixed message is kept
        only if its command root (first word, lowercased, alias-aware) is
        a game/gambling/crime command — those are recap-worthy activity.
        Every other `!command` (!pay, !balance, !daily, !shop, ...) is a
        one-off utility action and gets dropped, so the model can't quip
        about it. Bare game-turn words like `hit`/`stand` aren't commands,
        so they pass through here; the prompt handles not narrating them.
        """
        if not content.startswith("!"):
            return True
        root = content[1:].split(None, 1)[0].lower() if len(content) > 1 else ""
        return root in self.RECAP_GAME_COMMANDS

    def _recap_channel_visible(self, channel: discord.TextChannel) -> bool:
        """True if the @everyone role can read `channel` and it isn't NSFW."""
        if getattr(channel, "nsfw", False):
            return False
        everyone = channel.guild.default_role
        perms = channel.permissions_for(everyone)
        return perms.read_messages

    async def _build_recap_events_block(self, guild_id: int, today: str) -> str:
        """Assemble the structured 'notable events' context for !recap.

        Three sources, all keyed on data rather than scraped from bot
        embeds:
          • notable_events — records broken + lottery wins logged today.
          • gambling_history — today's biggest gambling net wins.
          • crime_history — today's biggest successful crimes (gained > 0).

        Returns a `<notable_events>...</notable_events>` block (plain lines
        the AI can weave into quips), or "" if nothing notable happened.
        Best-effort: a DB hiccup degrades the recap to chat-only, never
        fails it.
        """
        from src.persistence import load_notable_events_today
        from src.persistence.history import load_crime_history, load_gambling_history
        from src.helpers import _record_label

        lines: list[str] = []
        try:
            events = await load_notable_events_today(guild_id, today)
        except Exception:
            events = []
        for ev in events:
            if ev["kind"] == "lottery_win":
                lines.append(f"{ev['holder_name']} won the lottery ({ev['value']:,} coins)")
            elif ev["kind"] == "record":
                label = _record_label(ev["category"] or "")
                if (ev["category"] or "").startswith("hangman_wins_"):
                    lines.append(
                        f"{ev['holder_name']} set a new server record — "
                        f"{label} ({ev['value']:,} wins)"
                    )
                else:
                    lines.append(
                        f"{ev['holder_name']} set a new server record — "
                        f"{label} ({ev['value']:,} coins)"
                    )

        # crime_history / gambling_history are keyed by plain CT calendar
        # date (not the 5am-rollover day string). They overlap the recap
        # window closely enough; sum across today's 6h buckets. Since
        # migration 0018 the per-bucket dict is keyed (guild_id, uid_str) —
        # only this guild's rows are summed, so a user's activity in
        # another server can't leak into this recap.
        from src.economy import _ct_now
        cal_today = _ct_now().date().isoformat()

        async def _top_gained(loader, label_fn):
            try:
                hist = await loader()
            except Exception:
                return
            by_user: dict[str, int] = {}
            for bucket in hist.get(cal_today, {}).values():
                for (gid, uid_str), rec in bucket.items():
                    if gid != guild_id:
                        continue
                    by_user[uid_str] = by_user.get(uid_str, 0) + int(rec.get("gained", 0))
            ranked = sorted(
                ((u, g) for u, g in by_user.items() if g > 0),
                key=lambda t: t[1], reverse=True,
            )
            if not ranked:
                return
            # The single biggest always shows; subsequent entries only if
            # they clear the floor, capped at RECAP_EVENT_MAX total.
            chosen = [ranked[0]]
            for uid_str, gained in ranked[1:]:
                if len(chosen) >= self.RECAP_EVENT_MAX or gained < self.RECAP_EVENT_FLOOR:
                    break
                chosen.append((uid_str, gained))
            names = await asyncio.gather(*(
                self._recap_resolve_name(guild_id, uid_str) for uid_str, _ in chosen
            ))
            for (uid_str, gained), name in zip(chosen, names):
                lines.append(label_fn(name, gained))

        await _top_gained(
            load_gambling_history,
            lambda name, g: f"{name} won {g:,} coins gambling",
        )
        await _top_gained(
            load_crime_history,
            lambda name, g: f"{name} pulled off {g:,} coins in crime (steal/mug)",
        )

        if not lines:
            return ""
        body = "\n".join(f"- {ln}" for ln in lines)
        return (
            "\n\nThese are notable economy/game events that happened today "
            "(treat them as facts — work the interesting ones into the recap "
            "as quips, skip any that are dull):\n"
            f"<notable_events>\n{body}\n</notable_events>"
        )

    async def _recap_resolve_name(self, guild_id: int, uid_str: str) -> str:
        """Resolve a user id string to a display name.

        The bot runs without the Server Members intent, so the member
        cache is unreliable — `guild.get_member()` returns None for most
        users. Resolution chain: cache → `guild.fetch_member()` (API) →
        `bot.fetch_user()` (API, works for bots and non-members too) →
        bare id as last resort.

        The `fetch_member`/`fetch_user` steps are inlined here rather than
        delegated to `helpers.fetch_member` so each failure is logged with
        its cause — recaps were printing raw ids for a bot account and the
        swallowed exception hid which step (and why) was missing. If the
        bare-id path is still hit, `recap_name_unresolved` in the logs
        says exactly what threw.
        """
        uid = int(uid_str)
        guild = self.bot.get_guild(guild_id) if self.bot else None

        if guild is not None:
            cached = guild.get_member(uid)
            if cached is not None:
                return cached.display_name
            try:
                member = await guild.fetch_member(uid)
                return member.display_name
            except Exception as e:
                member_err = f"{type(e).__name__}: {e}"
        else:
            member_err = "no guild"

        if self.bot is not None:
            try:
                user = await self.bot.fetch_user(uid)
                return user.display_name
            except Exception as e:
                user_err = f"{type(e).__name__}: {e}"
        else:
            user_err = "no bot"

        log.warning(
            "recap_name_unresolved",
            extra={
                "guild_id": guild_id, "user_id": uid,
                "fetch_member_error": member_err, "fetch_user_error": user_err,
            },
        )
        return f"user {uid_str}"

    @commands.command(name="recap", aliases=["dailyrecap"])
    @requires_perm
    async def cmd_recap(self, ctx: commands.Context, *, focus: str = None):
        """Summarize today's server-wide chat as a list of dry quips.

        `!recap` recaps everything; `!recap <topic or @user>` narrows the
        recap to a person or subject. One run per user per guild per day.
        """
        if ctx.guild is None:
            await ctx.send(embed=emb("❌ Servers Only", "`!recap` only works in a server.", C_RED))
            return
        if await check_ai_channel(ctx):
            return

        guild_id = ctx.guild.id
        uid = ctx.author.id
        today = _ct_today()
        key = (guild_id, uid)

        # godmode users and bot admins skip the once-a-day cap entirely —
        # no claim, no rollback, no persisted usage row.
        bypass_cap = uid in state.godmode_users or is_admin(ctx)

        # Gate-and-claim: reserve today's slot synchronously, before any
        # await, so a spam-fired second invocation sees the claim and bails.
        prior = state.recap_usage.get(key)
        if not bypass_cap:
            if prior == today:
                await ctx.send(embed=emb(
                    "🗒️ Already Recapped",
                    f"You've already run `!recap` today. Next one available "
                    f"<t:{next_daily_reset_ts()}:R>.",
                    C_GREY,
                ))
                return
            state.recap_usage[key] = today  # claim

        # Collect today's messages from every @everyone-visible, non-NSFW
        # text channel. cutoff = the most recent 5am-CT reset, in UTC.
        now_ct = datetime.datetime.now(ZoneInfo("America/Chicago"))
        reset_date = now_ct.date()
        if now_ct.hour < 5:
            reset_date -= datetime.timedelta(days=1)
        cutoff = datetime.datetime.combine(
            reset_date, datetime.time(5, 0), tzinfo=ZoneInfo("America/Chicago"),
        ).astimezone(datetime.timezone.utc)

        focus = focus.strip() if focus else None
        placeholder = await ctx.send("🗒️ Reading today's messages...")
        typing_task = asyncio.create_task(keep_typing(ctx.channel))
        try:
            collected: list[tuple[datetime.datetime, str]] = []
            for channel in ctx.guild.text_channels:
                if not self._recap_channel_visible(channel):
                    continue
                me = channel.guild.me
                if me is None or not channel.permissions_for(me).read_message_history:
                    continue
                try:
                    async for msg in channel.history(
                        limit=self.RECAP_PER_CHANNEL_LIMIT, after=cutoff, oldest_first=False,
                    ):
                        if msg.author.bot:
                            continue
                        content = msg.content.strip()
                        if not content:
                            continue
                        if not self._recap_keep_message(content):
                            continue
                        collected.append((
                            msg.created_at,
                            f"[#{channel.name}] {msg.author.display_name}: {content[:300]}",
                        ))
                except discord.Forbidden:
                    continue

            if not collected:
                if not bypass_cap:
                    state.recap_usage[key] = prior  # roll back the claim
                    if prior is None:
                        state.recap_usage.pop(key, None)
                await placeholder.edit(content="", embed=emb(
                    "🗒️ Nothing to Recap",
                    "No messages in any public channel since the 5am reset yet.",
                    C_GREY,
                ))
                return

            # Newest-first budget: keep the most recent messages, drop the
            # oldest when over the message or char cap, then sort chronological.
            collected.sort(key=lambda t: t[0], reverse=True)
            kept: list[str] = []
            total_chars = 0
            for _, line in collected:
                if len(kept) >= self.RECAP_MAX_MESSAGES:
                    break
                if total_chars + len(line) > self.RECAP_MAX_CHARS:
                    break
                kept.append(line)
                total_chars += len(line)
            truncated = len(kept) < len(collected)
            kept.reverse()  # chronological for the model
            transcript = "\n".join(kept)

            # Structured economy/game events (records, lottery wins, top
            # gambling/crime of the day) — keyed on data, not scraped from
            # bot embeds. Appended to whichever prompt branch we take.
            events_block = await self._build_recap_events_block(guild_id, today)

            if focus:
                user_prompt = (
                    f"Here are today's chat logs. Write the recap, but focus "
                    f"ONLY on anything related to: {focus}. Ignore unrelated "
                    f"conversations. If nothing relates to it, say so in one "
                    f"line.\n\n<logs>\n{transcript}\n</logs>{events_block}"
                )
            else:
                user_prompt = (
                    f"Here are today's chat logs. Write the recap.\n\n"
                    f"<logs>\n{transcript}\n</logs>{events_block}"
                )

            messages = [
                {"role": "system", "content": self.RECAP_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

            await placeholder.edit(content="🗒️ Writing the recap...")
            async with aiohttp.ClientSession() as session:
                model = get_guild_ask_model(guild_id)
                recap = await stream_ollama(
                    session, messages, placeholder,
                    guild_id=guild_id, model=model, user_id=uid,
                )
            if not recap:
                # AI disabled / rate-limited — placeholder already explains.
                # Refund the daily slot so the user isn't burned for nothing.
                if not bypass_cap:
                    state.recap_usage[key] = prior
                    if prior is None:
                        state.recap_usage.pop(key, None)
                return

            header = f"🗒️ **Daily Recap — {today}**"
            if focus:
                header += f" · focus: *{focus[:80]}*"
            footer = ""
            if truncated:
                footer = (
                    f"\n\n*Recapped the most recent {len(kept)} of "
                    f"{len(collected)} messages — earlier ones were trimmed.*"
                )
            await finalize(placeholder, ctx.channel, f"{header}\n\n{recap}{footer}")

            # Commit the daily-cap claim now that the recap actually landed.
            if not bypass_cap:
                await save_recap_usage(guild_id, uid, today)
        except aiohttp.ClientError as e:
            if not bypass_cap:
                state.recap_usage[key] = prior
                if prior is None:
                    state.recap_usage.pop(key, None)
            _log_audit(f"{ctx.author.display_name} ({uid})", ctx.message.content[:100], f"Ollama offline: {e}")
            await placeholder.edit(content="", embed=emb("", "The AI is currently offline.", C_RED))
        except Exception as e:
            if not bypass_cap:
                state.recap_usage[key] = prior
                if prior is None:
                    state.recap_usage.pop(key, None)
            _log_audit(f"{ctx.author.display_name} ({uid})", ctx.message.content[:100], f"{type(e).__name__}: {e}")
            await placeholder.edit(content=f"⚠️ Something went wrong: `{e}`")
        finally:
            typing_task.cancel()


async def setup(bot):
    await bot.add_cog(AICog(bot))
