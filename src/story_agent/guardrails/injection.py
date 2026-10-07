"""Prompt-injection defence: heuristic detection, quarantine and delimiting.

Everything the user supplies, and anything recalled from memory, is data. The
detector flags instruction-like phrases, role changes, hidden characters and
encoded payloads. Flagged text is replaced by a marker and reported, never
silently dropped. These are heuristics and will miss some attacks; the system
prompts also forbid following embedded instructions.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field

from story_agent.schema import Finding, Severity

OPEN_TAG = "<untrusted_data"
CLOSE_TAG = "</untrusted_data>"

_ZERO_WIDTH = {
    chr(c)
    for c in (
        0x200B,
        0x200C,
        0x200D,
        0x200E,
        0x200F,
        0x2060,
        0x2061,
        0x2062,
        0x2063,
        0x2064,
        0xFEFF,
        0x00AD,
        0x180E,
    )
}
_BIDI = {chr(c) for c in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)}


def _is_tag_char(ch: str) -> bool:
    return 0xE0000 <= ord(ch) <= 0xE007F


def _rx(pattern: str, flags: int = re.I) -> re.Pattern[str]:
    return re.compile(pattern, flags)


_DET = (
    r"(?:all\s+|any\s+|the\s+|your\s+|my\s+|these\s+|those\s+|previous\s+|prior\s+|"
    r"above\s+|earlier\s+|preceding\s+|other\s+)*"
)
_RULE_NOUN = (
    r"(?:instructions?|rules|guidelines|directives|prompts?|guardrails?|safeguards?|"
    r"constraints|restrictions|policies|system\s+prompt)"
)

PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "INJ_OVERRIDE": (
        _rx(rf"\b(?:ignore|disregard|forget|override|bypass|skip)\s+{_DET}{_RULE_NOUN}\b"),
        _rx(
            r"\b(?:from\s+now\s+on|starting\s+now|henceforth)\b.{0,40}\byou\s+(?:are|will|must|should|shall)\b"
        ),
        _rx(r"\byou\s+are\s+now\s+(?:a|an|in|the|my|dan)\b"),
        _rx(
            r"\b(?:new|updated|revised|additional)\s+(?:system\s+)?(?:instructions?|rules|prompt)\s*[:\-]"
        ),
        _rx(r"\bpretend\s+(?:to\s+be|you\s+are|that\s+you)\b"),
        _rx(
            r"\bact\s+as\s+(?:if\s+you|an?\s+(?:unrestricted|unfiltered|jailbroken|different|new|evil))\b"
        ),
        _rx(r"\byou\s+(?:are|will\s+be)\s+(?:an?\s+)?(?:unrestricted|unfiltered|jailbroken)\b"),
        _rx(r"\b(?:developer|god|dan|jailbreak|sudo)\s+mode\b"),
        _rx(r"\bjailbreak(?:ed)?\b"),
        _rx(r"\bdo\s+anything\s+now\b"),
        _rx(r"\bdo\s+not\s+follow\s+(?:the|your|any)\s+(?:rules|instructions|guidelines|system)"),
        _rx(r"\b(?:stop|cease)\s+following\s+(?:the|your)\b"),
    ),
    "INJ_BYPASS": (
        _rx(
            r"\b(?:disable|turn\s+off|deactivate|remove|skip|bypass)\s+(?:the\s+|all\s+|your\s+)?"
            r"(?:guardrails?|redaction|grounding|clarification(?:\s+(?:step|gate|questions))?|"
            r"human\s+review|safety\s+checks?|injection\s+(?:scan|detection|checks?)|scope\s+check)\b"
        ),
        _rx(r"\bdo\s+not\s+ask\s+(?:me\s+)?(?:any\s+)?(?:clarif\w+\s+)?questions\b"),
        _rx(
            r"\bmark\s+(?:all|every|each)\s+(?:stories|story|requirements?|items?)\s+as\s+(?:approved|confirmed|grounded)\b"
        ),
        _rx(r"\bassume\s+(?:all|every)\s+(?:inferred|unknown)\s+items?\s+(?:are\s+)?confirmed\b"),
    ),
    "INJ_EXTRACTION": (
        _rx(
            r"\b(?:reveal|show|print|display|repeat|output|disclose|leak|dump|recite)\b[^.\n]{0,40}"
            r"\b(?:system\s+prompt|your\s+(?:prompt|instructions|rules|guidelines)|"
            r"hidden\s+(?:prompt|instructions)|initial\s+(?:prompt|instructions)|prompt\s+template)"
        ),
        _rx(r"\bwhat\s+(?:is|are)\s+your\s+(?:system\s+)?(?:prompt|instructions|rules)\b"),
        _rx(
            r"\b(?:list|show|dump|export|reveal|print)\b[^.\n]{0,30}\b(?:all|every|entire|whole)\b"
            r"[^.\n]{0,30}\b(?:memory|memories|saved\s+(?:entries|answers?)|stored\s+(?:entries|answers?|data))\b"
        ),
        _rx(r"\bmemor(?:y|ies)\b[^.\n]{0,40}\b(?:other|another|all)\s+workspaces?\b"),
        _rx(r"\b(?:other|another|all)\s+workspaces?\b[^.\n]{0,40}\bmemor(?:y|ies)\b"),
    ),
    "INJ_ROLE": (
        _rx(
            r"^\s*(?:system|assistant|developer)\s*(?:message)?\s*:\s*(?:you\b|ignore|disregard|"
            r"forget|new\s|from\s+now|do\s+not|never\b|always\b|override|obey|respond|answer|"
            r"approve|understood|i\s+will|i'll|the\s+user)",
            re.I | re.M,
        ),
        _rx(r"<\|(?:im_start|im_end|system|user|assistant|endoftext)\|>"),
        _rx(r"\[/?INST\]", re.M),
        _rx(r"<</?SYS>>"),
        _rx(r"^\s*#{2,}\s*(?:system|instructions?)\b", re.I | re.M),
        _rx(r"</?\s*(?:system|assistant|instructions?|prompt)\s*>"),
        _rx(r"\bBEGIN\s+(?:SYSTEM|NEW)\s+(?:PROMPT|INSTRUCTIONS)\b"),
        _rx(
            r"\byou\s+are\s+(?:a|an)\s+(?:helpful\s+)?(?:ai\s+)?(?:assistant|language\s+model|chatbot)\b"
        ),
    ),
    "INJ_BREAKOUT": (_rx(r"</?\s*untrusted_data\b"),),
}

_B64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
_HEX = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){16,}(?![0-9A-Fa-f])")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SENTENCE_BREAK = ".!?\n"


@dataclass(frozen=True)
class Hit:
    """One flagged region of the original text."""

    start: int
    end: int
    code: str


@dataclass
class ScanReport:
    """Result of scanning one text."""

    hits: list[Hit] = field(default_factory=list)
    stripped_invisible: int = 0

    @property
    def flagged(self) -> bool:
        """True when any region was flagged."""
        return bool(self.hits)

    @property
    def codes(self) -> set[str]:
        """The set of finding codes."""
        return {hit.code for hit in self.hits}


@dataclass(frozen=True)
class QuarantinedItem:
    """Text removed from the model input, kept for the user to review."""

    index: int
    text: str
    codes: tuple[str, ...]


@dataclass
class QuarantineResult:
    """Cleaned text plus what was quarantined."""

    text: str
    items: list[QuarantinedItem]
    findings: list[Finding]


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Return NFKC text without invisible characters, plus a map to original offsets."""
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch in _ZERO_WIDTH or ch in _BIDI or _is_tag_char(ch):
            continue
        for c in unicodedata.normalize("NFKC", ch):
            out.append(c)
            index.append(i)
    return "".join(out), index


