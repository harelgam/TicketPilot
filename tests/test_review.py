"""Human-review policy and flag provenance.

The provenance tests are the ones worth reading carefully: three degraded paths
share the same UNKNOWN/UNKNOWN/review shape but must carry different flags,
because each flag asserts something specific and two of the paths have no
truthful flag available.
"""

from __future__ import annotations

import pytest

from ticketpilot.models import Flag
from ticketpilot.review import (
    DegradedPath,
    ReviewSignals,
    decide,
    flags_for_degraded_path,
    resolve_flags,
)


def _signals(**overrides: object) -> ReviewSignals:
    base: dict[str, object] = {
        "category": "BILLING",
        "priority": "P2",
        "confidence": 0.9,
        "flags": (),
        "confidence_threshold": 0.75,
    }
    base.update(overrides)
    return ReviewSignals(**base)  # type: ignore[arg-type]


class TestFlagProvenance:
    def test_empty_ticket_claims_missing_info(self) -> None:
        # True claim: the information really is absent from the ticket.
        assert flags_for_degraded_path(DegradedPath.EMPTY_TICKET) == (Flag.MISSING_INFO,)

    def test_no_valid_kb_claims_no_kb_support(self) -> None:
        # True claim: the KB was consulted and nothing applied.
        assert flags_for_degraded_path(DegradedPath.NO_VALID_KB) == (Flag.NO_KB_SUPPORT,)

    def test_validation_failure_claims_nothing(self) -> None:
        # A schema failure is a fact about the model's output, not about the
        # ticket. Borrowing MISSING_INFO here would assert the ticket lacked
        # information when it may have been perfectly complete.
        assert flags_for_degraded_path(DegradedPath.VALIDATION_FAILED) == ()

    def test_provider_failure_claims_nothing(self) -> None:
        # Critically, NOT NO_KB_SUPPORT: an API error is no evidence whatsoever
        # about whether a suitable article exists, because the lookup never ran.
        assert flags_for_degraded_path(DegradedPath.PROVIDER_FAILED) == ()

    def test_provider_failure_never_yields_no_kb_support(self) -> None:
        assert Flag.NO_KB_SUPPORT not in flags_for_degraded_path(DegradedPath.PROVIDER_FAILED)

    def test_all_four_paths_are_distinguished(self) -> None:
        # Guards against a future refactor collapsing these into one shape.
        results = {
            path: flags_for_degraded_path(path)
            for path in (
                DegradedPath.EMPTY_TICKET,
                DegradedPath.NO_VALID_KB,
                DegradedPath.VALIDATION_FAILED,
                DegradedPath.PROVIDER_FAILED,
            )
        }
        assert results[DegradedPath.EMPTY_TICKET] != results[DegradedPath.NO_VALID_KB]
        assert results[DegradedPath.VALIDATION_FAILED] == results[DegradedPath.PROVIDER_FAILED] == ()


class TestResolveFlags:
    def test_detector_alone_sets_injection(self) -> None:
        assert resolve_flags([], injection_detected=True) == [Flag.PROMPT_INJECTION]

    def test_model_alone_sets_injection(self) -> None:
        assert resolve_flags([Flag.PROMPT_INJECTION]) == [Flag.PROMPT_INJECTION]

    def test_union_deduplicates(self) -> None:
        # Either layer detecting is enough; both detecting is not double-counted.
        assert resolve_flags([Flag.PROMPT_INJECTION], injection_detected=True) == [
            Flag.PROMPT_INJECTION
        ]

    def test_output_order_is_canonical_not_set_order(self) -> None:
        # Without a canonical order, set iteration would make the stability
        # metric report churn that is not a real decision change.
        out = resolve_flags([Flag.CONFLICTING_SIGNALS, Flag.MISSING_INFO], injection_detected=True)
        assert out == [Flag.PROMPT_INJECTION, Flag.MISSING_INFO, Flag.CONFLICTING_SIGNALS]

    def test_order_is_stable_across_input_permutations(self) -> None:
        a = resolve_flags([Flag.MISSING_INFO, Flag.NO_KB_SUPPORT])
        b = resolve_flags([Flag.NO_KB_SUPPORT, Flag.MISSING_INFO])
        assert a == b


