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


class FakeGuild:
    def __init__(self, gid: int = 1, name: str = "test-guild"):
        self.id = gid
        self.name = name
        self.members = []

    def get_member(self, uid: int):
        for m in self.members:
            if m.id == uid:
                return m
        return None


class FakeChannel:
    def __init__(self, ch_id: int = 100):
        self.id = ch_id
        self.send = AsyncMock()


class FakeMessage:
    def __init__(self, content: str = ""):
        self.content = content
        self.delete = AsyncMock()
        self.edit = AsyncMock()


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
        self._send_mock = AsyncMock(side_effect=self._record_send)

    async def _record_send(self, content=None, *, embed=None, **kwargs):
        if embed is not None:
            self.sent_embeds.append(embed)
        if content is not None:
            self.sent_messages.append(content)
        # Return a fake Message so callers that await further can keep working.
        return FakeMessage()

    async def send(self, content=None, *, embed=None, **kwargs):
        return await self._send_mock(content, embed=embed, **kwargs)

    @property
    def send_mock(self) -> AsyncMock:
        return self._send_mock
