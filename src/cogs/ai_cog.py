import asyncio

import aiohttp
import discord
from discord.ext import commands

from src.helpers import (
    emb, C_GREEN, C_RED, C_GOLD, C_BLUE, C_GREY,
    _edit_board, _delete_after, _log_audit, log_bot_permission_error,
)
from src.economy import (
    add_balance, get_guild_ask_model, get_guild_roleplay_model,
)
from src.permissions import (
    is_admin, check_rate_limit,
    check_ai_channel,
)
from src.persistence import (
    save_chess_games, save_ai_threads
)
from src.guild_config import get_guild_cfg
from src.ai import (
    enforce_cost, keep_typing,
    stream_ollama, finalize, respond,
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
from src.games.chess import build_chess_display
from src.games.hangman import build_hangman_display


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
        if check_rate_limit(ctx.author.id):
            await ctx.send("⚠️ Slow down! Please wait a moment before sending another message.")
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
        if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
            thread = await ctx.message.create_thread(name=f"ask: {question[:80]}")
            state.ai_threads[thread.id] = {
                "kind": "ask",
                "owner_id": ctx.author.id,
                "guild_id": guild_id,
                "invited_ids": {ctx.author.id},
                "system_prompt": ASK_SYSTEM_PROMPT,
                "character_prompt": None,
                "history": [],
            }
            await respond(thread, ctx.author.id, question, ctx.message, system_prompt=system_prompt, guild_id=guild_id, author_name=ctx.author.display_name)
            await thread.send(embed=emb("💬 Ask Thread", "Keep talking — I'll remember the conversation.\n`!invite @user` — let someone else join · `!stop` — end the thread", C_BLUE))
        else:
            await respond(ctx.channel, ctx.author.id, question, ctx.message, system_prompt=system_prompt, guild_id=guild_id, author_name=ctx.author.display_name)


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
        if check_rate_limit(ctx.author.id):
            await ctx.send("⚠️ Slow down! Please wait a moment before sending another message.")
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

        if not await enforce_cost(ctx, "story"):
            return

        thread_label = alias_name.capitalize() if alias_name else "Story"
        guild_id = ctx.guild.id if ctx.guild else None
        if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
            thread = await ctx.message.create_thread(name=f"{thread_label}: {clean_prompt[:75]}")
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

            await respond(thread, uid, clean_prompt, ctx.message, system_prompt=system_prompt, guild_id=guild_id)
            await thread.send(embed=emb(f"📖 {thread_label} Started", "`!continue` — next chapter · `!reverse` — undo last response · `!invite @user` — add a co-author · `!stop` — end the story", C_BLUE))
        else:
            await respond(ctx.channel, uid, clean_prompt, ctx.message, system_prompt=system_prompt, guild_id=guild_id)


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
        if check_rate_limit(ctx.author.id):
            await ctx.send("⚠️ Slow down! Please wait a moment before sending another message.")
            return

        uid = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        t = state.ai_threads.get(ctx.channel.id)
        history = t["history"] if t else state.channel_histories[ctx.channel.id]

        if not history:
            await ctx.send(embed=emb("❌ Nothing to Continue", "No story to continue in this thread.", C_RED))
            return

        if not await enforce_cost(ctx, "continue"):
            return

        sp = t.get("system_prompt") if t else STORY_SYSTEM_PROMPT
        await respond(ctx.channel, uid, "Continue the story.", ctx.message, system_prompt=sp, guild_id=guild_id)


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

        tldr_prompt = [
            {"role": "system", "content": "You are a concise summarizer. Summarize the following story excerpt in 2-3 sentences, capturing the key events and mood. Do not editorialize or add commentary — just summarize."},
            {"role": "user", "content": f"Summarize this:\n\n{last_text}"},
        ]

        placeholder = await ctx.channel.send("📝 Summarizing...")
        typing_task = asyncio.create_task(keep_typing(ctx.channel))
        try:
            async with aiohttp.ClientSession() as session:
                model = get_guild_ask_model(guild_id) if guild_id else OLLAMA_MODEL
                summary = await stream_ollama(session, tldr_prompt, placeholder, guild_id=guild_id, model=model)
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

        if not await enforce_cost(ctx, "roleplay"):
            return

        # Create a thread to contain the roleplay
        guild_id = ctx.guild.id if ctx.guild else None
        if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
            thread = await ctx.message.create_thread(name=f"roleplay: {clean_prompt[:70]}")
            rp_channel_id = thread.id
        else:
            thread = None
            rp_channel_id = ctx.channel.id

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
        if ctx.guild and isinstance(ctx.channel, discord.TextChannel):
            thread = await ctx.message.create_thread(name=f"rpg: {ctx.author.display_name}'s adventure")
            rpg_channel_id = thread.id
        else:
            thread = None
            rpg_channel_id = ctx.channel.id

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
                full_response = await stream_ollama(session, messages, placeholder, model=model)

            # Add to history with a synthetic user turn so the conversation structure is valid
            t = state.ai_threads.get(rpg_channel_id)
            if t is not None:
                t["history"].append({"role": "user", "content": "Begin the adventure."})
                t["history"].append({"role": "assistant", "content": full_response})
                await save_ai_threads()
            await finalize(placeholder, dest, full_response)
            await dest.send(embed=emb("🗺️ RPG Adventure", "`!reverse` — undo last response · `!invite @user` — add a party member · `!stop` — end the adventure", C_BLUE))
        except aiohttp.ClientError as e:
            _log_audit(f"{ctx.author.display_name} ({ctx.author.id})", ctx.message.content[:100], f"Ollama offline: {e}")
            await placeholder.edit(content="", embed=emb("", "The AI is currently offline", C_RED))
        except Exception as e:
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
    ) -> str | None:
        """Forfeit a 2-player PvP game (ttt/c4/chess) in this channel.

        Returns the human-readable line to append to the !stop summary, or
        None if there's no matching game / the user isn't in it. Pays out
        the wager pot to the opponent, tags game["last_move"], edits the
        board to a 🏳️ embed asynchronously, and removes the game.
        """
        cid = ctx.channel.id
        uid = ctx.author.id
        if cid not in registry or uid not in registry[cid]["players"]:
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
        asyncio.create_task(_edit_board(
            ctx.channel, game,
            emb(title, display + f"\n\n**Last move:** {game['last_move']}", C_RED),
        ))
        del registry[cid]
        return line

    @commands.command(name="stop", aliases=["quit", "forfeit", "q"])
    async def cmd_stop(self, ctx: commands.Context):
        uid = ctx.author.id
        cid = ctx.channel.id
        stopped: list[str] = []
        close_thread = False

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
            amount = state.active_blackjack_games[uid]["amount"]
            del state.active_blackjack_games[uid]
            stopped.append(f"🃏 Blackjack (forfeited {amount:,} 🪙)")

        # Hangman: only the host stops it; reveal the word.
        if cid in state.active_hangman_games and state.active_hangman_games[cid]["user_id"] == uid:
            game = state.active_hangman_games[cid]
            word = game["word"]
            game["last_move"] = f"{ctx.author.display_name} forfeited. The word was `{word}`"
            asyncio.create_task(_edit_board(
                ctx.channel, game,
                emb(
                    "🏳️ Hangman Forfeited",
                    build_hangman_display(game) + f"\n\nThe word was `{word}`.\n\n**Last move:** {game['last_move']}",
                    C_RED,
                ),
            ))
            del state.active_hangman_games[cid]
            stopped.append(f"🔤 Hangman (the word was `{word}`)")

        # 2-player PvP games — same shape, factored into _stop_pvp_game.
        for line in (
            await self._stop_pvp_game(ctx, state.active_ttt_games, "🎮 Tic-Tac-Toe", build_ttt_display),
            await self._stop_pvp_game(ctx, state.active_c4_games,  "🟡 Connect 4",   build_c4_display),
            await self._stop_pvp_game(
                ctx, state.active_chess_games, "♟️ Chess",
                lambda game, **k: build_chess_display(game["board"], **k),
                display_kwargs_for=lambda game, uid: {"is_black_perspective": uid == game["players"][1]},
            ),
        ):
            if line:
                stopped.append(line)

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

        await save_chess_games()
        await ctx.send(embed=emb("⏹️ Stopped", "\n".join(stopped), C_GREY))

        if close_thread and isinstance(ctx.channel, discord.Thread):
            try:
                await ctx.channel.edit(archived=True, locked=True)
            except discord.Forbidden:
                log_bot_permission_error(ctx.guild, "Manage Threads")
            except Exception:
                pass


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


async def setup(bot):
    await bot.add_cog(AICog(bot))
