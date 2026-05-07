import json
import logging

from src.logging_setup import JsonFormatter


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def _record(msg: str = "hello", **extra) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="src.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=None, exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_emits_core_fields():
    out = _format(_record("stream_started"))
    assert out["msg"] == "stream_started"
    assert out["level"] == "INFO"
    assert out["logger"] == "src.test"
    assert "ts" in out


def test_promotes_extra_fields():
    out = _format(_record("stream_started", request_id="abc123", user_id=42, guild_id=7))
    assert out["request_id"] == "abc123"
    assert out["user_id"] == 42
    assert out["guild_id"] == 7


def test_does_not_leak_internal_logrecord_attrs():
    out = _format(_record("hi"))
    for leaked in ("args", "msecs", "pathname", "thread", "processName"):
        assert leaked not in out


def test_includes_exception_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = logging.LogRecord(
            name="src.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=None, exc_info=sys.exc_info(),
        )
    out = _format(rec)
    assert "exc" in out
    assert "ValueError" in out["exc"]
    assert "boom" in out["exc"]


def test_handles_non_serializable_extra():
    """default=str must catch values json.dumps can't natively serialize."""
    class Weird:
        def __str__(self): return "weird-instance"
    out = _format(_record("x", thing=Weird()))
    assert out["thing"] == "weird-instance"


def test_message_args_are_interpolated():
    rec = logging.LogRecord(
        name="src.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="user %s did %s", args=("alice", "thing"), exc_info=None,
    )
    out = _format(rec)
    assert out["msg"] == "user alice did thing"


def test_real_logger_extra_roundtrip(caplog):
    """End-to-end: a real logger.info(..., extra={...}) call lands in JSON output."""
    log = logging.getLogger("src.test.roundtrip")
    with caplog.at_level(logging.INFO, logger="src.test.roundtrip"):
        log.info("event", extra={"request_id": "r1", "user_id": 99})
    rec = next(r for r in caplog.records if r.message == "event")
    out = _format(rec)
    assert out["request_id"] == "r1"
    assert out["user_id"] == 99