def match_codes(text: str) -> set[str]:
    """Return the pattern codes that match ``text`` after normalisation."""
    clean, _ = normalize_with_map(text)
    return {code for code, rules in PATTERNS.items() if any(r.search(clean) for r in rules)}


def _decode_base64(token: str) -> str | None:
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=True)
        return raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _decode_hex(token: str) -> str | None:
    try:
        return bytes.fromhex(token).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _looks_like_prose(decoded: str) -> bool:
    if not decoded:
        return False
    printable = sum(ch.isprintable() or ch.isspace() for ch in decoded)
    return printable / len(decoded) >= 0.95 and len(decoded.split()) >= 3


class InjectionDetector:
    """Heuristic detector with quarantine."""

    def scan(self, text: str) -> ScanReport:
        """Find flagged regions in ``text``."""
        report = ScanReport()
        self._scan_patterns(text, report)
        self._scan_invisible(text, report)
        self._scan_encoded(text, report)
        for match in _HTML_COMMENT.finditer(text):
            report.hits.append(Hit(match.start(), match.end(), "INJ_HIDDEN"))
        return report

    def quarantine(self, text: str) -> QuarantineResult:
        """Replace flagged sentences with a marker and report them."""
        report = self.scan(text)
        spans = self._merge(text, report.hits)
        items: list[QuarantinedItem] = []
        out: list[str] = []
        last = 0
        for number, (start, end, codes) in enumerate(spans, start=1):
            out.append(text[last:start])
            out.append(f"[QUARANTINED #{number}]")
            items.append(QuarantinedItem(number, text[start:end].strip(), tuple(sorted(codes))))
            last = end
        out.append(text[last:])
        cleaned = "".join(out)
        stripped = "".join(
            ch
            for ch in cleaned
            if ch not in _ZERO_WIDTH and ch not in _BIDI and not _is_tag_char(ch)
        )
        findings = [
            Finding(
                code=item.codes[0],
                message=f"Quarantined #{item.index}: {', '.join(item.codes)}",
                severity=Severity.ERROR,
                location=f"quarantine:{item.index}",
            )
            for item in items
        ]
        if report.stripped_invisible:
            findings.append(
                Finding(
                    code="INJ_HIDDEN_STRIPPED",
                    message=f"Removed {report.stripped_invisible} invisible characters",
                    severity=Severity.INFO,
                )
            )
        return QuarantineResult(stripped, items, findings)

    def _scan_patterns(self, text: str, report: ScanReport) -> None:
        clean, index = normalize_with_map(text)
        for code, rules in PATTERNS.items():
            for rule in rules:
                for match in rule.finditer(clean):
                    start = index[match.start()] if index else 0
                    last_char = max(match.end() - 1, match.start())
                    end = (index[last_char] + 1) if index else 0
                    report.hits.append(Hit(start, end, code))

    def _scan_invisible(self, text: str, report: ScanReport) -> None:
        for i, ch in enumerate(text):
            if ch in _BIDI or _is_tag_char(ch):
                report.hits.append(Hit(i, i + 1, "INJ_HIDDEN"))
            elif ch in _ZERO_WIDTH:
                report.stripped_invisible += 1

    def _scan_encoded(self, text: str, report: ScanReport) -> None:
        for pattern, decoder in ((_B64, _decode_base64), (_HEX, _decode_hex)):
            for match in pattern.finditer(text):
                decoded = decoder(match.group(0))
                if decoded is not None and _looks_like_prose(decoded):
                    report.hits.append(Hit(match.start(), match.end(), "INJ_ENCODED"))

    @staticmethod
    def _merge(text: str, hits: list[Hit]) -> list[tuple[int, int, set[str]]]:
        spans: list[tuple[int, int, set[str]]] = []
        for hit in sorted(hits, key=lambda h: h.start):
            start = hit.start
            while start > 0 and text[start - 1] not in _SENTENCE_BREAK:
                start -= 1
            end = hit.end
            while end < len(text) and text[end] not in _SENTENCE_BREAK:
                end += 1
            if end < len(text) and text[end] != "\n":
                end += 1
            if spans and start <= spans[-1][1]:
                prev = spans[-1]
                spans[-1] = (prev[0], max(prev[1], end), prev[2] | {hit.code})
            else:
                spans.append((start, end, {hit.code}))
        return spans


def wrap_untrusted(kind: str, text: str) -> str:
    """Wrap ``text`` in delimiters, neutralising any attempt to close them early."""
    safe = re.sub(r"</?\s*untrusted_data", "[delimiter removed]", text, flags=re.I)
    return f'{OPEN_TAG} kind="{kind}">\n{safe}\n{CLOSE_TAG}'
