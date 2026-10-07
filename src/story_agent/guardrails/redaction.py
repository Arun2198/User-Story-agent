"""PII and secret redaction with stable placeholders.

The mapping from placeholder to original value stays local. It is never sent to
the model, and restoring values in outputs is off unless the caller asks for it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

PLACEHOLDER_RE = re.compile(r"<([A-Z_]+)_(\d+)>")


@dataclass(frozen=True)
class Span:
    """A detected sensitive value. ``start`` and ``end`` index the source text."""

    start: int
    end: int
    label: str
    priority: int


class Detector(Protocol):
    """Pluggable detector, for example an NER model."""

    def detect(self, text: str) -> list[Span]:
        """Return spans found in ``text``."""
        ...


def luhn_ok(digits: str) -> bool:
    """Return True when ``digits`` passes the Luhn check."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    """Return True when ``value`` passes the IBAN mod-97 check."""
    compact = value.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    number = "".join(str(int(c, 36)) for c in rearranged)
    return int(number) % 97 == 1


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _card_ok(value: str) -> bool:
    digits = _digits(value)
    return 13 <= len(digits) <= 19 and luhn_ok(digits)


def _always(_value: str) -> bool:
    return True


@dataclass(frozen=True)
class _Rule:
    label: str
    pattern: re.Pattern[str]
    priority: int
    group: int = 0
    check: Callable[[str], bool] = _always


def _rx(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


_ACCOUNT_CONTEXT = r"(?:a/c|acct|account|acc)\b\.?(?:\s*(?:no\.?|number|num|#))?\s*(?:is|:|=)?\s*"

# Lower priority number wins an overlap.
_RULES: tuple[_Rule, ...] = (
    _Rule("API_KEY", _rx(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), 0),
    _Rule("API_KEY", _rx(r"\bsk[-_](?:live|test)?[-_]?[A-Za-z0-9]{20,}"), 0),
    _Rule("API_KEY", _rx(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    _Rule("API_KEY", _rx(r"\bgh[pousr]_[A-Za-z0-9]{36,}"), 0),
    _Rule("API_KEY", _rx(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    _Rule("API_KEY", _rx(r"\bAIza[0-9A-Za-z_\-]{35}"), 0),
    _Rule(
        "TOKEN",
        _rx(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
        0,
    ),
    _Rule("TOKEN", _rx(r"\bBearer\s+([A-Za-z0-9._\-]{16,})", re.I), 0, group=1),
    _Rule(
        "API_KEY",
        _rx(
            r"\b(?:api[_-]?key|secret|access[_-]?token|auth[_-]?token)\s*[:=]\s*([^\s,;]{8,})", re.I
        ),
        0,
        group=1,
    ),
    _Rule(
        "PASSWORD",
        _rx(r"\b(?:password|passwd|pwd|passcode)\s*(?:is|:|=)\s*([^\s,;]+)", re.I),
        0,
        group=1,
    ),
    _Rule("PASSWORD", _rx(r"\bpin\s*(?:is|:|=)\s*(\d{4,8})\b", re.I), 0, group=1),
    _Rule("ACCOUNT", _rx(_ACCOUNT_CONTEXT + r"(\d[\d -]{6,22}\d)", re.I), 1, group=1),
    _Rule("EMAIL", _rx(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), 2),
    _Rule(
        "IBAN",
        _rx(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"),
        3,
        check=iban_ok,
    ),
    _Rule("CARD", _rx(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"), 4, check=_card_ok),
    _Rule("CARD", _rx(r"(?<![\d-])\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?![\d-])"), 4),
    _Rule("SSN", _rx(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"), 5),
    _Rule("PAN", _rx(r"\b[A-Z]{5}\d{4}[A-Z]\b"), 5),
    _Rule("IFSC", _rx(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), 5),
    _Rule("AADHAAR", _rx(r"(?<![\d-])[2-9]\d{3}[ -]\d{4}[ -]\d{4}(?![\d]|[ -]\d)"), 5),
    _Rule(
        "AADHAAR",
        _rx(r"\b(?:aadhaar|aadhar|uid(?:ai)?)\b[^\d\n]{0,20}([2-9]\d{11})(?!\d)", re.I),
        5,
        group=1,
    ),
    _Rule(
        "PHONE",
        _rx(r"(?<![\w.])\+\d{1,3}[ -]?\(?\d{2,5}\)?[ -]?\d{3,5}[ -]?\d{3,5}(?!\d)"),
        6,
    ),
    _Rule("PHONE", _rx(r"(?<![\w-])(?:\+?91[ -]?)?[6-9]\d{9}(?![\d-])"), 6),
    _Rule("PHONE", _rx(r"(?<!\d)\(?\d{3}\)?[ -]\d{3}[ -]\d{4}(?!\d)"), 6),
    _Rule("ACCOUNT", _rx(r"(?<![\d.,-])\d{11,18}(?![\d.,-])"), 7),
)


def find_spans(text: str, extra: Iterable[Detector] = ()) -> list[Span]:
    """Return non-overlapping sensitive spans, sorted by position."""
    candidates: list[Span] = []
    placeholders = [m.span() for m in PLACEHOLDER_RE.finditer(text)]
    for rule in _RULES:
        for match in rule.pattern.finditer(text):
            value = match.group(rule.group)
            if not rule.check(value):
                continue
            start, end = match.span(rule.group)
            candidates.append(Span(start, end, rule.label, rule.priority))
    for detector in extra:
        candidates.extend(detector.detect(text))
    accepted: list[Span] = []
    for span in sorted(candidates, key=lambda s: (s.priority, s.start, -(s.end - s.start))):
        if any(span.start < p_end and p_start < span.end for p_start, p_end in placeholders):
            continue
        if any(span.start < a.end and a.start < span.end for a in accepted):
            continue
        accepted.append(span)
    return sorted(accepted, key=lambda s: s.start)


class Redactor:
    """Replaces sensitive values with placeholders such as ``<ACCOUNT_1>``.

    One instance serves a whole run so numbering stays consistent across the
    scenario, notes and later free text.
    """

    def __init__(
        self,
        mapping: Mapping[str, str] | None = None,
        detectors: Iterable[Detector] = (),
    ) -> None:
        """Start empty, or continue from a saved ``mapping`` of placeholder to value."""
        self._mapping: dict[str, str] = dict(mapping or {})
        self._by_value: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self._detectors = tuple(detectors)
        for placeholder, value in self._mapping.items():
            found = PLACEHOLDER_RE.fullmatch(placeholder)
            if found:
                label, number = found.group(1), int(found.group(2))
                self._by_value[(label, value)] = placeholder
                self._counters[label] = max(self._counters.get(label, 0), number)

    @property
    def mapping(self) -> dict[str, str]:
        """A copy of the placeholder-to-value mapping. Keep it local."""
        return dict(self._mapping)

    def redact(self, text: str) -> str:
        """Return ``text`` with sensitive values replaced by stable placeholders."""
        for match in PLACEHOLDER_RE.finditer(text):
            label, number = match.group(1), int(match.group(2))
            self._counters[label] = max(self._counters.get(label, 0), number)
        spans = find_spans(text, self._detectors)
        out: list[str] = []
        last = 0
        for span in spans:
            out.append(text[last : span.start])
            out.append(self._placeholder(span.label, text[span.start : span.end]))
            last = span.end
        out.append(text[last:])
        return "".join(out)

    def restore(self, text: str) -> str:
        """Put original values back. Callers must opt in; it is off by default."""
        return PLACEHOLDER_RE.sub(lambda m: self._mapping.get(m.group(0), m.group(0)), text)

    def count(self, text: str) -> int:
        """Return how many sensitive values ``text`` still contains."""
        return len(find_spans(text, self._detectors))

    def _placeholder(self, label: str, value: str) -> str:
        key = (label, value)
        if key in self._by_value:
            return self._by_value[key]
        self._counters[label] = self._counters.get(label, 0) + 1
        placeholder = f"<{label}_{self._counters[label]}>"
        self._by_value[key] = placeholder
        self._mapping[placeholder] = value
        return placeholder