class TestReviewPolicy:
    def test_clean_confident_ticket_needs_no_review(self) -> None:
        assert decide(_signals()).needs_human_review is False

    @pytest.mark.parametrize("priority", ["P0", "P1"])
    def test_p0_and_p1_always_reviewed(self, priority: str) -> None:
        result = decide(_signals(priority=priority))
        assert result.needs_human_review is True
        assert f"priority_{priority}" in result.reasons

    def test_security_always_reviewed(self) -> None:
        result = decide(_signals(category="SECURITY"))
        assert result.needs_human_review is True
        assert "category_security" in result.reasons

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [("category", "UNKNOWN", "category_unknown"), ("priority", "UNKNOWN", "priority_unknown")],
    )
    def test_unknowns_reviewed(self, field: str, value: str, reason: str) -> None:
        result = decide(_signals(**{field: value}))
        assert result.needs_human_review is True
        assert reason in result.reasons

    def test_low_confidence_reviewed(self) -> None:
        result = decide(_signals(confidence=0.5))
        assert result.needs_human_review is True
        assert any("confidence_below_threshold" in r for r in result.reasons)

    def test_threshold_is_configurable_not_hard_coded(self) -> None:
        # Same confidence, two thresholds, two outcomes: the threshold is data.
        assert decide(_signals(confidence=0.8, confidence_threshold=0.75)).needs_human_review is False
        assert decide(_signals(confidence=0.8, confidence_threshold=0.9)).needs_human_review is True

    @pytest.mark.parametrize("flag", list(Flag))
    def test_every_allowed_flag_triggers_review(self, flag: Flag) -> None:
        result = decide(_signals(flags=(flag,)))
        assert result.needs_human_review is True
        assert f"flag_{flag.value.lower()}" in result.reasons

    @pytest.mark.parametrize(
        "signal",
        [
            "schema_validation_failed",
            "evidence_dropped",
            "evidence_empty",
            "kb_ids_empty",
            "provider_failed",
            "fallback_emitted",
        ],
    )
    def test_each_failure_signal_triggers_review(self, signal: str) -> None:
        result = decide(_signals(**{signal: True}))
        assert result.needs_human_review is True
        assert signal in result.reasons or f"{signal}" in " ".join(result.reasons)


class TestEscalateOnlyInvariant:
    def test_model_requested_review_is_honoured_on_an_otherwise_clean_ticket(self) -> None:
        # A model-produced True is never argued down, even when no policy rule
        # would independently have fired.
        result = decide(_signals(model_requested_review=True))
        assert result.needs_human_review is True
        assert "model_requested_review" in result.reasons

    def test_model_not_requesting_review_cannot_suppress_a_policy_rule(self) -> None:
        # The dangerous direction: an injected instruction persuades the model to
        # report needs_human_review=false on a P0 security incident.
        result = decide(
            _signals(
                category="SECURITY",
                priority="P0",
                model_requested_review=False,
            )
        )
        assert result.needs_human_review is True
        assert "priority_P0" in result.reasons
        assert "category_security" in result.reasons

    def test_reasons_are_deduplicated(self) -> None:
        result = decide(_signals(category="UNKNOWN", priority="UNKNOWN", confidence=0.0))
        assert len(result.reasons) == len(set(result.reasons))

    def test_all_reasons_reported_not_just_the_first(self) -> None:
        # The run record should distinguish "P0 and injection and low confidence"
        # from any one of those alone.
        result = decide(
            _signals(
                category="SECURITY",
                priority="P0",
                confidence=0.1,
                flags=(Flag.PROMPT_INJECTION,),
            )
        )
        assert len(result.reasons) >= 4
