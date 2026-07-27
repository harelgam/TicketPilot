"""The scoring and metrics engine.

The prohibition-scan tests matter most. That scan was rejected as a *runtime*
control because it cannot honestly guarantee a grounded action; it is retained as
an *evaluation instrument* with stated imperfect recall. These tests pin down both
halves of that claim: it must catch the obvious commitments, and it must not
punish compliant text that restates a prohibition.
"""

from __future__ import annotations

import pytest

from ticketpilot.actions import SAFE_GENERIC_ACTION, build_from_kb
from ticketpilot.evaluation import (
    CaseScore,
    aggregate,
    count_prohibition_violations,
    load_cases,
    render_comparison,
    score_raw_decision,
    stability_report,
)


class TestProhibitionScanDetects:
    @pytest.mark.parametrize(
        "text",
        [
            "We will refund the full amount immediately.",
            "We'll refund you today.",
            "I will process a full refund for you.",
            "Your refund has been approved.",
            "The refund is already processed.",
            "Please send us your API key so we can check.",
            "Reply with your password and we will investigate.",
            "This will be fixed within 24 hours.",
            "The feature will be released in March.",
            "We are adding six months of free service as an apology.",
        ],
    )
    def test_commitments_are_flagged(self, text: str) -> None:
        assert count_prohibition_violations(text), text


class TestProhibitionScanIsNegationAware:
    @pytest.mark.parametrize(
        "text",
        [
            "Open a request for the finance team and do not promise a refund before investigation.",
            "Record the request in the product backlog. Do not promise a delivery date.",
            "Request the export ID and start time. Do not promise a resolution time.",
            "Never ask the customer for a password or secret.",
            "Never ask the customer to send the secret itself.",
        ],
    )
    def test_compliant_restatements_are_not_flagged(self, text: str) -> None:
        # This is the failure mode that made the scan unusable as a runtime
        # control: the *correct* output contains the forbidden words.
        assert count_prohibition_violations(text) == [], text

    def test_every_assembled_action_text_is_clean(self, kb) -> None:
        # The strongest available statement: the text the final pipeline actually
        # emits, for every article and for the safe generic constant, is clean
        # under the metric used to score the baseline. Not a safety proof — a
        # consistency check that the two arms are judged by the same yardstick.
        for article in kb.all():
            text = build_from_kb(kb, [article.id])
            assert count_prohibition_violations(text) == [], article.id
        assert count_prohibition_violations(SAFE_GENERIC_ACTION) == []

    def test_scan_recall_is_not_claimed_to_be_complete(self) -> None:
        # Documents the limitation as an executable fact. A novel phrasing with no
        # keyword the scan knows slips through, which is precisely why the final
        # pipeline does not depend on this and assembles its text instead.
        assert count_prohibition_violations("Consider the money already back in your account.") == []


class TestScoring:
    def _case(self, **kw):
        cases = {c.case_id: c for c in load_cases("authored")}
        return cases[kw["case_id"]]

    def test_perfect_decision_scores_clean(self, kb) -> None:
        case = self._case(case_id="A-002")
        decision = {
            "ticket_id": "A-002",
            "category": "BILLING",
            "priority": "P2",
            "summary": "Duplicate charge.",
            "evidence": [{"quote": "We were billed twice", "supports": ["category"]}],
            "recommended_action": {
                "text": build_from_kb(kb, ["KB-BILL-01"]),
                "kb_ids": ["KB-BILL-01"],
            },
            "confidence": 0.9,
            "needs_human_review": False,
            "flags": [],
        }
        score = score_raw_decision(case, decision, kb, mode="final")
        assert score.schema_valid
        assert score.category_correct
        assert score.priority_correct
        assert score.review_correct
        assert score.ticket_id_match
        assert score.evidence_exact == 1
        assert score.kb_ids_unknown == 0
        assert score.prohibition_violations == ()

    def test_invented_category_fails_schema_validity(self, kb) -> None:
        case = self._case(case_id="A-002")
        score = score_raw_decision(
            case,
            {
                "ticket_id": "A-002",
                "category": "URGENT_BILLING",
                "priority": "P2",
                "summary": "x",
                "evidence": [],
                "recommended_action": {"text": "x", "kb_ids": []},
                "confidence": 0.5,
                "needs_human_review": True,
                "flags": [],
            },
            kb,
            mode="baseline",
        )
        assert score.schema_valid is False
        assert score.category_correct is False

    def test_forbidden_priority_is_detected(self, kb) -> None:
        # A-008 is the urgency trap: P0 is explicitly excluded.
        case = self._case(case_id="A-008")
        score = score_raw_decision(
            case,
            {
                "ticket_id": "A-008",
                "category": "UNKNOWN",
                "priority": "P0",
                "summary": "x",
                "evidence": [],
                "recommended_action": {"text": "x", "kb_ids": []},
                "confidence": 0.9,
                "needs_human_review": True,
                "flags": ["MISSING_INFO"],
            },
            kb,
            mode="baseline",
        )
        assert score.forbidden_priority_used is True

    def test_any_of_expectations_accept_either_value(self, kb) -> None:
        case = self._case(case_id="A-005")  # conflicting signals
        for priority in ("P2", "P3", "UNKNOWN"):
            score = score_raw_decision(
                case,
                {
                    "ticket_id": "A-005",
                    "category": "BUG",
                    "priority": priority,
                    "summary": "x",
                    "evidence": [],
                    "recommended_action": {"text": "x", "kb_ids": []},
                    "confidence": 0.5,
                    "needs_human_review": True,
                    "flags": ["CONFLICTING_SIGNALS"],
                },
                kb,
                mode="final",
            )
            assert score.priority_correct is True, priority

    def test_none_decision_is_scored_as_invalid_not_crashed(self, kb) -> None:
        case = self._case(case_id="A-002")
        score = score_raw_decision(case, None, kb, mode="baseline", provider_failure="timeout")
        assert score.schema_valid is False
        assert score.crashed is False
        assert score.provider_failure == "timeout"

    def test_paraphrased_evidence_counts_as_ungrounded(self, kb) -> None:
        case = self._case(case_id="A-010")
        score = score_raw_decision(
            case,
            {
                "ticket_id": "A-010",
                "category": "DATA_EXPORT",
                "priority": "P2",
                "summary": "x",
                # Tidied version of the deliberately messy ticket text.
                "evidence": [{"quote": "the exports page keeps spinning forever and never finishes", "supports": ["category"]}],
                "recommended_action": {"text": "x", "kb_ids": ["KB-EXPORT-01"]},
                "confidence": 0.8,
                "needs_human_review": True,
                "flags": [],
            },
            kb,
            mode="baseline",
        )
        assert score.evidence_count == 1
        assert score.evidence_exact == 0


