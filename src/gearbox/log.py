"""Structured JSON logging on the standard library.

One JSON object per line, so logs can be searched and parsed later (traces go to
OpenTelemetry from M7; this is for plain app logs). Prompt and answer text are never
logged here: the request log stores metadata only (tier, tokens, cost, latency).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

# Attributes every LogRecord has. Anything else came from `extra=` and gets logged as a field.
_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(
            {
                k: v
                for k, v in record.__dict__.items()
                # LiteLLM tags every log record with its own marker object; drop it.
                if k not in _STANDARD_ATTRS and not k.startswith("litellm")
            }
        )
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
