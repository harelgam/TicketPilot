"""The section 7 baseline: one LLM call, a basic prompt, limited validation.

This exists to be measured, not to be good. It is the version a competent
developer writes first, before discovering what goes wrong — and the evaluation's
credibility depends on it being a *fair* first attempt rather than a straw man.
So it gets the same model, the same provider, the same retry behaviour, and the
full knowledge base. What it lacks is everything the final version adds:

* no schema constraint — the response is free-form JSON in a text body
* no closed-vocabulary enforcement, so an invented category survives
* no evidence checking, so a paraphrased or translated quote survives
* no KB-ID filtering, so an invented article ID survives
* no review policy, so ``needs_human_review`` is whatever the model said
* no repair attempt and no safe fallback
* ``ticket_id`` and the recommended-action text are authored by the model

"Limited validation" here means exactly one thing: JSON that fails to parse is
reported as unparsed rather than crashing the harness. Nothing is corrected,
filtered, or clamped — correcting anything would destroy the measurement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .kb import KnowledgeBase
from .models import TicketInput
from .prompts import build_baseline_system_prompt, build_baseline_user_prompt
from .providers.base import LLMProvider


@dataclass
class BaselineResult:
    """Raw outcome of one baseline call.

    Deliberately not a ``TriageDecision``: coercing this into the typed contract
    would validate away the very defects the baseline exists to expose.
    """

    ticket_id: str
    raw_text: str | None = None
    #: Parsed JSON object, or None when the body was not parseable JSON.
    decision: dict[str, Any] | None = None
    parse_error: str | None = None
    provider_failure: str | None = None
    provider_detail: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    model: str | None = None

    @property
    def parse_ok(self) -> bool:
        return self.decision is not None


def _extract_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a JSON object from a text body.

    Tolerates the two harmless wrappers a model commonly adds — a fenced code
    block, or prose before/after the object — because failing on those would
    overstate the baseline's schema-validity failures and make the comparison
    flattering rather than honest. It does not repair malformed JSON.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Drop the opening fence (with optional language tag) and closing fence.
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # Last resort: the outermost {...} span. Still a parse, never a repair.
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return None, str(exc)
        else:
            return None, str(exc)

    if not isinstance(parsed, dict):
        return None, f"expected a JSON object, got {type(parsed).__name__}"
    return parsed, None


def triage_baseline(
    ticket: TicketInput, kb: KnowledgeBase, provider: LLMProvider
) -> BaselineResult:
    """Run the baseline pipeline for one ticket."""
    result = BaselineResult(ticket_id=ticket.ticket_id)

    response = provider.generate(
        system=build_baseline_system_prompt(kb),
        messages=[{"role": "user", "content": build_baseline_user_prompt(ticket)}],
        # No output_model: free-form text, which is the point.
        output_model=None,
    )
    result.model = response.model
    result.usage = dict(response.usage)

    if not response.ok:
        # The baseline has no fallback. A provider failure yields no decision,
        # and the evaluation counts it as such.
        result.provider_failure = response.failure.value if response.failure else "unknown"
        result.provider_detail = response.failure_detail
        return result

    result.raw_text = response.text
    result.decision, result.parse_error = _extract_json_object(response.text or "")
    return result
