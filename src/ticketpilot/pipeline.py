"""The final triage pipeline: one model call, one repair at most, then invariants.

Control flow
------------
1. Pre-checks. Whitespace-only text short-circuits before any call (A5), and the
   Layer-2 injection detector runs on the raw ticket.
2. One schema-constrained call for ``ModelTriageOutput`` — no ``ticket_id``, no
   action text (A0, A8).
3. At most **one** repair call, carrying every accumulated error at once, and only
   when there is something repairable. A timeout or refusal has no candidate
   response, so no repair is attempted.
4. The deterministic post-layer (Layer 3), which holds regardless of whether
   either detection layer noticed anything wrong.

The ordering of step 4 is what bounds the damage from a successful injection: the
model's proposals are filtered, not trusted, and the only fields it can influence
are ones code re-derives or re-checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from . import actions
from .config import Settings
from .injection import scan as scan_injection
from .kb import KnowledgeBase
from .models import (
    ModelTriageOutput,
    RecommendedAction,
    TicketInput,
    TriageDecision,
)
from .prompts import (
    build_canary_block,
    build_repair_prompt,
    build_system_prompt,
    build_user_prompt,
    new_canary,
)
from .providers.base import LLMProvider
from .review import DegradedPath, ReviewSignals, decide, flags_for_degraded_path, resolve_flags
from .validation import clamp_unit_interval, contains_canary, validate_evidence, validate_kb_ids

FALLBACK_SUMMARY = "Automated triage could not produce a validated decision."
EMPTY_TICKET_SUMMARY = "The ticket contains no text to analyse."


@dataclass
class TriageOutcome:
    """A validated decision plus the diagnostics that explain it."""

    decision: TriageDecision
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _fallback(
    ticket: TicketInput,
    path: DegradedPath,
    *,
    summary: str = FALLBACK_SUMMARY,
    injection_detected: bool = False,
) -> TriageDecision:
    """Build a safe, schema-valid, reviewable abstention.

    Flags come from the provenance rules, not from convenience: a validation or
    provider failure carries none, because the closed vocabulary has no term for
    "the model or the network failed" and borrowing one would assert something
    untrue about the ticket or the knowledge base.
    """
    flags = resolve_flags(
        [], injection_detected=injection_detected, extra=flags_for_degraded_path(path)
    )
    return TriageDecision(
        # Copied from the input on every path, including this one.
        ticket_id=ticket.ticket_id,
        category="UNKNOWN",
        priority="UNKNOWN",
        summary=summary,
        evidence=[],
        recommended_action=RecommendedAction(
            text=actions.SAFE_GENERIC_ACTION, kb_ids=[]
        ),
        confidence=0.0,
        needs_human_review=True,
        flags=flags,
    )


def _validation_errors(raw_text: str | None) -> list[str]:
    """Turn a failed parse into repair instructions."""
    if not raw_text:
        return ["The response was empty."]
    try:
        ModelTriageOutput.model_validate_json(raw_text)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(p) for p in err['loc']) or 'response'}: {err['msg']}"
            for err in exc.errors()[:8]
        ]
    except Exception:
        return ["The response was not valid JSON matching the required structure."]
    return ["The response could not be validated."]


def triage(
    ticket: TicketInput,
    kb: KnowledgeBase,
    provider: LLMProvider,
    settings: Settings | None = None,
) -> TriageOutcome:
    """Triage one ticket. Never raises; never fabricates."""
    settings = settings or Settings.from_env()
    diagnostics: dict[str, Any] = {"repair_attempted": False, "provider_calls": 0}

    injection = scan_injection(ticket.text)
    diagnostics["injection_scan"] = injection.as_diagnostic()

    # ---- Pre-check: nothing to analyse (A5) -------------------------------
    if not ticket.text or not ticket.text.strip():
        diagnostics["degraded_path"] = DegradedPath.EMPTY_TICKET.value
        diagnostics["note"] = "no provider call made: no model can supply absent information"
        decision = _fallback(
            ticket,
            DegradedPath.EMPTY_TICKET,
            summary=EMPTY_TICKET_SUMMARY,
            injection_detected=injection.detected,
        )
        diagnostics["review"] = {"needs_human_review": True, "reasons": ["empty_ticket"]}
        return TriageOutcome(decision=decision, diagnostics=diagnostics)

    canary = new_canary()
    if hasattr(provider, "canary"):  # let the scripted provider echo a real token
        provider.canary = canary  # type: ignore[attr-defined]

    # Split so the policy/KB block stays byte-stable and cacheable while the
    # per-request canary sits after the cache breakpoint.
    system = build_system_prompt(kb, ticket.text)
    canary_block = build_canary_block(canary)
    messages: list[dict[str, str]] = [
        {"role": "user", "content": build_user_prompt(ticket)}
    ]

    # ---- First call --------------------------------------------------------
    result = provider.generate(
        system=system,
        messages=messages,
        output_model=ModelTriageOutput,
        system_suffix=canary_block,
    )
    diagnostics["provider_calls"] += 1
    diagnostics["provider"] = result.as_diagnostic()

    if not result.ok:
        # No candidate response exists, so a repair call would have nothing to
        # correct. Asserted in the tests by provider_calls == 1.
        diagnostics["degraded_path"] = DegradedPath.PROVIDER_FAILED.value
        diagnostics["provider_failure"] = result.failure.value if result.failure else "unknown"
        decision = _fallback(
            ticket, DegradedPath.PROVIDER_FAILED, injection_detected=injection.detected
        )
        diagnostics["review"] = {"needs_human_review": True, "reasons": ["provider_failed"]}
        return TriageOutcome(decision=decision, diagnostics=diagnostics)

    schema_failed = result.parsed is None
    errors: list[str] = _validation_errors(result.text) if schema_failed else []
    output: ModelTriageOutput | None = result.parsed  # type: ignore[assignment]

    # ---- Evidence check on the first response (may add repair reasons) -----
    evidence = None
    if output is not None:
        evidence = validate_evidence(list(output.evidence), ticket.text)
        if evidence.all_dropped:
            errors.append(
                f"{len(evidence.dropped_quotes)} evidence quote(s) were not exact "
                "substrings of the ticket text."
            )

    # ---- The single repair call -------------------------------------------
    if errors:
        diagnostics["repair_attempted"] = True
        diagnostics["repair_errors"] = list(errors)
        repair_messages = [
            *messages,
            {"role": "assistant", "content": result.text or ""},
            {"role": "user", "content": build_repair_prompt(kb, errors)},
        ]
        repaired = provider.generate(
            system=system,
            messages=repair_messages,
            output_model=ModelTriageOutput,
            system_suffix=canary_block,
        )
        diagnostics["provider_calls"] += 1
        diagnostics["repair_provider"] = repaired.as_diagnostic()

        if repaired.ok and repaired.parsed is not None:
            candidate: ModelTriageOutput = repaired.parsed  # type: ignore[assignment]
            candidate_evidence = validate_evidence(list(candidate.evidence), ticket.text)
            if not candidate_evidence.all_dropped:
                output, evidence, result = candidate, candidate_evidence, repaired
                diagnostics["repair_outcome"] = "succeeded"
            else:
                diagnostics["repair_outcome"] = "evidence still ungrounded"
                output = None
        else:
            diagnostics["repair_outcome"] = "failed"
            output = None

    if output is None or evidence is None:
        diagnostics["degraded_path"] = DegradedPath.VALIDATION_FAILED.value
        decision = _fallback(
            ticket, DegradedPath.VALIDATION_FAILED, injection_detected=injection.detected
        )
        diagnostics["review"] = {
            "needs_human_review": True,
            "reasons": ["schema_validation_failed"],
        }
        return TriageOutcome(decision=decision, diagnostics=diagnostics)

    # ---- Canary check: fail closed ----------------------------------------
    leak_surface = [result.text or "", output.summary, *(e.quote for e in output.evidence)]
    if contains_canary(leak_surface, canary):
        diagnostics["degraded_path"] = DegradedPath.VALIDATION_FAILED.value
        diagnostics["canary_leak"] = True
        decision = _fallback(
            ticket, DegradedPath.VALIDATION_FAILED, injection_detected=True
        )
        diagnostics["review"] = {"needs_human_review": True, "reasons": ["canary_leak"]}
        return TriageOutcome(decision=decision, diagnostics=diagnostics)
    diagnostics["canary_leak"] = False

    # ---- Layer 3: the deterministic post-layer ----------------------------
    diagnostics["evidence"] = evidence.as_diagnostic()

    kb_check = validate_kb_ids(list(output.kb_ids), kb.allowed_ids)
    diagnostics["kb_ids"] = kb_check.as_diagnostic()
    ordered_ids = kb.canonical_order(list(kb_check.valid))
    action_text = actions.build_from_kb(kb, ordered_ids)

    flags = resolve_flags(
        list(output.flags),
        injection_detected=injection.detected,
        no_kb_support=not kb_check.has_support,
    )

    confidence = clamp_unit_interval(output.confidence)
    signals = ReviewSignals(
        category=output.category.value,
        priority=output.priority.value,
        confidence=confidence,
        flags=tuple(flags),
        confidence_threshold=settings.confidence_threshold,
        model_requested_review=output.needs_human_review,
        schema_validation_failed=schema_failed,
        evidence_dropped=evidence.any_dropped,
        evidence_empty=not evidence.kept,
        kb_ids_empty=not kb_check.has_support,
    )
    review = decide(signals)
    diagnostics["review"] = review.as_diagnostic()
    diagnostics["degraded_path"] = (
        DegradedPath.NO_VALID_KB.value
        if not kb_check.has_support
        else DegradedPath.NONE.value
    )

    decision = TriageDecision(
        # Never taken from the model: the field is absent from its schema (A0).
        ticket_id=ticket.ticket_id,
        category=output.category,
        priority=output.priority,
        summary=output.summary,
        evidence=list(evidence.kept),
        recommended_action=RecommendedAction(text=action_text, kb_ids=ordered_ids),
        confidence=confidence,
        # Escalate-only: the rule engine may raise this, never lower it.
        needs_human_review=review.needs_human_review,
        flags=flags,
    )

    # Post-condition, asserted on every path including the fallbacks above.
    assert decision.ticket_id == ticket.ticket_id
    return TriageOutcome(decision=decision, diagnostics=diagnostics)
