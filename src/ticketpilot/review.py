"""Human-review policy and flag provenance. Pure functions, no I/O, no LLM.

Two rules govern everything here.

**Review escalates only.** Code may turn ``needs_human_review`` from False to
True; it never turns True into False. A model-produced False cannot override a
policy rule, and a model-produced True is never argued down. The model is a
proposer, not the decider.

**Every flag must be a true claim about the thing it names.** ``MISSING_INFO``
says something about the *ticket*. ``NO_KB_SUPPORT`` says the knowledge base was
consulted and nothing applied. A schema failure says something about the *model
output*, and a timeout says something about the *network* — neither is evidence
about the ticket or the KB. Since the flag vocabulary is closed and contains no
member for "the model or provider failed", those paths emit no flags at all and
record the reason in the run record instead. Staying silent is the only truthful
option available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Flag, Priority

# Canonical flag order for output. Declaration order in the enum, applied on the
# way out so the same set of flags always serialises identically — without this,
# set-union ordering would make the stability metric report spurious churn.
_FLAG_ORDER: tuple[Flag, ...] = (
    Flag.PROMPT_INJECTION,
    Flag.MISSING_INFO,
    Flag.NO_KB_SUPPORT,
    Flag.CONFLICTING_SIGNALS,
)

# Priority is a str enum, so members hash equal to their values and this set
# accepts either a Priority or a bare string.
_ESCALATED_PRIORITIES = frozenset({Priority.P0, Priority.P1})


class DegradedPath(str, Enum):
    """Why a decision could not be produced normally.

    Distinguished because the correct flag set differs per path — see
    ``flags_for_degraded_path``.
    """

    NONE = "none"
    #: Ticket text was empty or whitespace; no model call was made.
    EMPTY_TICKET = "empty_ticket"
    #: The model proposed KB IDs and none survived the allowlist filter.
    NO_VALID_KB = "no_valid_kb"
    #: Schema, enum, or evidence validation failed and the repair did not fix it.
    VALIDATION_FAILED = "validation_failed"
    #: Timeout, connection error, empty content, or provider refusal.
    PROVIDER_FAILED = "provider_failed"


def flags_for_degraded_path(path: DegradedPath) -> tuple[Flag, ...]:
    """Flags that are *true* for a given degraded path.

    The two empty results are the interesting ones. Attaching ``MISSING_INFO``
    to a schema failure would assert the ticket lacked information when the
    actual problem was the model's output. Attaching ``NO_KB_SUPPORT`` to a
    timeout would assert the KB had no applicable article when the KB was never
    consulted at all. Both would be false statements in the response.
    """
    if path is DegradedPath.EMPTY_TICKET:
        return (Flag.MISSING_INFO,)
    if path is DegradedPath.NO_VALID_KB:
        return (Flag.NO_KB_SUPPORT,)
    # VALIDATION_FAILED and PROVIDER_FAILED: deliberately no flags.
    return ()


def resolve_flags(
    model_flags: list[Flag] | tuple[Flag, ...],
    *,
    injection_detected: bool = False,
    no_kb_support: bool = False,
    extra: tuple[Flag, ...] = (),
) -> list[Flag]:
    """Union the model's flags with the deterministically derived ones.

    Either detection layer is sufficient for ``PROMPT_INJECTION``: the regex
    detector may catch what the model missed, and the model may catch a phrasing
    the regex list does not cover. Output is in canonical order and deduplicated.
    """
    combined: set[Flag] = set(model_flags)
    if injection_detected:
        combined.add(Flag.PROMPT_INJECTION)
    if no_kb_support:
        combined.add(Flag.NO_KB_SUPPORT)
    combined.update(extra)
    return [flag for flag in _FLAG_ORDER if flag in combined]


@dataclass(frozen=True)
class ReviewSignals:
    """Everything the review policy considers.

    Grouped into one object so the rule set is auditable in a single place and
    a new trigger cannot be added by quietly editing a call site.
    """

    category: str
    priority: str
    confidence: float
    flags: tuple[Flag, ...]
    confidence_threshold: float
    model_requested_review: bool = False
    schema_validation_failed: bool = False
    evidence_dropped: bool = False
    evidence_empty: bool = False
    kb_ids_empty: bool = False
    provider_failed: bool = False
    fallback_emitted: bool = False


@dataclass(frozen=True)
class ReviewDecision:
    """Result of applying the policy, with the reasons that fired."""

    needs_human_review: bool
    reasons: tuple[str, ...] = field(default=())

    def as_diagnostic(self) -> dict[str, object]:
        return {
            "needs_human_review": self.needs_human_review,
            "reasons": list(self.reasons),
        }


def decide(signals: ReviewSignals) -> ReviewDecision:
    """Apply the documented human-review policy.

    Returns every reason that fired, not just the first, because the run record
    should explain a review decision fully — "P0 and prompt injection and low
    confidence" is a materially different situation from any one of those alone.
    """
    reasons: list[str] = []

    if signals.model_requested_review:
        # Never downgraded. The model asking for review is always honoured.
        reasons.append("model_requested_review")

    if signals.priority in _ESCALATED_PRIORITIES:
        reasons.append(f"priority_{signals.priority}")
    if signals.category == "SECURITY":
        reasons.append("category_security")
    if signals.category == "UNKNOWN":
        reasons.append("category_unknown")
    if signals.priority == "UNKNOWN":
        reasons.append("priority_unknown")

    if signals.confidence < signals.confidence_threshold:
        reasons.append(
            f"confidence_below_threshold({signals.confidence:.2f}"
            f"<{signals.confidence_threshold:.2f})"
        )

    for flag in signals.flags:
        # Every allowed flag is a review trigger: injection, missing
        # information, conflicting signals, and absent KB support all mean a
        # human should look at this.
        reasons.append(f"flag_{flag.value.lower()}")

    if signals.schema_validation_failed:
        # Fires even when a repair later succeeded: the first attempt producing
        # invalid output is itself a signal the decision is less trustworthy.
        reasons.append("schema_validation_failed")
    if signals.evidence_dropped:
        reasons.append("evidence_dropped")
    if signals.evidence_empty:
        reasons.append("evidence_empty")
    if signals.kb_ids_empty:
        reasons.append("kb_ids_empty")
    if signals.provider_failed:
        reasons.append("provider_failed")
    if signals.fallback_emitted:
        reasons.append("safe_fallback_emitted")

    # Deduplicate while preserving order (e.g. priority_unknown can arrive from
    # two directions on a fallback).
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            ordered.append(reason)

    return ReviewDecision(needs_human_review=bool(ordered), reasons=tuple(ordered))
