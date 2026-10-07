"""Checks that run before anything is written to memory.

Memory must never hold PII, secrets, raw scenario text or instruction-like text.
These checks are the last line of defence: the store calls them on every write,
including edits made through the CLI.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from story_agent.guardrails.injection import InjectionDetector
from story_agent.guardrails.redaction import PLACEHOLDER_RE, Redactor
from story_agent.schema import Finding, Severity

_DETECTOR = InjectionDetector()


def _error(code: str, message: str) -> Finding:
    return Finding(code=code, message=message, severity=Severity.ERROR)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def check_content(
    content: str,
    max_chars: int,
    scenario_text: str = "",
    max_overlap_chars: int = 60,
) -> list[Finding]:
    """Return error findings when ``content`` must not be stored. Empty means it is safe."""
    findings: list[Finding] = []
    if not content.strip():
        findings.append(_error("MEMORY_EMPTY", "content is empty"))
        return findings
    if len(content) > max_chars:
        findings.append(_error("MEMORY_TOO_LONG", f"content is longer than {max_chars} characters"))
    if PLACEHOLDER_RE.search(content):
        findings.append(_error("MEMORY_PLACEHOLDER", "content contains a redaction placeholder"))
    if Redactor().count(content):
        findings.append(_error("MEMORY_PII", "content contains PII or a secret"))
    if _DETECTOR.scan(content).flagged:
        findings.append(
            _error("MEMORY_INJECTION", "content looks like instructions or hidden text")
        )
    if scenario_text and _copies_scenario(content, scenario_text, max_overlap_chars):
        findings.append(_error("MEMORY_RAW_SCENARIO", "content copies the scenario text"))
    return findings


def _copies_scenario(content: str, scenario: str, limit: int) -> bool:
    a, b = _squash(content), _squash(scenario)
    if not a or not b:
        return False
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return match.size >= limit
