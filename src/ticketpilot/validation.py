"""Deterministic validation of model output. Pure functions, no I/O, no LLM.

These are also the *measurement instruments* used to score the baseline, which
is why they are built before either pipeline: the same exact-substring check
that enforces grounding in the final version is what counts ungrounded quotes
in the baseline run.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .models import CATEGORY_VALUES, FLAG_VALUES, PRIORITY_VALUES, EvidenceItem

_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """NFC + whitespace collapse. Used only for diagnostics, never for matching."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", text)).strip()


@dataclass(frozen=True)
class EvidenceValidation:
    """Outcome of the exact-substring evidence check."""

    kept: tuple[EvidenceItem, ...] = field(default=())
    dropped_quotes: tuple[str, ...] = field(default=())
    # Diagnostic only. Counts quotes that failed the strict check but would have
    # passed under NFC + whitespace collapse. Those quotes are still rejected;
    # this number exists so the evaluation report can state whether strictness
    # ever actually cost anything on the Hebrew ticket, rather than guessing.
    would_match_normalised: int = 0

    @property
    def any_dropped(self) -> bool:
        return bool(self.dropped_quotes)

    @property
    def all_dropped(self) -> bool:
        """True when there was evidence to check and none of it survived.

        This is the condition that justifies spending the repair call; a partial
        drop does not, because a surviving quote still grounds the decision.
        """
        return not self.kept and bool(self.dropped_quotes)

    def as_diagnostic(self) -> dict[str, object]:
        return {
            "kept": len(self.kept),
            "dropped": list(self.dropped_quotes),
            "would_match_normalised": self.would_match_normalised,
        }


def validate_evidence(
    items: list[EvidenceItem], ticket_text: str
) -> EvidenceValidation:
    """Keep only quotes that are exact substrings of the raw ticket text.

    No normalisation is applied before comparison. Section 3 of the assignment
    says "exact substring", and normalising first would quietly weaken that to
    "substring modulo whitespace and Unicode form". The Hebrew ticket — RTL text
    with an embedded LTR invoice number — is precisely the case where the
    difference shows up, so the strict reading is the one implemented and the
    permissive one is recorded as a diagnostic instead.
    """
    kept: list[EvidenceItem] = []
    dropped: list[str] = []
    would_match = 0

    normalised_ticket: str | None = None
    for item in items:
        if item.quote and item.quote in ticket_text:
            kept.append(item)
            continue

        dropped.append(item.quote)
        # Only compute the normalised form when something already failed.
        if normalised_ticket is None:
            normalised_ticket = _normalise(ticket_text)
        normalised_quote = _normalise(item.quote)
        if normalised_quote and normalised_quote in normalised_ticket:
            would_match += 1

    return EvidenceValidation(
        kept=tuple(kept),
        dropped_quotes=tuple(dropped),
        would_match_normalised=would_match,
    )


@dataclass(frozen=True)
class KbIdValidation:
    """Outcome of filtering proposed KB IDs against the allowlist."""

    valid: tuple[str, ...] = field(default=())
    invalid: tuple[str, ...] = field(default=())

    @property
    def has_support(self) -> bool:
        return bool(self.valid)

    def as_diagnostic(self) -> dict[str, object]:
        return {"valid": list(self.valid), "invalid": list(self.invalid)}


def validate_kb_ids(proposed: list[str], allowed: frozenset[str]) -> KbIdValidation:
    """Drop IDs outside the allowlist, keep the rest.

    Unlike category and priority, ``kb_ids`` degrades gracefully rather than
    hard-failing: it is list-valued, so partial correctness is still useful to a
    reviewer, and the empty case has a policy-designated flag
    (``NO_KB_SUPPORT``). A scalar has no partial credit, which is why an
    out-of-set category goes to repair and then to abstention instead.

    Duplicates are collapsed while preserving first-seen order; final ordering
    is imposed later by ``KnowledgeBase.canonical_order``.
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in proposed:
        candidate = raw.strip() if isinstance(raw, str) else str(raw)
        if candidate in seen:
            continue
        seen.add(candidate)
        (valid if candidate in allowed else invalid).append(candidate)
    return KbIdValidation(valid=tuple(valid), invalid=tuple(invalid))


def clamp_unit_interval(value: float) -> float:
    """Clamp confidence into [0.0, 1.0].

    Redundant against the typed model on the final path and load-bearing on the
    baseline path, which parses raw JSON. Kept unconditional because the code
    should never depend on an upstream layer having enforced a bound.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric != numeric:  # NaN
        return 0.0
    return max(0.0, min(1.0, numeric))


def contains_canary(texts: list[str], canary: str) -> bool:
    """True if the system-prompt canary leaked into any output field.

    A leak means the model reproduced part of its trusted instructions, so the
    response is discarded entirely and the safe fallback is returned — failing
    closed rather than shipping output that may contain the system prompt.
    """
    if not canary:
        return False
    return any(canary in text for text in texts if text)


def enum_errors(raw: dict[str, object]) -> list[str]:
    """Validate closed vocabularies in a raw (untyped) decision dict.

    Used by the baseline, which parses model output with ``json.loads`` and has
    no typed model to reject an invented value. On the final path the typed
    model has already rejected these, and this function is a redundant check —
    kept deliberately, because the code must never assume the API enforced the
    allowlist for it.

    Returns human-readable error strings suitable for the repair prompt.
    """
    errors: list[str] = []

    category = raw.get("category")
    if not isinstance(category, str) or category not in CATEGORY_VALUES:
        errors.append(
            f"category {category!r} is not one of: {', '.join(sorted(CATEGORY_VALUES))}"
        )

    priority = raw.get("priority")
    if not isinstance(priority, str) or priority not in PRIORITY_VALUES:
        errors.append(
            f"priority {priority!r} is not one of: {', '.join(sorted(PRIORITY_VALUES))}"
        )

    flags = raw.get("flags", [])
    if not isinstance(flags, list):
        errors.append("flags must be a list")
    else:
        for flag in flags:
            if not isinstance(flag, str) or flag not in FLAG_VALUES:
                errors.append(
                    f"flag {flag!r} is not one of: {', '.join(sorted(FLAG_VALUES))}"
                )

    return errors
