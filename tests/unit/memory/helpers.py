from datetime import timedelta
from pathlib import Path
from typing import Any

from story_agent.config import AppConfig
from story_agent.memory.store import SqliteMemoryStore
from story_agent.schema import MemoryEntry, MemoryType, utcnow


def store_for(app: AppConfig, tmp: Path, workspace: str = "w1") -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp, workspace, app.memory)


def entry(id_: str = "M-1", **kw: Any) -> MemoryEntry:
    age = kw.pop("age_days", 1)
    base: dict[str, Any] = {
        "id": id_,
        "workspace": "w1",
        "domain": "banking",
        "type": MemoryType.CONFIRMED_ANSWER,
        "content": "Mobile and web only",
        "source_run_id": "r0",
        "tags": ["category:channels"],
        "created_at": utcnow() - timedelta(days=age),
        "last_confirmed_at": utcnow() - timedelta(days=age),
    }
    base.update(kw)
    return MemoryEntry.model_validate(base)
