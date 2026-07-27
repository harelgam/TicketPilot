"""Scoring and metrics for the baseline-to-final comparison.

Everything here is measurement, never enforcement. That distinction matters most
for ``count_prohibition_violations`` below: a negation-aware text scan is not
sound enough to *guarantee* a grounded action at runtime, which is why the final
pipeline assembles action text from the knowledge base instead (README A8). The
same scan is perfectly usable as an *evaluation instrument* with its recall
limitation stated, applied identically to both arms of the comparison.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config import data_dir
from .kb import KnowledgeBase
from .models import TicketInput
from .validation import enum_errors, validate_evidence, validate_kb_ids
from .models import EvidenceItem


# --------------------------------------------------------------------- loading


@dataclass(frozen=True)
class EvalCase:
    """One evaluation case: a ticket plus its expected outcome."""

    case_id: str
    ticket: TicketInput
    source: str  # "supplied" or "authored"
    expected_category: str | None = None
    expected_category_any_of: tuple[str, ...] = ()
    expected_priority: str | None = None
    expected_priority_any_of: tuple[str, ...] = ()
    expected_flags_include: tuple[str, ...] = ()
    expected_needs_human_review: bool | None = None
    expected_kb_ids_include: tuple[str, ...] = ()
    must_not_priority: tuple[str, ...] = ()
    covers: tuple[str, ...] = ()
    justification: str = ""
    tier_invariance_pair: str | None = None


def _case_from_entry(
    entry: dict[str, Any], ticket: TicketInput, source: str, case_id: str
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        ticket=ticket,
        source=source,
        expected_category=entry.get("expected_category"),
        expected_category_any_of=tuple(entry.get("expected_category_any_of", ())),
        expected_priority=entry.get("expected_priority"),
        expected_priority_any_of=tuple(entry.get("expected_priority_any_of", ())),
        expected_flags_include=tuple(entry.get("expected_flags_include", ())),
        expected_needs_human_review=entry.get("expected_needs_human_review"),
        expected_kb_ids_include=tuple(entry.get("expected_kb_ids_include", ())),
        must_not_priority=tuple(entry.get("must_not_priority", ())),
        covers=tuple(entry.get("covers", ())),
        justification=entry.get("justification", ""),
        tier_invariance_pair=entry.get("tier_invariance_pair"),
    )


def load_cases(which: str = "all") -> list[EvalCase]:
    """Load evaluation cases.

    ``which`` is ``"supplied"``, ``"authored"``, or ``"all"``. Supplied tickets
    are joined to their author-judged labels from a separate file, which is what
    keeps the labels out of application code and clearly marked as judgment
    (README A7).
    """
    cases: list[EvalCase] = []

    if which in {"supplied", "all"}:
        tickets = json.loads(
            (data_dir() / "tickets" / "supplied.json").read_text(encoding="utf-8")
        )["tickets"]
        expected = {
            entry["ticket_id"]: entry
            for entry in json.loads(
                (data_dir() / "eval" / "supplied_expected.json").read_text(encoding="utf-8")
            )["expectations"]
        }
        for ticket in tickets:
            entry = expected.get(ticket["ticket_id"], {})
            cases.append(
                _case_from_entry(
                    entry,
                    TicketInput(
                        ticket_id=ticket["ticket_id"],
                        text=ticket["text"],
                        customer_tier=ticket.get("customer_tier"),
                    ),
                    "supplied",
                    ticket["ticket_id"],
                )
            )

    if which in {"authored", "all"}:
        authored = json.loads(
            (data_dir() / "eval" / "cases.json").read_text(encoding="utf-8")
        )["cases"]
        for entry in authored:
            cases.append(
                _case_from_entry(
                    entry,
                    TicketInput(
                        ticket_id=entry["ticket_id"],
                        text=entry["text"],
                        customer_tier=entry.get("customer_tier"),
                    ),
                    "authored",
                    entry["case_id"],
                )
            )

    return cases


# ------------------------------------------------- prohibition scan (metric only)

# Commitment phrasings drawn from what the supplied KB forbids: refunds before
# investigation, delivery dates, resolution times, and asking the customer for a
# secret.
_COMMITMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("refund_promise", re.compile(r"\b(?:will|we'll|going to|shall)\s+(?:\w+\s+){0,3}refund", re.I)),
    ("refund_promise_noun", re.compile(r"\b(?:issue|process|provide|grant|approve)\s+(?:a\s+|the\s+|your\s+)?(?:full\s+)?refund", re.I)),
    ("refund_already", re.compile(r"\brefund\s+(?:has been|is|was)\s+(?:already\s+)?(?:approved|processed|issued)", re.I)),
    ("delivery_date", re.compile(r"\b(?:will\s+be\s+)?(?:released|delivered|shipped|available)\s+(?:in|on|by|next|within)\b", re.I)),
    ("resolution_time", re.compile(r"\b(?:fixed|resolved|restored|completed)\s+(?:in|within|by)\s+\d", re.I)),
    ("ask_for_secret", re.compile(r"\b(?:send|share|provide|paste|reply with)\s+(?:us\s+|me\s+)?(?:your\s+)?(?:password|api\s*key|secret|token|credential)", re.I)),
    ("compensation", re.compile(r"\b(?:free|complimentary)\s+(?:month|service|credit)", re.I)),
)

_NEGATORS = re.compile(r"\b(?:do not|don't|never|cannot|can't|won't|without|avoid|refrain from)\b", re.I)
_NEGATION_WINDOW = 60


def count_prohibition_violations(text: str, kb: KnowledgeBase | None = None) -> list[str]:
    """Count apparent commitments the supplied KB forbids. **Lower bound.**

    Negation-aware, because compliant text routinely restates the prohibition —
    *"...and do not promise a refund before investigation"* must not be counted as
    a violation. A match counts only when no negator appears within the preceding
    window.

    Recall is *not* complete and this is not claimed to be a safety control.
    Phrasings such as "the refund is already approved" are covered, but a
    sufficiently novel wording is not, and no keyword scan could be. It is used
    only to put a defensible floor under the baseline's ungrounded-commitment
    rate. The final version does not rely on it at all: its action text is
    assembled from KB content, so its rate is zero by construction rather than by
    detection.
    """
    if not text:
        return []
    hits: list[str] = []
    for name, pattern in _COMMITMENT_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - _NEGATION_WINDOW) : match.start()]
            if _NEGATORS.search(window):
                continue  # compliant restatement of the prohibition
            hits.append(name)
            break
    return hits


# ---------------------------------------------------------------------- scoring


@dataclass
class CaseScore:
    """Per-case measurements. ``None`` means "not scored for this case"."""

    case_id: str
    source: str
    mode: str
    schema_valid: bool = False
    category_correct: bool | None = None
    priority_correct: bool | None = None
    forbidden_priority_used: bool = False
    review_correct: bool | None = None
    expected_flags_present: bool | None = None
    #: Flags returned beyond the expected minimum. Counted, not penalised — the
    #: expected lists are minimums, and an extra flag is often defensible.
    extra_flag_count: int = 0
    ticket_id_match: bool = False
    evidence_count: int = 0
    evidence_exact: int = 0
    kb_ids_count: int = 0
    kb_ids_unknown: int = 0
    prohibition_violations: tuple[str, ...] = ()
    provider_failure: str | None = None
    crashed: bool = False
    notes: tuple[str, ...] = field(default=())

    #: Decision fields used for the stability comparison. Summary wording is
    #: excluded deliberately: section 8 asks for decision fields, not phrasing.
    decision_fingerprint: str = ""


def _score_category(case: EvalCase, actual: str | None) -> bool | None:
    if case.expected_category is not None:
        return actual == case.expected_category
    if case.expected_category_any_of:
        return actual in case.expected_category_any_of
    return None


def _score_priority(case: EvalCase, actual: str | None) -> bool | None:
    if case.expected_priority is not None:
        return actual == case.expected_priority
    if case.expected_priority_any_of:
        return actual in case.expected_priority_any_of
    return None


def _fingerprint(
    category: str | None,
    priority: str | None,
    flags: list[str],
    review: bool | None,
    kb_ids: list[str],
) -> str:
    return json.dumps(
        {
            "category": category,
            "priority": priority,
            "flags": sorted(flags),
            "needs_human_review": review,
            "kb_ids": sorted(kb_ids),
        },
        sort_keys=True,
    )


def score_raw_decision(
    case: EvalCase,
    decision: dict[str, Any] | None,
    kb: KnowledgeBase,
    *,
    mode: str,
    provider_failure: str | None = None,
    schema_valid_override: bool | None = None,
) -> CaseScore:
    """Score a decision expressed as a plain dict.

    Used for both arms: the baseline produces a raw dict natively, and a final
    ``TriageDecision`` is dumped to one, so a single scorer applies identical
    rules to both. Scoring them with different code would be the easiest way to
    accidentally flatter the final version.
    """
    score = CaseScore(case_id=case.case_id, source=case.source, mode=mode)
    score.provider_failure = provider_failure

    if decision is None:
        score.schema_valid = False
        return score

    # Schema validity: parseable, and every closed vocabulary respected.
    errors = enum_errors(decision)
    required = {
        "ticket_id",
        "category",
        "priority",
        "summary",
        "evidence",
        "recommended_action",
        "confidence",
        "needs_human_review",
        "flags",
    }
    missing = required - set(decision)
    if missing:
        errors.append(f"missing fields: {sorted(missing)}")
    score.schema_valid = (
        schema_valid_override if schema_valid_override is not None else not errors
    )
    if errors:
        score.notes = tuple(errors[:4])

    category = decision.get("category")
    priority = decision.get("priority")
    score.category_correct = _score_category(case, category if isinstance(category, str) else None)
    score.priority_correct = _score_priority(case, priority if isinstance(priority, str) else None)
    score.forbidden_priority_used = priority in case.must_not_priority

    score.ticket_id_match = decision.get("ticket_id") == case.ticket.ticket_id

    flags = [f for f in decision.get("flags", []) if isinstance(f, str)]
    score.extra_flag_count = len(set(flags) - set(case.expected_flags_include))
    if case.expected_flags_include:
        # Inclusion-based: every required flag must be present. Extra flags are
        # NOT treated as errors, because the expected lists specify the minimum
        # required set rather than an exhaustive one. Extras are counted
        # separately so the metric cannot be read as exact-match accuracy.
        score.expected_flags_present = all(f in flags for f in case.expected_flags_include)
    elif case.expected_needs_human_review is False:
        # For cases expected to pass unreviewed, an empty flag list is the
        # expectation; a spurious flag is a real defect worth counting.
        score.expected_flags_present = not flags

    review = decision.get("needs_human_review")
    if case.expected_needs_human_review is not None and isinstance(review, bool):
        score.review_correct = review == case.expected_needs_human_review

    # Evidence grounding, using the same exact-substring rule the pipeline uses.
    raw_evidence = decision.get("evidence")
    if isinstance(raw_evidence, list):
        items: list[EvidenceItem] = []
        for entry in raw_evidence:
            if isinstance(entry, dict) and isinstance(entry.get("quote"), str):
                supports = entry.get("supports")
                items.append(
                    EvidenceItem(
                        quote=entry["quote"],
                        supports=supports if isinstance(supports, list) and supports else ["unspecified"],
                    )
                )
        score.evidence_count = len(items)
        score.evidence_exact = len(validate_evidence(items, case.ticket.text).kept)

    action = decision.get("recommended_action")
    kb_ids: list[str] = []
    action_text = ""
    if isinstance(action, dict):
        raw_ids = action.get("kb_ids")
        if isinstance(raw_ids, list):
            kb_ids = [i for i in raw_ids if isinstance(i, str)]
        if isinstance(action.get("text"), str):
            action_text = action["text"]
    score.kb_ids_count = len(kb_ids)
    score.kb_ids_unknown = len(validate_kb_ids(kb_ids, kb.allowed_ids).invalid)
    score.prohibition_violations = tuple(count_prohibition_violations(action_text, kb))

    score.decision_fingerprint = _fingerprint(
        category if isinstance(category, str) else None,
        priority if isinstance(priority, str) else None,
        flags,
        review if isinstance(review, bool) else None,
        kb_ids,
    )
    return score


# ------------------------------------------------------------------ aggregation


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(100.0 * numerator / denominator, 1)


def aggregate(scores: list[CaseScore]) -> dict[str, Any]:
    """Roll per-case scores into the reported metrics."""
    total = len(scores)
    scored_category = [s for s in scores if s.category_correct is not None]
    scored_priority = [s for s in scores if s.priority_correct is not None]
    scored_review = [s for s in scores if s.review_correct is not None]
    scored_flags = [s for s in scores if s.expected_flags_present is not None]

    evidence_total = sum(s.evidence_count for s in scores)
    evidence_exact = sum(s.evidence_exact for s in scores)
    kb_total = sum(s.kb_ids_count for s in scores)

    return {
        "cases": total,
        "schema_validity_pct": _rate(sum(1 for s in scores if s.schema_valid), total),
        "category_accuracy_pct": _rate(
            sum(1 for s in scored_category if s.category_correct), len(scored_category)
        ),
        "priority_accuracy_pct": _rate(
            sum(1 for s in scored_priority if s.priority_correct), len(scored_priority)
        ),
        "forbidden_priority_violations": sum(1 for s in scores if s.forbidden_priority_used),
        "review_accuracy_pct": _rate(
            sum(1 for s in scored_review if s.review_correct), len(scored_review)
        ),
        "required_flags_present_pct": _rate(
            sum(1 for s in scored_flags if s.expected_flags_present), len(scored_flags)
        ),
        # Reported alongside the inclusion metric above so a reader can see that
        # 100% "required flags present" does not mean the flag sets matched exactly.
        "cases_with_extra_flags": sum(1 for s in scores if s.extra_flag_count),
        "ticket_id_mismatches": sum(1 for s in scores if not s.ticket_id_match),
        "evidence_quotes_total": evidence_total,
        "valid_evidence_quotes_pct": _rate(evidence_exact, evidence_total),
        # Stated as a count as well as a rate, because the rate goes to None when
        # an arm emits no quotes at all — which is what correct containment looks
        # like after every ungrounded quote has been dropped, and reads
        # confusingly like missing data.
        "ungrounded_quotes_emitted": evidence_total - evidence_exact,
        "kb_ids_total": kb_total,
        "unknown_kb_ids": sum(s.kb_ids_unknown for s in scores),
        "prohibition_violations_lower_bound": sum(
            1 for s in scores if s.prohibition_violations
        ),
        "provider_failures": sum(1 for s in scores if s.provider_failure),
        "crashes": sum(1 for s in scores if s.crashed),
    }


_METRIC_LABELS: tuple[tuple[str, str], ...] = (
    ("schema_validity_pct", "Schema validity"),
    ("category_accuracy_pct", "Category accuracy"),
    ("priority_accuracy_pct", "Priority accuracy"),
    ("forbidden_priority_violations", "Forbidden-priority violations"),
    ("valid_evidence_quotes_pct", "Valid evidence quotes"),
    ("ungrounded_quotes_emitted", "Ungrounded quotes emitted"),
    ("unknown_kb_ids", "Unknown KB IDs"),
    ("prohibition_violations_lower_bound", "Ungrounded commitments (lower bound)"),
    ("ticket_id_mismatches", "Ticket-ID mismatches"),
    ("review_accuracy_pct", "Human-review accuracy"),
    ("required_flags_present_pct", "Required flags present (inclusion)"),
    ("cases_with_extra_flags", "Cases with flags beyond the minimum"),
    ("provider_failures", "Provider failures"),
    ("crashes", "Crashes"),
)


def _fmt(key: str, value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{value}%" if key.endswith("_pct") else str(value)


def render_comparison(baseline: dict[str, Any] | None, final: dict[str, Any] | None) -> str:
    """Render the baseline-to-final comparison as a Markdown table."""
    lines = [
        "| Metric | Baseline | Final |",
        "| --- | --- | --- |",
    ]
    for key, label in _METRIC_LABELS:
        b = _fmt(key, baseline.get(key)) if baseline else "n/a"
        f = _fmt(key, final.get(key)) if final else "n/a"
        lines.append(f"| {label} | {b} | {f} |")
    return "\n".join(lines)


def stability_report(fingerprints: dict[str, list[str]]) -> dict[str, Any]:
    """Summarise repeated-run agreement on decision fields.

    Compares the decision fingerprint (category, priority, flags,
    needs_human_review, kb_ids) and never the summary text, per section 8.
    """
    per_ticket: dict[str, Any] = {}
    stable = 0
    for ticket_id, prints in sorted(fingerprints.items()):
        distinct = sorted(set(prints))
        is_stable = len(distinct) == 1
        stable += int(is_stable)
        per_ticket[ticket_id] = {
            "runs": len(prints),
            "distinct_decisions": len(distinct),
            "stable": is_stable,
        }
    return {
        "tickets": len(fingerprints),
        "fully_stable_tickets": stable,
        "stability_pct": _rate(stable, len(fingerprints)),
        "per_ticket": per_ticket,
    }
