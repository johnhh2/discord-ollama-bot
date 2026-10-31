"""Last line of defence on every payload that leaves the sidecar.

The catalog and the query builder are what keep sensitive data out; this
module assumes they failed. `scrub` drops any key whose name says it holds
something a user typed or a credential, and replaces any string value
shaped like a token. It runs on tool results and on the bot's /state/*
views alike, and never raises — a payload it can't walk is returned as-is.
"""
from __future__ import annotations

import re

# Exact key names, lower-cased. Each is either a credential, free text a
# user wrote, a display name, or moderation detail — wherever it appears.
DENIED_KEYS = frozenset({
    "token", "secret", "password", "passwd", "api_key", "apikey", "authorization", "link_code",
    "prompt", "system_prompt", "character_prompt", "prompt_text", "story_aliases", "nsfw_aliases", "nsfw_banned_tags",
    "answer", "history", "history_json", "jail_reason", "reason", "gamertag", "holder_name", "custom_name",
    "condition", "report", "content", "quote_json", "tally_json", "poll_votes", "claim_log", "mute_key",
    "flag_user_id", "checksum",
})

_SECRET_VALUE_RES = (
    re.compile(r"[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{25,}"),   # Discord bot token
    re.compile(r"\b(?:sk|rk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),                   # hex blobs: keys, hashes
    re.compile(r"(?i)\b(?:password|passwd|secret|token)\s*[=:]\s*\S+"),
)

REDACTED = "[redacted]"


def scrub_value(value: str) -> str:
    out = value
    for pattern in _SECRET_VALUE_RES:
        out = pattern.sub(REDACTED, out)
    return out


def scrub(obj):
    try:
        return _walk(obj)
    except Exception:
        return obj


def _walk(obj):
    if isinstance(obj, dict):
        return {k: _walk(v) for k, v in obj.items() if str(k).lower() not in DENIED_KEYS}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_walk(v) for v in obj]
    if isinstance(obj, str):
        return scrub_value(obj)
    if isinstance(obj, bytes):
        return REDACTED
    return obj
