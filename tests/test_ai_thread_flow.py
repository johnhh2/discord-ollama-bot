"""End-to-end coverage for the AI thread + reply chain.

Walks the user-facing flow:
  !ask / !story / !roleplay / !rpg
    → cog creates a Discord thread (FakeThread)
    → registers state.ai_threads[thread.id]
    → calls respond() to post the first AI message

  user posts in the thread (no `!` prefix)
    → events.on_message routes it via in_ai_thread → respond()

  !continue, !tldr, !invite, !reverse
    → mutate the thread's history / invited_ids / message log

  !stop (covered by tests/test_cmd_stop.py — not duplicated here)

Mocking strategy:
- `respond()` is stubbed at the seam — it's the last function before
  Ollama HTTP and Discord API touches. Tests assert on what `respond()`
  was called with (channel, content, system_prompt) instead of trying
  to mock the streaming JSON cycle.
- For !tldr we stub `stream_ollama` directly because the cog calls it
  inline rather than via respond().
- FakeThread (added in tests/fakes/discord.py) satisfies
  `isinstance(_, discord.Thread)` so the cog picks the thread branch.
- Conftest already stubs save_ai_threads, so registry mutations land
  in state.ai_threads without DB I/O.
"""

import pytest

import discord
import src.state as _state
import src.cogs.ai_cog as _ai_cog
from src.cogs.ai_cog import AICog

