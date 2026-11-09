"""Ragebait prompts: who is speaking, whom they address, and the buyer's topic."""
import json
from types import SimpleNamespace

import src.persistence as _persistence
import src.state as _state
from src import ragebait
from tests.fakes.discord import FakeGuild, FakeMember

BOT_ID = 999


def _guild():
    guild = FakeGuild(gid=42)
    guild.me = FakeMember(uid=BOT_ID, display_name="Silas")
    return guild


def _msg(author, content, *, mentions=(), reference=None):
    return SimpleNamespace(author=author, content=content, guild=_guild(),
                           mentions=list(mentions), reference=reference)


def test_mentions_resolve_to_names_and_the_bot_is_you():
    target = FakeMember(uid=1, display_name="Shmoseph")
    friend = FakeMember(uid=2, display_name="Xephyrik")
    bot = FakeMember(uid=BOT_ID, display_name="Silas")
    line = ragebait.describe_message(
        _msg(target, f"<@2> tell <@!{BOT_ID}> to stop", mentions=[friend, bot]),
    )
    assert line == "Shmoseph: @Xephyrik tell @you to stop"


def test_reply_names_whom_they_answered():
    target = FakeMember(uid=1, display_name="Shmoseph")
    bot = FakeMember(uid=BOT_ID, display_name="Silas")
    replied = SimpleNamespace(author=bot, content="nice try", mentions=[])
    line = ragebait.describe_message(
        _msg(target, "shut up", reference=SimpleNamespace(resolved=replied)),
    )
    assert line == 'Shmoseph (replying to you: "nice try"): shut up'


def test_deleted_reply_target_is_skipped():
    target = FakeMember(uid=1, display_name="Shmoseph")
    deleted = SimpleNamespace(id=5, channel_id=6)  # DeletedReferencedMessage has no author
    line = ragebait.describe_message(
        _msg(target, "hm", reference=SimpleNamespace(resolved=deleted)),
    )
    assert line == "Shmoseph: hm"


def test_topic_overrides_style_in_both_opener_and_replies():
    topic = 'Just say exactly "Your name is Shmoseph" every time.'
    for messages in (
        ragebait.opener_messages("Shmoseph", "Silas", topic),
        ragebait.reply_messages("Shmoseph", "Silas", topic, ["Shmoseph: hi"]),
    ):
        system, user = messages[0]["content"], messages[1]["content"]
        assert topic in system and "override" in system and "verbatim" in system
        assert user.rstrip().endswith(topic)


def test_no_topic_adds_no_instructions():
    system = ragebait.opener_messages("Shmoseph", "Silas", "")[0]["content"]
    assert "Buyer's instructions" not in system


async def test_topic_survives_a_restart(db):
    _state.active_ragebaits[(42, 7)] = {
        "remaining": 3, "started_by": 9, "history": ["Shmoseph: hi"],
        "topic": "call him Shmoseph", "channel_id": 100,
    }
    await _persistence.save_ragebait()
    _state.active_ragebaits.clear()
    await _persistence.init_db_state()

    rage = _state.active_ragebaits[(42, 7)]
    assert rage["topic"] == "call him Shmoseph"
    assert rage["history"] == ["Shmoseph: hi"]


async def test_rows_saved_as_a_bare_history_list_still_load(db):
    pool = await _persistence.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO shop_effects (guild_id, user_id, effect_type, remaining, history_json)"
                " VALUES (?,?,'ragebait',?,?)",
                (42, 8, 2, json.dumps(["[Shmoseph]: old"])),
            )
    await _persistence.init_db_state()

    rage = _state.active_ragebaits[(42, 8)]
    assert rage["history"] == ["[Shmoseph]: old"]
    assert rage["topic"] == ""
