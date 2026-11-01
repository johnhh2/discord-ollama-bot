from botmcp import redact


def test_denied_keys_are_dropped_at_any_depth():
    payload = {
        "ok": 1,
        "token": "x",
        "rows": [{"user_id": 1, "jail_reason": "typed", "nested": {"system_prompt": "p", "level": 3}}],
        "Password": "y",
    }
    out = redact.scrub(payload)
    assert out == {"ok": 1, "rows": [{"user_id": 1, "nested": {"level": 3}}]}


def test_token_shaped_values_are_replaced():
    # Assembled at runtime: the literal would trip GitHub push protection,
    # which pattern-matches source for Discord bot tokens.
    discord_token = ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GaBcDe", "abcdefghijklmnopqrstuvwxyz1234567890"))
    sha = "a" * 64
    out = redact.scrub({"a": f"see {discord_token} now", "b": sha, "c": "Bearer abcdefghijklmnopqrstuvwxyz", "d": "sk-abcdefghijklmnopqrstuvwxyz"})
    assert out["a"] == f"see {redact.REDACTED} now"
    assert out["b"] == redact.REDACTED
    assert out["c"] == redact.REDACTED
    assert out["d"] == redact.REDACTED


def test_ordinary_values_pass_through():
    out = redact.scrub({"id": 123456789012345678, "name": "Alpha", "date": "2026-10-31", "pgn": "1. e4 e5 2. Nf3", "f": 1.5, "n": None, "b": True})
    assert out == {"id": 123456789012345678, "name": "Alpha", "date": "2026-10-31", "pgn": "1. e4 e5 2. Nf3", "f": 1.5, "n": None, "b": True}


def test_bytes_and_sets_are_handled():
    out = redact.scrub({"blob": b"\x00\x01", "s": {1, 2}})
    assert out["blob"] == redact.REDACTED
    assert sorted(out["s"]) == [1, 2]


def test_scrub_never_raises():
    class Odd:
        pass
    odd = Odd()
    assert redact.scrub(odd) is odd