from tests.fakes.discord import (
    FakeMember, FakeGuild, FakeTextChannel, FakeThread, FakeMessage, FakeCtx,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _stub_respond_and_costs(monkeypatch):
    """Stub respond() so tests don't need a real Ollama; record calls.

    Stub enforce_cost so balance gates don't block tests. The token-bucket
    limit lives in stream_ollama, which respond() is stubbed past, so it
    doesn't fire here either. Also stub _send_invite (used by
    story/rp/rpg branches) so the background reaction-listener task isn't
    spawned.
    """
    respond_calls = []

    async def _stub_respond(channel, uid, content, reply_to, **kwargs):
        respond_calls.append({
            "channel": channel, "uid": uid, "content": content,
            "reply_to": reply_to, **kwargs,
        })
        # Mimic the real respond(): append a synthetic assistant turn so
        # follow-up commands (e.g. !tldr, !continue) see history.
        ai_thread = _state.ai_threads.get(channel.id)
        if ai_thread is not None:
            ai_thread["history"].append({"role": "user", "content": content})
            ai_thread["history"].append({"role": "assistant", "content": "[stub-response]"})
    monkeypatch.setattr(_ai_cog, "respond", _stub_respond)

    async def _stub_enforce_cost(ctx, feature):
        return True
    monkeypatch.setattr(_ai_cog, "enforce_cost", _stub_enforce_cost)

    async def _stub_send_invite(*args, **kwargs):
        return None
    monkeypatch.setattr(_ai_cog, "_send_invite", _stub_send_invite)

    # check_ai_channel reads cfg["ai_channels"]; default behavior (empty list
    # → no restriction) is fine, but mock to short-circuit guild lookups.
    async def _stub_check_ai_channel(ctx):
        return False
    monkeypatch.setattr(_ai_cog, "check_ai_channel", _stub_check_ai_channel)

    # ai_cog.py captures `save_ai_threads` at import time via
    # `from src.persistence import save_ai_threads`. Conftest patches
    # `_persistence.save_ai_threads` but that doesn't update the bound
    # reference in ai_cog. Stub directly here.
    async def _stub_save_ai_threads(*a, **kw):
        return None
    monkeypatch.setattr(_ai_cog, "save_ai_threads", _stub_save_ai_threads)

    return respond_calls


def _make_ctx_with_text_channel(author: FakeMember, message_content: str = "!cmd",
                                channel_id: int = 100) -> FakeCtx:
    """FakeCtx whose channel is a real-typed FakeTextChannel (so the
    `isinstance(ctx.channel, discord.TextChannel)` check in cmd_ask
    picks the create-thread branch)."""
    guild = FakeGuild(gid=42)
    guild.members = [author]
    channel = FakeTextChannel(ch_id=channel_id, name="test")
    ctx = FakeCtx(author=author, guild=guild, channel=channel)
    ctx.message = FakeMessage(content=message_content, author=author, message_id=999)
    return ctx


def _make_ctx_with_thread(author: FakeMember, thread_id: int = 200) -> tuple[FakeCtx, FakeThread]:
    """FakeCtx whose channel is a FakeThread (for cmd_continue / cmd_tldr)."""
    guild = FakeGuild(gid=42)
    guild.members = [author]
    thread = FakeThread(thread_id=thread_id)
    ctx = FakeCtx(author=author, guild=guild, channel=thread)
    ctx.message = FakeMessage(content="!continue", author=author, message_id=999)
    return ctx, thread


# ── !ask ──────────────────────────────────────────────────────────────────────

async def test_ask_in_text_channel_creates_thread_and_seeds_state(_stub_respond_and_costs):
    cog = AICog(bot=None)
    asker = FakeMember(uid=1001, display_name="asker")
    ctx = _make_ctx_with_text_channel(asker, message_content="!ask hello")

    # Pre-populate channel.history (cmd_ask reads last 10 messages for context)
    ctx.channel.history = lambda limit=11: _empty_async_iter()

    await cog.cmd_ask.callback(cog, ctx, question="What is AI?")

    # ctx.message.create_thread was called.
    ctx.message.create_thread.assert_awaited_once()
    # The new thread is registered in ai_threads with the right metadata.
    threads = list(_state.ai_threads.values())
    assert len(threads) == 1
    t = threads[0]
    assert t["kind"] == "ask"
    assert t["owner_id"] == asker.id
    assert t["invited_ids"] == {asker.id}
    assert t["history"] == [
        {"role": "user", "content": "What is AI?"},
        {"role": "assistant", "content": "[stub-response]"},
    ]
    # respond() was called with the new thread (not the original channel).
    assert len(_stub_respond_and_costs) == 1
    call = _stub_respond_and_costs[0]
    assert call["content"] == "What is AI?"
    assert call["uid"] == asker.id
    # The "Keep talking" header embed was sent in the thread after respond.
    thread = list(_state.ai_threads.keys())[0]
    fake_thread = call["channel"]
    assert fake_thread.id == thread
    fake_thread.send.assert_awaited()


async def test_ask_skips_thread_creation_when_user_is_out_of_tokens(_stub_respond_and_costs):
    """Drained-bucket users hit the rate-limit embed before any thread is
    created or coin cost is deducted. Otherwise we'd leave an empty Discord
    thread the user can't actually use until the bucket refills."""
    import src.ai as _ai
    cog = AICog(bot=None)
    asker = FakeMember(uid=1003, display_name="broke")
    ctx = _make_ctx_with_text_channel(asker, message_content="!ask hello")
    ctx.channel.history = lambda limit=11: _empty_async_iter()

    # Drain the bucket directly. (godmode_users is reset by the global
    # fixture, so this user isn't a bypassing admin.)
    import time as _t
    _ai._user_token_buckets[asker.id] = (0.0, _t.monotonic())
    try:
        await cog.cmd_ask.callback(cog, ctx, question="What is AI?")
    finally:
        _ai._user_token_buckets.clear()

    # No thread, no respond() call, no coin deduction.
    ctx.message.create_thread.assert_not_awaited()
    assert _state.ai_threads == {}
    assert _stub_respond_and_costs == []
    # User was told why.
    assert any(
        getattr(m, "title", "") == "⏳ AI Rate Limit"
        for m in ctx.sent_embeds
    )


async def test_ask_with_no_question_replies_usage(_stub_respond_and_costs):
    cog = AICog(bot=None)
    asker = FakeMember(uid=1002)
    ctx = _make_ctx_with_text_channel(asker)

    await cog.cmd_ask.callback(cog, ctx, question=None)

    # No thread, no respond() call.
    assert _state.ai_threads == {}
    assert _stub_respond_and_costs == []
    # Ctx received the usage message.
    assert any("Usage" in m for m in ctx.sent_messages)


def _empty_async_iter():
    """Helper for FakeTextChannel.history() when the cog needs an iterator
    but the channel has no prior messages."""
    class _It:
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration
    return _It()


# ── !story ────────────────────────────────────────────────────────────────────

async def test_story_creates_thread_with_story_kind_and_default_prompt(_stub_respond_and_costs):
    cog = AICog(bot=None)
    author = FakeMember(uid=2001)
    ctx = _make_ctx_with_text_channel(author, message_content="!story Two robots")

    await cog.cmd_story.callback(cog, ctx, prompt="Two robots arguing over a kettle")

    threads = list(_state.ai_threads.values())
    assert len(threads) == 1
    t = threads[0]
    assert t["kind"] == "story"
    assert t["owner_id"] == author.id
    # Default !story uses STORY_SYSTEM_PROMPT (not a custom alias).
    assert t["system_prompt"] == _ai_cog.STORY_SYSTEM_PROMPT
    # respond() was called with the same prompt as system_prompt.
    call = _stub_respond_and_costs[0]
    assert call["system_prompt"] == _ai_cog.STORY_SYSTEM_PROMPT


async def test_story_with_no_prompt_emits_usage_with_alias_hint(monkeypatch, _stub_respond_and_costs):
    """When the guild has story aliases registered, the usage hint lists them."""
    cog = AICog(bot=None)
    author = FakeMember(uid=2002)
    ctx = _make_ctx_with_text_channel(author)

    # Seed a story_alias on this guild.
    monkeypatch.setattr(
        _ai_cog, "get_guild_cfg",
        lambda gid: {"story_aliases": {"fanfic": "edgy prompt", "scifi": "hard scifi"}},
    )

    await cog.cmd_story.callback(cog, ctx, prompt=None)

    # No thread created.
    assert _state.ai_threads == {}
    # Sent message includes the usage line + alias names.
    sent = " ".join(ctx.sent_messages)
    assert "Usage:" in sent
    assert "!fanfic" in sent and "!scifi" in sent


# ── on_command_error story alias listener ────────────────────────────────────

async def test_story_alias_listener_routes_to_story_with_custom_prompt(monkeypatch, _stub_respond_and_costs):
    """Typing `!fanfic <prompt>` (where fanfic is a registered alias) should
    invoke the listener, which calls _story_with_prompt with the custom
    prompt. Tested by directly calling the listener — discord.py would
    normally fire it on CommandNotFound."""
    from discord.ext import commands as dpy_commands

    cog = AICog(bot=None)
    author = FakeMember(uid=3001)
    ctx = _make_ctx_with_text_channel(author, message_content="!fanfic Batman vs Superman")
    # Listener reads ctx.message.content (already set to !fanfic ...).

    monkeypatch.setattr(
        _ai_cog, "get_guild_cfg",
        lambda gid: {"story_aliases": {"fanfic": "the edgy custom prompt"}},
    )

    err = dpy_commands.CommandNotFound("fanfic")
    await cog.on_command_error(ctx, err)

    # Thread created with the custom prompt.
    assert len(_state.ai_threads) == 1
    t = list(_state.ai_threads.values())[0]
    assert t["kind"] == "story"
    assert t["system_prompt"] == "the edgy custom prompt"
    # And respond() was called with the custom prompt, not the default story one.
    call = _stub_respond_and_costs[0]
    assert call["system_prompt"] == "the edgy custom prompt"


async def test_story_alias_listener_ignores_unknown_word(monkeypatch, _stub_respond_and_costs):
    """A CommandNotFound for a word that ISN'T a registered alias must not
    create a thread — the listener should silently return."""
    from discord.ext import commands as dpy_commands

    cog = AICog(bot=None)
    author = FakeMember(uid=3002)
    ctx = _make_ctx_with_text_channel(author, message_content="!notarealcmd hello")

    monkeypatch.setattr(_ai_cog, "get_guild_cfg", lambda gid: {"story_aliases": {}})

    await cog.on_command_error(ctx, dpy_commands.CommandNotFound("notarealcmd"))

    assert _state.ai_threads == {}
    assert _stub_respond_and_costs == []


async def test_story_alias_listener_ignores_non_command_not_found(monkeypatch, _stub_respond_and_costs):
    """If the error is some other discord.py exception, the listener returns."""
    from discord.ext import commands as dpy_commands

    cog = AICog(bot=None)
    author = FakeMember(uid=3003)
    ctx = _make_ctx_with_text_channel(author, message_content="!fanfic stuff")

    monkeypatch.setattr(
        _ai_cog, "get_guild_cfg",
        lambda gid: {"story_aliases": {"fanfic": "some prompt"}},
    )

    # Listener should ignore CheckFailure et al.
    await cog.on_command_error(ctx, dpy_commands.CheckFailure("perms"))

    assert _state.ai_threads == {}


# ── !roleplay & !rpg ─────────────────────────────────────────────────────────

async def test_roleplay_creates_thread_with_character_prompt(_stub_respond_and_costs):
    cog = AICog(bot=None)
    author = FakeMember(uid=4001)
    ctx = _make_ctx_with_text_channel(author, message_content="!roleplay Sherlock Holmes")

    await cog.cmd_roleplay.callback(cog, ctx, character_prompt="Sherlock Holmes")

    threads = list(_state.ai_threads.values())
    assert len(threads) == 1
    t = threads[0]
    assert t["kind"] == "roleplay"
    assert t["character_prompt"] == "Sherlock Holmes"
    # roleplay system_prompt embeds the character.
    assert "Sherlock Holmes" in t["system_prompt"]
    assert "stay in character" in t["system_prompt"].lower()


async def test_rpg_creates_thread_with_rpg_kind(monkeypatch, _stub_respond_and_costs):
    """cmd_rpg calls stream_ollama directly (not via respond()) — stub it
    AND finalize/keep_typing so the test doesn't try to reach Ollama."""
    cog = AICog(bot=None)
    author = FakeMember(uid=4002)
    ctx = _make_ctx_with_text_channel(author, message_content="!rpg")

    async def _stub_stream(session, messages, placeholder, **kwargs):
        return "Welcome to the adventure! What's your character's name?"
    monkeypatch.setattr(_ai_cog, "stream_ollama", _stub_stream)
    async def _stub_finalize(*a, **kw):
        return None
    monkeypatch.setattr(_ai_cog, "finalize", _stub_finalize)
    async def _no_typing(*a, **kw):
        return None
    monkeypatch.setattr(_ai_cog, "keep_typing", _no_typing)

    await cog.cmd_rpg.callback(cog, ctx)

    threads = list(_state.ai_threads.values())
    assert len(threads) == 1
    t = threads[0]
    assert t["kind"] == "rpg"
    # The long RPG prompt mentions character creation; sanity check it's there.
    assert "character" in t["system_prompt"].lower()
    # cmd_rpg seeds the history with a synthetic (user, assistant) pair.
    assert len(t["history"]) == 2
    assert t["history"][0]["role"] == "user"
    assert t["history"][1]["role"] == "assistant"
    assert "adventure" in t["history"][1]["content"].lower()


# ── !continue ────────────────────────────────────────────────────────────────

async def test_continue_in_thread_uses_stored_system_prompt(_stub_respond_and_costs):
    cog = AICog(bot=None)
    author = FakeMember(uid=5001)
    ctx, thread = _make_ctx_with_thread(author, thread_id=505)

    # Seed an active story thread with the edgy custom prompt (alias case).
    _state.ai_threads[505] = {
        "kind": "story",
        "owner_id": author.id,
        "guild_id": 42,
        "invited_ids": {author.id},
        "system_prompt": "the edgy custom prompt",
        "character_prompt": None,
        "history": [
            {"role": "user", "content": "Two robots arguing over a kettle"},
            {"role": "assistant", "content": "Once upon a time..."},
        ],
    }

    await cog.cmd_continue.callback(cog, ctx)

    # respond() was called with the thread's stored system prompt — NOT the
    # default STORY_SYSTEM_PROMPT. This is the regression Phase 3 of round-1
    # fixed (cmd_continue used to hardcode FANFIC_SYSTEM_PROMPT).
    call = _stub_respond_and_costs[0]
    assert call["system_prompt"] == "the edgy custom prompt"
    assert call["content"] == "Continue the story."


async def test_continue_outside_thread_replies_threads_only(_stub_respond_and_costs):
    cog = AICog(bot=None)
    author = FakeMember(uid=5002)
    ctx = _make_ctx_with_text_channel(author)

    await cog.cmd_continue.callback(cog, ctx)

    assert _stub_respond_and_costs == []
    embed = ctx.sent_embeds[0]
    assert "Threads Only" in embed.title


async def test_continue_with_empty_history_replies_nothing_to_continue(_stub_respond_and_costs):
    cog = AICog(bot=None)
    author = FakeMember(uid=5003)
    ctx, thread = _make_ctx_with_thread(author, thread_id=507)
    _state.ai_threads[507] = {
        "kind": "story", "owner_id": author.id, "invited_ids": {author.id},
        "system_prompt": "x", "character_prompt": None, "history": [],
    }

    await cog.cmd_continue.callback(cog, ctx)

    assert _stub_respond_and_costs == []
    embed = ctx.sent_embeds[0]
    assert "Nothing to Continue" in embed.title


# ── !tldr ────────────────────────────────────────────────────────────────────

async def test_tldr_summarizes_last_assistant_response(monkeypatch, _stub_respond_and_costs):
    """!tldr calls stream_ollama directly (not via respond()). Stub it."""
    cog = AICog(bot=None)
    author = FakeMember(uid=6001)
    ctx, thread = _make_ctx_with_thread(author, thread_id=600)
    _state.ai_threads[600] = {
        "kind": "story", "owner_id": author.id, "invited_ids": {author.id},
        "system_prompt": "x", "character_prompt": None,
        "history": [
            {"role": "user", "content": "robots and kettles"},
            {"role": "assistant", "content": "A long story about robots and kettles, ending in tea."},
        ],
    }

    summary_calls = []
    async def _stub_stream(session, messages, placeholder, **kwargs):
        summary_calls.append(messages)
        return "TL;DR: robots argued over tea, made up."
    monkeypatch.setattr(_ai_cog, "stream_ollama", _stub_stream)

    # keep_typing spawns a background task; stub to avoid task warnings.
    async def _no_typing(*a, **kw):
        return
    monkeypatch.setattr(_ai_cog, "keep_typing", _no_typing)

    await cog.cmd_tldr.callback(cog, ctx)

    # stream_ollama was called with a summarizer system prompt + the last
    # assistant response as the user-content.
    assert len(summary_calls) == 1
    msgs = summary_calls[0]
    assert msgs[0]["role"] == "system"
    assert "summariz" in msgs[0]["content"].lower()
    assert "robots and kettles" in msgs[1]["content"]


async def test_tldr_with_no_assistant_response_replies_nothing_to_summarize(_stub_respond_and_costs):
    cog = AICog(bot=None)
    author = FakeMember(uid=6002)
    ctx, thread = _make_ctx_with_thread(author, thread_id=601)
    _state.ai_threads[601] = {
        "kind": "story", "owner_id": author.id, "invited_ids": {author.id},
        "system_prompt": "x", "character_prompt": None,
        "history": [{"role": "user", "content": "just a user msg"}],
    }

    await cog.cmd_tldr.callback(cog, ctx)

    embed = ctx.sent_embeds[0]
    assert "Nothing to Summarize" in embed.title


async def test_tldr_outside_thread_replies_threads_only(_stub_respond_and_costs):
    cog = AICog(bot=None)
    author = FakeMember(uid=6003)
    ctx = _make_ctx_with_text_channel(author)

    await cog.cmd_tldr.callback(cog, ctx)

    embed = ctx.sent_embeds[0]
    assert "Threads Only" in embed.title


# ── !invite ──────────────────────────────────────────────────────────────────

async def test_invite_adds_user_to_thread_and_invited_ids(monkeypatch, _stub_respond_and_costs):
    """Owner runs !invite @alice in their AI thread → alice is added to
    invited_ids and to the Discord thread (via thread.add_user)."""
    cog = AICog(bot=None)
    owner = FakeMember(uid=7001)
    alice = FakeMember(uid=7002, display_name="alice")
    ctx, thread = _make_ctx_with_thread(owner, thread_id=700)
    ctx.guild.members = [owner, alice]
    ctx.message.mentions = [alice]

    _state.ai_threads[700] = {
        "kind": "story", "owner_id": owner.id, "invited_ids": {owner.id},
        "system_prompt": "x", "character_prompt": None, "history": [],
    }

    # _wait_for_confirmations: pretend alice clicked ✅ on the invite embed.
    async def _stub_wait(ctx_arg, invited_users, **kwargs):
        return {alice.id}
    monkeypatch.setattr(_ai_cog, "_wait_for_confirmations", _stub_wait)

    await cog.cmd_invite_activity.callback(cog, ctx)

    assert alice.id in _state.ai_threads[700]["invited_ids"]
    thread.add_user.assert_awaited_once_with(alice)
    # "Joined" embed was posted.
    assert any("Joined" in (e.title or "") for e in ctx.sent_embeds)


async def test_invite_without_mentions_replies_usage(_stub_respond_and_costs):
    cog = AICog(bot=None)
    owner = FakeMember(uid=7003)
    ctx, thread = _make_ctx_with_thread(owner, thread_id=701)
    _state.ai_threads[701] = {
        "kind": "story", "owner_id": owner.id, "invited_ids": {owner.id},
        "system_prompt": "x", "character_prompt": None, "history": [],
    }
    ctx.message.mentions = []

    await cog.cmd_invite_activity.callback(cog, ctx)

    embed = ctx.sent_embeds[0]
    assert "Usage" in embed.title


async def test_invite_when_not_host_replies_no_active_activity(monkeypatch, _stub_respond_and_costs):
    """Non-owner can't invite to a thread they don't host."""
    cog = AICog(bot=None)
    owner = FakeMember(uid=7004)
    bystander = FakeMember(uid=7005)
    target = FakeMember(uid=7006)
    ctx, thread = _make_ctx_with_thread(bystander, thread_id=702)
    ctx.guild.members = [owner, bystander, target]
    ctx.message.mentions = [target]

    _state.ai_threads[702] = {
        "kind": "story", "owner_id": owner.id, "invited_ids": {owner.id},
        "system_prompt": "x", "character_prompt": None, "history": [],
    }

    await cog.cmd_invite_activity.callback(cog, ctx)

    embed = ctx.sent_embeds[0]
    assert "No Active Activity" in embed.title
    assert target.id not in _state.ai_threads[702]["invited_ids"]


# ── on_message routing into an AI thread (events.py side) ─────────────────────

async def test_message_in_ai_thread_routes_to_respond_for_invited_user(monkeypatch):
    """A non-`!`-prefixed message in a registered AI thread routes to
    respond() — *if* the author is in invited_ids. Tested via the
    EventsCog.on_message dispatcher (not the cmd_X callback)."""
    import src.events as _events
    from src.events import EventsCog

    class _BotUser:
        id = 999_999
    class _Bot:
        user = _BotUser()
        cogs = {}
        process_commands_calls = []
        async def process_commands(self, m):
            self.process_commands_calls.append(m)
        async def fetch_user(self, uid):
            return type("U", (), {"display_name": f"user_{uid}", "id": uid})()

    bot = _Bot()
    cog = EventsCog(bot)

    invited = FakeMember(uid=8001)
    guild = FakeGuild(gid=42)
    thread = FakeThread(thread_id=800)

    _state.ai_threads[800] = {
        "kind": "ask", "owner_id": invited.id, "guild_id": 42,
        "invited_ids": {invited.id},
        "system_prompt": "x", "character_prompt": None, "history": [],
    }

    # Stubs to keep the on_message tail clean.
    respond_calls = []
    async def _stub_respond(channel, uid, content, reply_to, **kwargs):
        respond_calls.append((channel.id, uid, content))
    monkeypatch.setattr(_events, "respond", _stub_respond)
    async def _no_xp(*a, **kw):
        return (0, False)
    monkeypatch.setattr(_events, "_grant_xp", _no_xp)
    async def _no_daily(*a, **kw):
        return None
    monkeypatch.setattr(_events, "_auto_daily", _no_daily)

    msg = FakeMessage(content="hello AI", author=invited, message_id=100_000)
    msg.guild = guild
    msg.channel = thread
    msg.mentions = []
    msg.reference = None

    await cog.on_message(msg)

    # respond() was called with the thread channel + user content.
    assert respond_calls == [(800, invited.id, "hello AI")]
    # process_commands fires regardless (tail of _handle_ai_routing).
    assert msg in bot.process_commands_calls


async def test_message_in_ai_thread_from_uninvited_user_does_not_route(monkeypatch):
    """If a random user posts in someone else's AI thread, respond() must
    not be called. The thread is gated on invited_ids."""
    import src.events as _events
    from src.events import EventsCog

    class _BotUser:
        id = 999_999
    class _Bot:
        user = _BotUser()
        cogs = {}
        process_commands_calls = []
        async def process_commands(self, m):
            self.process_commands_calls.append(m)
        async def fetch_user(self, uid):
            return type("U", (), {"display_name": f"user_{uid}", "id": uid})()

    bot = _Bot()
    cog = EventsCog(bot)

    owner = FakeMember(uid=8002)
    intruder = FakeMember(uid=8003)
    guild = FakeGuild(gid=42)
    thread = FakeThread(thread_id=801)

    _state.ai_threads[801] = {
        "kind": "ask", "owner_id": owner.id, "guild_id": 42,
        "invited_ids": {owner.id},  # NOT including intruder
        "system_prompt": "x", "character_prompt": None, "history": [],
    }

    respond_calls = []
    async def _stub_respond(*a, **kw):
        respond_calls.append(a)
    monkeypatch.setattr(_events, "respond", _stub_respond)
    async def _no_xp(*a, **kw):
        return (0, False)
    monkeypatch.setattr(_events, "_grant_xp", _no_xp)
    async def _no_daily(*a, **kw):
        return None
    monkeypatch.setattr(_events, "_auto_daily", _no_daily)

    msg = FakeMessage(content="butting in", author=intruder, message_id=100_001)
    msg.guild = guild
    msg.channel = thread
    msg.mentions = []
    msg.reference = None

    await cog.on_message(msg)

    # respond() NOT called for the uninvited user.
    assert respond_calls == []
    # The dispatcher still tail-calls process_commands.
    assert msg in bot.process_commands_calls


# ── Mention-prefixed commands must dispatch, not go to the LLM ────────────────

def _make_command_aware_bot():
    """Stub bot whose get_context resolves '!give'/'!pay' like the real
    dispatcher (command is None for anything else)."""
    class _BotUser:
        id = 999_999
    class _Bot:
        user = _BotUser()
        cogs = {}
        def __init__(self):
            self.process_commands_calls = []
            self.invoked = []
        async def process_commands(self, m):
            self.process_commands_calls.append(m)
        async def get_context(self, m):
            word = m.content.split()[0][1:].lower() if m.content.startswith("!") else ""
            cmd = "pay-command" if word in ("give", "pay") else None
            return type("Ctx", (), {"command": cmd, "message": m})()
        async def invoke(self, ctx):
            self.invoked.append(ctx)
        async def fetch_user(self, uid):
            return type("U", (), {"display_name": f"user_{uid}", "id": uid})()
    return _Bot()


def _stub_routing_side_effects(monkeypatch, respond_calls):
    import src.events as _events
    async def _stub_respond(*a, **kw):
        respond_calls.append(a)
    monkeypatch.setattr(_events, "respond", _stub_respond)
    async def _no_xp(*a, **kw):
        return (0, False)
    monkeypatch.setattr(_events, "_grant_xp", _no_xp)
    async def _no_daily(*a, **kw):
        return None
    monkeypatch.setattr(_events, "_auto_daily", _no_daily)


async def test_mention_prefixed_command_dispatches_instead_of_llm(monkeypatch):
    """'@Bot !give @user 1' must run the give/pay command, not send the
    message to the LLM (which used to answer in prose, coaching the user
    to type !pay while the dispatcher never saw the message)."""
    from src.events import EventsCog

    bot = _make_command_aware_bot()
    cog = EventsCog(bot)
    respond_calls = []
    _stub_routing_side_effects(monkeypatch, respond_calls)

    author = FakeMember(uid=8005)
    msg = FakeMessage(
        content=f"<@{bot.user.id}> !give <@8006> 1", author=author, message_id=100_002
    )
    msg.guild = FakeGuild(gid=42)
    msg.mentions = [bot.user]
    msg.reference = None

    await cog.on_message(msg)

    assert respond_calls == [], "LLM must not answer a mention-prefixed command"
    assert len(bot.invoked) == 1 and bot.invoked[0].command == "pay-command"
    # The mention was stripped so the dispatcher saw the bare command.
    assert msg.content == "!give <@8006> 1"


async def test_mention_prefixed_unknown_bang_word_still_goes_to_llm(monkeypatch):
    """'@Bot !notacommand ...' resolves to no command — the AI keeps it."""
    from src.events import EventsCog

    bot = _make_command_aware_bot()
    cog = EventsCog(bot)
    respond_calls = []
    _stub_routing_side_effects(monkeypatch, respond_calls)

    author = FakeMember(uid=8007)
    msg = FakeMessage(
        content=f"<@{bot.user.id}> !zzz hello", author=author, message_id=100_003
    )
    msg.guild = FakeGuild(gid=42)
    msg.mentions = [bot.user]
    msg.reference = None

    await cog.on_message(msg)

    assert bot.invoked == []
    assert len(respond_calls) == 1, "unknown !word falls through to the AI"


# ── Thread isinstance regression ──────────────────────────────────────────────

async def test_fake_thread_satisfies_discord_thread_isinstance():
    """FakeThread MUST pass `isinstance(_, discord.Thread)` — the cog's
    cmd_continue / cmd_tldr / respond() all branch on this check, and a
    plain Mock would silently take the wrong path."""
    t = FakeThread(thread_id=1)
    assert isinstance(t, discord.Thread)
