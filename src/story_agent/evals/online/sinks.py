"""Online sinks. ``JsonlSink`` works locally. The others are stubs waiting for details."""

from __future__ import annotations

import json
import os
from pathlib import Path

from story_agent.config import ConfigError
from story_agent.evals.online.record import OnlineRecord


class PendingInputError(ConfigError):
    """A sink is a stub; it needs details that have not been given yet."""


class JsonlSink:
    """Appends one JSON line per run to a private local file. Skips a run it already has."""

    def __init__(self, path: Path) -> None:
        """Remember the file. It is created on the first record."""
        self.path = path

    def seen(self) -> set[str]:
        """Return the run ids already stored."""
        if not self.path.exists():
            return set()
        ids: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                ids.add(str(json.loads(line)["run_id"]))
            except (ValueError, KeyError):
                continue
        return ids

    def send(self, record: OnlineRecord) -> None:
        """Append the record."""
        if record.run_id in self.seen():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(record.to_json_line() + "\n")


class _Pending:
    """Base for stubs. Using one raises ``PendingInputError``."""

    PENDING = ""

    def send(self, record: OnlineRecord) -> None:  # noqa: ARG002  (the sink interface)
        """Refuse, saying what is still needed."""
        raise PendingInputError(self.PENDING)


class OtlpHttpSink(_Pending):
    """Send traces to an OTLP/HTTP collector. Maps ``to_otlp(record.trace)`` to a POST."""

    PENDING = (
        "pending input: the otlp_http sink needs the collector endpoint, the name of the "
        "environment variable holding its auth header, resource attributes to add, and "
        "whether to batch."
    )


class LangfuseSink(_Pending):
    """Send traces and scores to Langfuse. Feedback metrics map to scores."""

    PENDING = (
        "pending input: the langfuse sink needs the host, the names of the environment "
        "variables holding the public and secret keys, and which metrics become scores."
    )


class WarehouseSink(_Pending):
    """Write records to a table. Each record flattens to one row of ``record.metrics()``."""

    PENDING = (
        "pending input: the warehouse sink needs the system, the table and schema, the "
        "environment variable holding the credentials, and the retention period."
    )


def pending_sink(name: str) -> type[_Pending]:
    """Return the stub class for ``name``."""
    return {"otlp_http": OtlpHttpSink, "langfuse": LangfuseSink, "warehouse": WarehouseSink}[name]
