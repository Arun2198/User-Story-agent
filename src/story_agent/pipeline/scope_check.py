"""Stage 1: classify the request and refuse anything out of scope."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from story_agent.config import AppConfig
from story_agent.guardrails.injection import wrap_untrusted
from story_agent.guardrails.scope import ScopeCategory, ScopeDecision, ScopeGuard
from story_agent.llm import LLMClient, LLMRequest
from story_agent.prompts import load_prompt
from story_agent.schema import Intent

PROMPT_ID = "scope_check"
_ALLOWED = {c.value: Intent(c.value) for c in ScopeCategory if c.value in Intent._value2member_map_}


class ScopeVerdict(BaseModel):
    """Model output for the scope check."""

    model_config = ConfigDict(extra="forbid")

    category: ScopeCategory
    reason: str = Field(max_length=300)


def check_scope(
    client: LLMClient, config: AppConfig, prompts_dir: Path, text: str
) -> ScopeDecision:
    """Return an allow or refuse decision for ``text``.

    The deterministic guard runs first. The model only classifies what the guard
    let through, and its answer can never widen the allowed intents.
    """
    guard = ScopeGuard(config.guardrails.scope.refusal_message)
    refusal = guard.evaluate(text)
    if refusal is not None:
        return refusal
    prompt = load_prompt(prompts_dir, PROMPT_ID)
    request = LLMRequest(
        prompt_id=PROMPT_ID,
        system=prompt.text,
        user=wrap_untrusted("request", text),
        model=config.models.generator,
    )
    verdict = client.complete(request, ScopeVerdict).value
    intent = _ALLOWED.get(verdict.category.value)
    if intent is None:
        return guard.refuse(verdict.category)
    return ScopeDecision(True, verdict.category, intent)