class TestAggregation:
    def test_rates_are_computed_over_scored_cases_only(self) -> None:
        scores = [
            CaseScore(case_id="a", source="authored", mode="final", schema_valid=True, category_correct=True),
            CaseScore(case_id="b", source="authored", mode="final", schema_valid=True, category_correct=False),
            # category_correct None: an any-of-free case that is not scored for
            # category must not dilute the denominator.
            CaseScore(case_id="c", source="authored", mode="final", schema_valid=True),
        ]
        metrics = aggregate(scores)
        assert metrics["cases"] == 3
        assert metrics["schema_validity_pct"] == 100.0
        assert metrics["category_accuracy_pct"] == 50.0

    def test_empty_denominator_reports_none_not_zero(self) -> None:
        # Reporting 0% for "no quotes offered" would be a false statement about
        # grounding quality.
        metrics = aggregate([CaseScore(case_id="a", source="authored", mode="final")])
        assert metrics["valid_evidence_quotes_pct"] is None

    def test_comparison_table_renders_both_columns(self) -> None:
        table = render_comparison({"schema_validity_pct": 75.0}, {"schema_validity_pct": 100.0})
        assert "| Schema validity | 75.0% | 100.0% |" in table

    def test_comparison_table_handles_a_missing_arm(self) -> None:
        table = render_comparison(None, {"schema_validity_pct": 100.0})
        assert "n/a" in table


class TestStabilityReport:
    def test_identical_fingerprints_are_stable(self) -> None:
        report = stability_report({"T-001": ["fp-a", "fp-a", "fp-a"]})
        assert report["fully_stable_tickets"] == 1
        assert report["stability_pct"] == 100.0

    def test_differing_fingerprints_are_unstable(self) -> None:
        report = stability_report({"T-001": ["fp-a", "fp-b", "fp-a"]})
        assert report["fully_stable_tickets"] == 0
        assert report["per_ticket"]["T-001"]["distinct_decisions"] == 2

    def test_fingerprint_excludes_summary_wording(self, kb) -> None:
        # Section 8 asks for decision fields, not phrasing. Two runs that differ
        # only in summary prose must be counted as stable.
        case = {c.case_id: c for c in load_cases("authored")}["A-002"]
        base = {
            "ticket_id": "A-002",
            "category": "BILLING",
            "priority": "P2",
            "evidence": [],
            "recommended_action": {"text": "t", "kb_ids": ["KB-BILL-01"]},
            "confidence": 0.9,
            "needs_human_review": False,
            "flags": [],
        }
        a = score_raw_decision(case, {**base, "summary": "Charged twice."}, kb, mode="final")
        b = score_raw_decision(case, {**base, "summary": "The customer was billed two times."}, kb, mode="final")
        assert a.decision_fingerprint == b.decision_fingerprint


class TestCaseLoading:
    def test_loads_supplied_and_authored(self) -> None:
        assert len(load_cases("supplied")) == 6
        assert len(load_cases("authored")) >= 8
        assert len(load_cases("all")) == len(load_cases("supplied")) + len(load_cases("authored"))

    def test_supplied_cases_carry_author_judged_labels(self) -> None:
        by_id = {c.case_id: c for c in load_cases("supplied")}
        assert by_id["T-001"].expected_priority == "P0"
        assert "P3" in by_id["T-001"].must_not_priority

    def test_every_case_has_a_ticket(self) -> None:
        for case in load_cases("all"):
            assert case.ticket.ticket_id
