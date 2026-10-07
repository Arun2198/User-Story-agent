"""Structured JSON logging. Messages must never contain raw scenarios or PII."""

from __future__ import annotations

import json
import logging
from typing import IO, Any


class JsonFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as a JSON string."""
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "run_id": getattr(record, "run_id", None),
            "stage": getattr(record, "stage", None),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, sort_keys=True)


def configure_logging(stream: IO[str], level: int = logging.INFO) -> logging.Logger:
    """Attach a JSON handler to the ``story_agent`` logger and return it."""
    logger = logging.getLogger("story_agent")
    logger.handlers.clear()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def stage_logger(run_id: str, stage: str) -> logging.LoggerAdapter[logging.Logger]:
    """Return a logger that stamps run_id and stage on every record."""
    return logging.LoggerAdapter(
        logging.getLogger("story_agent"), {"run_id": run_id, "stage": stage}
    )


def log_event(run_id: str, stage: str, msg: str, **fields: object) -> None:
    """Emit one structured record. ``fields`` must not contain raw user text or PII."""
    logging.getLogger("story_agent").info(
        msg, extra={"run_id": run_id, "stage": stage, "fields": fields}
    )
