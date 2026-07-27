"""The section 7 baseline.

These tests assert that the baseline is *weak in the intended ways*. That sounds
backwards, but it is the point: if the baseline quietly sanitised model output,
the baseline-to-final comparison would understate what the final version adds,
and the evaluation would be flattering rather than honest.
"""

from __future__ import annotations

from ticketpilot.baseline import triage_baseline
from ticketpilot.models import TicketInput
from ticketpilot.providers import MockProvider, Script

TICKET = TicketInput(ticket_id="A-002", text="We were billed twice.", customer_tier="standard")


class TestHappyPath:
    def test_valid_response_parses(self, kb) -> None:
        result = triage_baseline(TICKET, kb, MockProvider([Script.VALID]))
        assert result.parse_ok
        assert result.decision["category"] == "BILLING"

    def test_makes_exactly_one_call(self, kb) -> None:
        # The baseline has no repair step by definition.
        provider = MockProvider([Script.VALID])
        triage_baseline(TICKET, kb, provider)
        assert provider.call_count == 1

    def test_uses_text_mode_not_structured_output(self, kb) -> None:
        # Absence of the schema constraint is the baseline's defining weakness.
        provider = MockProvider([Script.VALID])
        triage_baseline(TICKET, kb, provider)
        assert provider.calls[0]["structured"] is False


class TestBaselineDoesNotSanitise:
    def test_invented_category_survives(self, kb) -> None:
        # Must NOT be corrected. The evaluation counts this as a baseline defect;
        # correcting it here would erase the measurement.
        result = triage_baseline(TICKET, kb, MockProvider([Script.INVENTED_CATEGORY]))
        assert result.parse_ok
        assert result.decision["category"] == "URGENT_BILLING"

    def test_invented_kb_id_survives(self, kb) -> None:
        result = triage_baseline(TICKET, kb, MockProvider([Script.INVENTED_KB_ID]))
        assert "KB-REFUND-99" in result.decision["recommended_action"]["kb_ids"]

    def test_fabricated_refund_promise_survives(self, kb) -> None:
        # The contrast that makes the ungrounded-commitment metric meaningful:
        # here the model authors the action text, so a promise contradicting
        # KB-BILL-01 reaches the output. On the final path the field is assembled
        # from the KB and this cannot happen.
        result = triage_baseline(TICKET, kb, MockProvider([Script.FABRICATED_REFUND_PROMISE]))
        assert "immediately refund" in result.decision["recommended_action"]["text"].lower()

    def test_model_authored_ticket_id_is_not_overwritten(self, kb) -> None:
        # The mock returns ticket_id "MOCK-001" while the input is "A-002". The
        # baseline reports the mismatch rather than repairing it, which is what
        # the A0 comparison measures.
        result = triage_baseline(TICKET, kb, MockProvider([Script.VALID]))
        assert result.decision["ticket_id"] == "MOCK-001"
        assert result.ticket_id == "A-002"

    def test_paraphrased_quote_survives(self, kb) -> None:
        result = triage_baseline(TICKET, kb, MockProvider([Script.PARAPHRASED_QUOTE]))
        assert result.decision["evidence"][0]["quote"] == "the customer was billed two times"


class TestBaselineFailureHandling:
    def test_malformed_json_is_reported_not_raised(self, kb) -> None:
        # "Limited validation" means exactly this much: the harness survives so
        # the run can be scored. Nothing is corrected.
        result = triage_baseline(TICKET, kb, MockProvider([Script.MALFORMED_JSON]))
        assert result.parse_ok is False
        assert result.parse_error
        assert result.raw_text

    def test_provider_failure_yields_no_decision_and_no_fallback(self, kb) -> None:
        # The baseline has no safe fallback — that is a final-version feature.
        result = triage_baseline(TICKET, kb, MockProvider([Script.TIMEOUT]))
        assert result.parse_ok is False
        assert result.provider_failure == "timeout"
        assert result.decision is None

    def test_incomplete_json_is_reported(self, kb) -> None:
        result = triage_baseline(TICKET, kb, MockProvider([Script.INCOMPLETE_JSON]))
        assert result.parse_ok is False


class TestJsonExtractionTolerance:
    """Tolerating harmless wrappers keeps the comparison honest.

    Counting a fenced code block as a schema-validity failure would inflate the
    baseline's failure rate for a formatting habit, not a real defect — making the
    final version look better than it is.
    """

    def test_fenced_code_block_is_parsed(self, kb) -> None:
        class Fenced(MockProvider):
            def generate(self, **kwargs):  # type: ignore[override]
                result = super().generate(**kwargs)
                return type(result)(text=f"```json\n{result.text}\n```", model="mock-model")

        result = triage_baseline(TICKET, kb, Fenced([Script.VALID]))
        assert result.parse_ok

    def test_surrounding_prose_is_tolerated(self, kb) -> None:
        class Chatty(MockProvider):
            def generate(self, **kwargs):  # type: ignore[override]
                result = super().generate(**kwargs)
                return type(result)(
                    text=f"Sure, here you go:\n{result.text}\nHope that helps!",
                    model="mock-model",
                )

        result = triage_baseline(TICKET, kb, Chatty([Script.VALID]))
        assert result.parse_ok

    def test_genuinely_broken_json_still_fails(self, kb) -> None:
        # Tolerance must not become repair.
        result = triage_baseline(TICKET, kb, MockProvider([Script.MALFORMED_JSON]))
        assert result.parse_ok is False
