"""Evidence grounding, KB-ID filtering, clamping, canary, raw-enum checks."""

from __future__ import annotations

import pytest

from ticketpilot.validation import (
    clamp_unit_interval,
    contains_canary,
    enum_errors,
    validate_evidence,
    validate_kb_ids,
)
from ticketpilot.models import EvidenceItem

TICKET = (
    "Since 09:10, seven customer tenants report HTTP 503 on production login.\n"
    "No one can access the product."
)


def _ev(quote: str) -> EvidenceItem:
    return EvidenceItem(quote=quote, supports=["category"])


class TestEvidenceExactSubstring:
    def test_exact_quote_is_kept(self) -> None:
        result = validate_evidence([_ev("seven customer tenants report HTTP 503")], TICKET)
        assert len(result.kept) == 1
        assert not result.any_dropped

    def test_paraphrase_is_rejected(self) -> None:
        # The substance is right and the wording is not. Section 3 requires an
        # exact substring, so this must be dropped rather than accepted.
        result = validate_evidence([_ev("seven tenants reported HTTP 503 errors")], TICKET)
        assert result.kept == ()
        assert result.all_dropped

    def test_translated_quote_is_rejected(self, hebrew_ticket: str) -> None:
        # A real failure mode on the bilingual ticket: the model answers in
        # English and "quotes" its own translation.
        result = validate_evidence([_ev("We were charged twice for invoice INV-8842")], hebrew_ticket)
        assert result.kept == ()
        assert result.all_dropped

    def test_original_hebrew_quote_is_kept(self, hebrew_ticket: str) -> None:
        result = validate_evidence([_ev("חויבנו פעמיים עבור חשבונית INV-8842")], hebrew_ticket)
        assert len(result.kept) == 1

    def test_partial_drop_keeps_survivors_and_does_not_trigger_repair(self) -> None:
        result = validate_evidence(
            [_ev("No one can access the product"), _ev("totally fabricated quote")],
            TICKET,
        )
        assert len(result.kept) == 1
        assert result.any_dropped is True
        # all_dropped is what gates the repair call; a surviving quote still
        # grounds the decision, so a partial drop must not spend it.
        assert result.all_dropped is False

    def test_whitespace_variant_is_still_rejected_but_counted(self) -> None:
        # Double space where the ticket has one. Rejected, because the policy is
        # strict — but counted, so the report can say whether strictness ever
        # cost anything rather than speculating.
        result = validate_evidence([_ev("No one  can access the product")], TICKET)
        assert result.kept == ()
        assert result.would_match_normalised == 1

    def test_empty_evidence_list_is_neither_dropped_nor_all_dropped(self) -> None:
        result = validate_evidence([], TICKET)
        assert result.kept == ()
        assert result.any_dropped is False
        # No evidence offered is a different condition from evidence offered and
        # all of it rejected; only the latter justifies a repair call.
        assert result.all_dropped is False


class TestKbIdFiltering:
    def test_invented_id_is_dropped_and_valid_kept(self, kb) -> None:
        result = validate_kb_ids(["KB-BILL-01", "KB-REFUND-99"], kb.allowed_ids)
        assert result.valid == ("KB-BILL-01",)
        assert result.invalid == ("KB-REFUND-99",)
        assert result.has_support is True

    def test_all_invented_leaves_no_support(self, kb) -> None:
        result = validate_kb_ids(["KB-NOPE-01"], kb.allowed_ids)
        assert result.valid == ()
        assert result.has_support is False

    def test_duplicates_collapse(self, kb) -> None:
        result = validate_kb_ids(["KB-SEC-01", "KB-SEC-01"], kb.allowed_ids)
        assert result.valid == ("KB-SEC-01",)

    def test_whitespace_is_stripped_before_comparison(self, kb) -> None:
        result = validate_kb_ids(["  KB-AUTH-01 "], kb.allowed_ids)
        assert result.valid == ("KB-AUTH-01",)

    def test_case_variant_is_not_silently_accepted(self, kb) -> None:
        # Deliberately strict: "kb-auth-01" is not an ID in the supplied KB.
        # Accepting it would mean inventing an aliasing rule the assignment
        # never states.
        result = validate_kb_ids(["kb-auth-01"], kb.allowed_ids)
        assert result.valid == ()
        assert result.invalid == ("kb-auth-01",)


class TestClamping:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(1.7, 1.0), (-0.4, 0.0), (0.5, 0.5), (0.0, 0.0), (1.0, 1.0)],
    )
    def test_clamps_into_unit_interval(self, raw: float, expected: float) -> None:
        assert clamp_unit_interval(raw) == expected

    @pytest.mark.parametrize("raw", ["not-a-number", None, float("nan")])
    def test_unusable_values_become_zero(self, raw: object) -> None:
        # Zero is the safe direction: it drives confidence below any threshold
        # and therefore forces human review.
        assert clamp_unit_interval(raw) == 0.0  # type: ignore[arg-type]


class TestCanary:
    def test_leak_detected(self) -> None:
        assert contains_canary(["... TP-CANARY-abc123 ..."], "TP-CANARY-abc123") is True

    def test_no_leak(self) -> None:
        assert contains_canary(["a normal summary"], "TP-CANARY-abc123") is False

    def test_empty_canary_never_matches(self) -> None:
        # Guards against a config mistake turning every response into a leak.
        assert contains_canary(["anything at all"], "") is False


class TestRawEnumErrors:
    def test_valid_raw_decision_has_no_errors(self) -> None:
        raw = {"category": "BILLING", "priority": "P2", "flags": ["MISSING_INFO"]}
        assert enum_errors(raw) == []

    def test_invented_category_reported_with_legal_values(self) -> None:
        errors = enum_errors({"category": "URGENT_BILLING", "priority": "P2", "flags": []})
        assert len(errors) == 1
        # The message must list what *is* legal: telling the model only that its
        # value was rejected invites a second invalid guess on repair.
        assert "DATA_EXPORT" in errors[0]

    def test_invented_priority_and_flag_both_reported(self) -> None:
        errors = enum_errors({"category": "BUG", "priority": "P5", "flags": ["VIP_CUSTOMER"]})
        assert len(errors) == 2
        assert any("P5" in e for e in errors)
        assert any("VIP_CUSTOMER" in e for e in errors)

    def test_missing_keys_are_errors_not_crashes(self) -> None:
        errors = enum_errors({})
        assert len(errors) == 2
