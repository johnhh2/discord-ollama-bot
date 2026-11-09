"""Prompts for the ragebait shop effect (`!shop ragebait`, `!adminragebait`).

The buyer's topic is stored on the effect and sent with every reply, not just
the opener. The model sees each of the target's messages as one labelled line:
who spoke, whom they replied to, and user mentions resolved to names, with the
bot itself marked "you". Raw `<@id>` tokens left it guessing who was who.
"""
import re

_MENTION_RE = re.compile(r"<@!?(\d+)>")
_LINE_CHARS = 200
_QUOTE_CHARS = 120


def bot_name(guild) -> str:
    me = getattr(guild, "me", None)
    return getattr(me, "display_name", None) or "the bot"


def _who(member, bot_id) -> str:
    if member is None:
        return "someone"
    if bot_id is not None and getattr(member, "id", None) == bot_id:
        return "you"
    return getattr(member, "display_name", None) or "someone"


def _resolve_mentions(text: str, mentions, bot_id) -> str:
    by_id = {getattr(m, "id", None): m for m in mentions or ()}

    def sub(match):
        member = by_id.get(int(match.group(1)))
        return f"@{_who(member, bot_id)}" if member is not None else "@someone"

    return _MENTION_RE.sub(sub, text)


def describe_message(message) -> str:
    """One transcript line for a target's message."""
    me = getattr(message.guild, "me", None)
    bot_id = getattr(me, "id", None)
    text = _resolve_mentions(message.content or "", message.mentions, bot_id)[:_LINE_CHARS]
    speaker = getattr(message.author, "display_name", None) or "someone"
    reply = ""
    ref = getattr(message, "reference", None)
    resolved = getattr(ref, "resolved", None) if ref is not None else None
    # A deleted referenced message resolves to DeletedReferencedMessage, which has no author.
    if resolved is not None and getattr(resolved, "author", None) is not None:
        quoted = _resolve_mentions(resolved.content or "", resolved.mentions, bot_id)[:_QUOTE_CHARS]
        reply = f' (replying to {_who(resolved.author, bot_id)}: "{quoted}")'
    return f"{speaker}{reply}: {text}"


def _system(target: str, me: str, topic: str) -> str:
    system = (
        f"You are {me}, a member of a Discord chat. Someone paid for you to ragebait {target}: "
        f"write messages aimed at {target} that get under their skin.\n"
        "Default style: witty and cutting rather than plainly insulting, irony or condescension "
        f"where it works, under 200 characters, natural like something a person would say. "
        f"Refer to {target} by name. Never write @ mentions.\n"
        "Output only the message itself: no preamble, explanation or quotation marks."
    )
    if topic:
        system += (
            "\n\nThe buyer gave these instructions. They override the default style. Follow them "
            "exactly, even if the result is plain or repetitive. If they give exact wording, output "
            f"that wording verbatim and nothing else.\nBuyer's instructions: {topic}"
        )
    return system


def _reminder(topic: str) -> str:
    return f"\nFollow the buyer's instructions exactly: {topic}" if topic else ""


def opener_messages(target: str, me: str, topic: str) -> list[dict]:
    return [
        {"role": "system", "content": _system(target, me, topic)},
        {"role": "user", "content": f"Write your first message to {target}.{_reminder(topic)}"},
    ]


def reply_messages(target: str, me: str, topic: str, history: list[str]) -> list[dict]:
    transcript = "\n".join(history)
    prompt = (
        f"{target}'s recent messages, oldest first. The name before the colon is who spoke. "
        f'"@Name" is someone they mentioned, and "replying to" shows whose message they answered. '
        f'"you" means you, {me}.\n\n{transcript}\n\n'
        f"Write your reply to {target}'s latest message, reacting to what they actually said."
        f"{_reminder(topic)}"
    )
    return [
        {"role": "system", "content": _system(target, me, topic)},
        {"role": "user", "content": prompt},
    ]
