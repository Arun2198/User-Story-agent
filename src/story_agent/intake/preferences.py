"""Extract whitelisted preferences from free text.

Free text is data. The only things it can change are the fields of
``Preferences``, and only through the patterns below. Nothing in the text can
alter rules, scope or guardrails.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from story_agent.schema import Preferences

_MAX_CRITERIA = re.compile(
    r"\b(?:max(?:imum)?|at\s+most|no\s+more\s+than|up\s+to|limit(?:ed)?\s+to)\s+(\d{1,2})\s+"
    r"(?:acceptance\s+)?(?:criteria|acs?)\b",
    re.I,
)
_FORMAT = re.compile(
    r"\b(?:output|export|give\s+me|format(?:ted)?|save)\b[^.\n]{0,20}\b(csv|json|markdown|md)\b",
    re.I,
)
_SCALE = re.compile(
    r"\b(?:estimate[sd]?|estimating|sizing|size|sized|use|in)\b[^.\n]{0,25}\b"
    r"(fibonacci|t-?shirt(?:\s+sizes?)?|hours)\b",
    re.I,
)


def _last(pattern: re.Pattern[str], text: str) -> str | None:
    matches = pattern.findall(text)
    return str(matches[-1]) if matches else None


def extract_preferences(text: str) -> Preferences:
    """Return the preferences found in ``text``. Unrecognised or invalid values are ignored."""
    values: dict[str, object] = {}
    count = _last(_MAX_CRITERIA, text)
    if count is not None:
        values["max_criteria_per_story"] = int(count)
    fmt = _last(_FORMAT, text)
    if fmt is not None:
        values["output_format"] = "md" if fmt.lower() == "markdown" else fmt.lower()
    scale = _last(_SCALE, text)
    if scale is not None:
        values["estimate_scale"] = "tshirt" if "shirt" in scale.lower() else scale.lower()
    try:
        return Preferences.model_validate(values)
    except ValidationError:
        return _keep_valid(values)


def _keep_valid(values: dict[str, object]) -> Preferences:
    good: dict[str, object] = {}
    for key, value in values.items():
        try:
            Preferences.model_validate({key: value})
        except ValidationError:
            continue
        good[key] = value
    return Preferences.model_validate(good)


def merge_preferences(base: Preferences, new: Preferences) -> Preferences:
    """Return ``base`` updated with every value set in ``new``."""
    update = new.model_dump(exclude_none=True)
    return base.model_copy(update=update)
