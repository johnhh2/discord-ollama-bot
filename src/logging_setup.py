"""JSON log formatter that promotes `extra={...}` fields to top-level keys.

Call sites already attach structured context via the stdlib `extra` arg, e.g.
`log.info("stream_started", extra={"request_id": rid, "user_id": uid})`. The
default text formatter drops those fields; this one preserves them so log
aggregators (Loki, ES, Datadog, `jq`) can filter and group by them.
"""
import json
import logging
import sys

# Built-in attributes on every LogRecord. Anything else came from `extra={}`
# (or was added to the record by a Filter) and should be promoted.
_STANDARD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


class JsonFormatter(logging.Formatter):
    """Render LogRecords as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install JsonFormatter as the root handler. Idempotent via force=True."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
