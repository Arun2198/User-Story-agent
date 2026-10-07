"""JSON publisher: the full structure, for tools."""

from __future__ import annotations

import json
from typing import Any

from story_agent.publish.base import PublishContext
from story_agent.publish.view import PublishView, StoryView
from story_agent.schema import SCHEMA_VERSION


def story_dict(s: StoryView) -> dict[str, Any]:
    """Return one story as plain data."""
    return {
        "id": s.id,
        "title": s.title,
        "statement": s.statement,
        "persona": s.persona,
        "want": s.want,
        "benefit": s.benefit,
        "acceptance_criteria": [
            {"n": c.n, "given": c.given, "when": c.when, "then": c.then, "kind": c.kind}
            for c in s.criteria
        ],
        "priority": s.priority,
        "estimate": s.estimate,
        "nfrs": list(s.nfrs),
        "dependencies": list(s.dependencies),
        "assumptions": list(s.assumptions),
        "open_questions": list(s.open_questions),
        "requirement_ids": list(s.requirement_ids),
        "provenance": list(s.provenance),
        "labels": list(s.labels),
        "idempotency_key": s.key,
        "confidence": s.confidence,
    }


class JsonPublisher:
    """Writes the run as JSON."""

    name = "json"
    extension = "json"

    def render(self, view: PublishView, ctx: PublishContext) -> str:  # noqa: ARG002  (shared signature)
        """Return the document."""
        data = {
            "schema_version": SCHEMA_VERSION,
            "run_id": view.run_id,
            "workspace": view.workspace,
            "epics": [
                {
                    "name": e.name,
                    "prefix": e.prefix,
                    "idempotency_key": e.key,
                    "features": [
                        {
                            "name": f.name,
                            "idempotency_key": f.key,
                            "stories": [story_dict(s) for s in f.stories],
                        }
                        for f in e.features
                    ],
                }
                for e in view.epics
            ],
            "requirements": [
                {"id": r.id, "text": r.text, "category": r.category, "assumed": r.assumed}
                for r in view.requirements
            ],
            "not_published": list(view.skipped),
        }
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
