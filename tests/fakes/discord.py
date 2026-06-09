"""Minimal Discord shims for testing command flows.

Just enough surface area to drive helpers like `shop_charge` and a few cog
methods. Anything fancy (intents, gateway, cache) is intentionally out of
scope — keep these dumb.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock


class FakePerms:
    def __init__(self, administrator: bool = False, manage_messages: bool = False):
        self.administrator = administrator
        self.manage_messages = manage_messages


class FakeMember:
    """Stand-in for discord.Member.

    `edit` is an AsyncMock by default; tests can set `.edit.side_effect` to
    raise discord.Forbidden to exercise refund/error paths.
    """
    def __init__(
        self,
        uid: int,
        display_name: str = "tester",
        administrator: bool = False,
    ):
        self.id = uid
        self.display_name = display_name
        self.name = display_name
        self.guild_permissions = FakePerms(administrator=administrator)
        self.edit = AsyncMock()
        self.roles = []
        self.bot = False
        self.mention = f"<@{uid}>"

    def __repr__(self):
        return f"FakeMember(id={self.id}, display_name={self.display_name!r})"


class FakeRole:
    def __init__(self, role_id: int, name: str = "test-role"):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class FakeGuild:
    def __init__(self, gid: int = 1, name: str = "test-guild"):
        self.id = gid
        self.name = name
        self.members = []
        self.roles = []
        self.channels = []
        # Tests that drive role/channel creation can set side_effects on these.
        self.create_role = AsyncMock()
        self.create_text_channel = AsyncMock()

    def get_member(self, uid: int):
        for m in self.members:
            if m.id == uid:
                return m
        return None

    def get_member_named(self, name: str):
        """Exact match on display_name or name, mirroring discord.Guild.
        Real guilds expose this; discord.py's built-in MemberConverter calls it
        for non-mention/non-id arguments. Returns None when nothing matches so
        the project MemberConverter falls through to its substring search."""
        for m in self.members:
            if name in (getattr(m, "display_name", None), getattr(m, "name", None)):
                return m
        return None

    def get_role(self, role_id: int):
        for r in self.roles:
            if r.id == role_id:
                return r
        return None

    def get_channel(self, ch_id: int):
        for c in self.channels:
            if c.id == ch_id:
                return c
        return None


class FakeChannel:
    """Generic non-text channel stand-in (won't satisfy isinstance discord.TextChannel)."""
    def __init__(self, ch_id: int = 100, guild: "FakeGuild | None" = None):
        self.id = ch_id
        self.guild = guild
        self.send = AsyncMock()


# Subclass discord.TextChannel so isinstance() checks pass without pulling in
# the full discord.py state machinery. We bypass __init__ and set just the
# attributes our code touches.
import discord as _discord  # noqa: E402


class FakeTextChannel(_discord.TextChannel):
    """Subclasses discord.TextChannel so isinstance() checks pass without
    pulling in the full discord.py state machinery. We bypass __init__ and
    set just the attributes our code touches; `mention` is a read-only
    property on the base class that derives from `id`.
    """
    def __init__(self, ch_id: int = 100, name: str = "test-channel"):
        self.id = ch_id
        self.name = name
        self.send = AsyncMock()
        self.delete = AsyncMock()
        self.edit = AsyncMock()


class FakeThread(_discord.Thread):
    """Subclasses discord.Thread so `isinstance(ch, discord.Thread)` checks
    in cmd_continue / cmd_tldr / respond() pick the thread branch.

    Bypasses discord.Thread.__init__ (which requires a State object and a
    parent guild we don't have). Set `history_messages` to a list of
    FakeMessage to make `.history(limit=N)` async-iterable for cmd_reverse.
    """
    def __init__(self, thread_id: int = 200, name: str = "test-thread"):
        self.id = thread_id
        self.name = name
        self.send = AsyncMock(return_value=FakeMessage())
        self.edit = AsyncMock()
        self.delete = AsyncMock()
        self.add_user = AsyncMock()
        self.history_messages: list = []

    def history(self, limit: int = 100):
        items = list(self.history_messages[:limit])

        class _AsyncIter:
            def __init__(self, xs):
                self._iter = iter(xs)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration
        return _AsyncIter(items)


class FakeMessage:
    """Stand-in for discord.Message.

    `create_thread` returns a FakeThread when called; tests can swap in their
    own thread via `fake_msg.create_thread = AsyncMock(return_value=...)` if
    they want to inspect the thread before the cog mutates it.
    """
    def __init__(self, content: str = "", author: "FakeMember | None" = None,
                 message_id: int = 1, channel: "FakeChannel | None" = None):
        self.id = message_id
        self.content = content
        self.author = author or FakeMember(uid=1)
        self.channel = channel or FakeChannel()
        self.mentions: list = []
        self.channel_mentions: list = []
        self.reference = None
        self.delete = AsyncMock()
        self.edit = AsyncMock()
        self.reply = AsyncMock(return_value=None)
        self.add_reaction = AsyncMock()
        self.clear_reactions = AsyncMock()
        self.create_thread = AsyncMock(side_effect=self._default_create_thread)

    async def _default_create_thread(self, name: str = "test-thread", **kwargs):
        # Default: hand back a fresh FakeThread. Tests that need to inspect
        # the thread before/after can patch `create_thread` directly.
        return FakeThread(name=name)


class FakeCtx:
    """Stand-in for discord.ext.commands.Context.

    `send` records every call (embed=, content=) on `sent_embeds` /
    `sent_messages` for easy assertion.
    """
    def __init__(
        self,
        author: FakeMember | None = None,
        guild: FakeGuild | None = None,
        channel: FakeChannel | None = None,
        command_name: str = "test",
    ):
        self.author = author or FakeMember(uid=1)
        self.guild = guild or FakeGuild()
        self.channel = channel or FakeChannel()
        self.bot = None  # cogs that need .bot can set this in tests
        # `command` is referenced by check_command_permission via .qualified_name.
        self.command = type("Cmd", (), {
            "name": command_name,
            "qualified_name": command_name,
        })()
        self.message = FakeMessage()
        self.sent_embeds: list[Any] = []
        self.sent_messages: list[str] = []
        self.sent_views: list[Any] = []
        self._send_mock = AsyncMock(side_effect=self._record_send)

    async def _record_send(self, content=None, *, embed=None, view=None, **kwargs):
        if embed is not None:
            self.sent_embeds.append(embed)
        if content is not None:
            self.sent_messages.append(content)
        if view is not None:
            self.sent_views.append(view)
        # Return a fake Message so callers that await further can keep working.
        return FakeMessage()

    async def send(self, content=None, *, embed=None, view=None, **kwargs):
        return await self._send_mock(content, embed=embed, view=view, **kwargs)

    @property
    def send_mock(self) -> AsyncMock:
        return self._send_mock
