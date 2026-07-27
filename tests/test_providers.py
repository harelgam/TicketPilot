"""The provider boundary and the scripted mock.

The mock is what makes the failure-safety requirements testable at all: a
schema-constrained real call should never return an invented enum value, so
scripting one is the only way to prove the allowlist path works.
"""

from __future__ import annotations

import json

import pytest

from ticketpilot.config import Settings
from ticketpilot.models import ModelTriageOutput
from ticketpilot.providers import MockProvider, Script, build_provider
from ticketpilot.providers.base import LLMProvider, ProviderFailure

STRUCTURED = {"output_model": ModelTriageOutput}


def _call(provider: MockProvider, **kwargs: object):
    return provider.generate(
        system="sys", messages=[{"role": "user", "content": "hi"}], **kwargs
    )


class TestProtocolConformance:
    def test_mock_satisfies_the_protocol(self) -> None:
        assert isinstance(MockProvider(), LLMProvider)

    def test_build_provider_selects_mock_by_configuration(self) -> None:
        settings = Settings(
            provider="mock",
            model="claude-opus-5",
            effort="low",
            max_tokens=1024,
            timeout_seconds=5.0,
            confidence_threshold=0.75,
        )
        assert build_provider(settings).name == "mock"

    def test_unknown_provider_is_rejected(self) -> None:
        settings = Settings(
            provider="gpt",
            model="x",
            effort="low",
            max_tokens=1,
            timeout_seconds=1.0,
            confidence_threshold=0.75,
        )
        with pytest.raises(ValueError, match="unknown provider"):
            build_provider(settings)


class TestSuccessScripts:
    def test_valid_script_parses_in_structured_mode(self) -> None:
        result = _call(MockProvider([Script.VALID]), **STRUCTURED)
        assert result.ok
        assert isinstance(result.parsed, ModelTriageOutput)
        assert result.parsed.category.value == "BILLING"

    def test_valid_script_returns_text_in_text_mode(self) -> None:
        result = _call(MockProvider([Script.VALID]))
        assert result.ok
        assert result.parsed is None
        assert json.loads(result.text)["category"] == "BILLING"


class TestUnrepairableFailures:
    @pytest.mark.parametrize(
        ("script", "expected"),
        [
            (Script.TIMEOUT, ProviderFailure.TIMEOUT),
            (Script.CONNECTION_ERROR, ProviderFailure.CONNECTION),
            (Script.EMPTY_CONTENT, ProviderFailure.EMPTY_CONTENT),
            (Script.REFUSAL, ProviderFailure.REFUSAL),
        ],
    )
    def test_failure_is_reported_not_raised(self, script: Script, expected) -> None:
        # Failure safety is a contract of this boundary: no provider problem may
        # propagate as an exception into the pipeline.
        result = _call(MockProvider([script]), **STRUCTURED)
        assert result.ok is False
        assert result.failure is expected
        assert result.failure_detail

    @pytest.mark.parametrize(
        "script",
        [Script.TIMEOUT, Script.CONNECTION_ERROR, Script.EMPTY_CONTENT, Script.REFUSAL],
    )
    def test_no_candidate_response_is_returned(self, script: Script) -> None:
        # This is what distinguishes these from malformed JSON: there is nothing
        # to send back for repair, which is why the pipeline must not try.
        result = _call(MockProvider([script]), **STRUCTURED)
        assert result.text is None
        assert result.parsed is None


class TestRepairableFailures:
    @pytest.mark.parametrize("script", [Script.MALFORMED_JSON, Script.INCOMPLETE_JSON])
    def test_malformed_body_is_a_success_with_no_parse(self, script: Script) -> None:
        # A successful call whose body did not validate is deliberately *not* a
        # provider failure: a candidate came back, so it earns the repair call.
        result = _call(MockProvider([script]), **STRUCTURED)
        assert result.ok is True
        assert result.parsed is None
        assert result.text

    @pytest.mark.parametrize(
        "script",
        [Script.INVENTED_CATEGORY, Script.INVENTED_PRIORITY, Script.INVENTED_FLAG],
    )
    def test_invented_enum_fails_typed_validation(self, script: Script) -> None:
        result = _call(MockProvider([script]), **STRUCTURED)
        assert result.ok is True
        # Rejected by the typed model, so it surfaces as text with no parse —
        # the same shape as malformed JSON, and handled by the same repair path.
        assert result.parsed is None

    def test_invented_kb_id_parses_because_ids_are_not_an_enum(self) -> None:
        # kb_ids is a list of free-form strings in the schema; the allowlist is
        # applied by code afterwards. This asserts the split of responsibility:
        # the typed model does not and should not police KB ids.
        result = _call(MockProvider([Script.INVENTED_KB_ID]), **STRUCTURED)
        assert result.parsed is not None
        assert "KB-REFUND-99" in result.parsed.kb_ids


class TestFabricatedActionTextIsDropped:
    def test_extra_recommended_action_never_reaches_the_parsed_model(self) -> None:
        # The A8 case at the provider boundary: the mock returns a legitimate KB
        # id alongside prose promising an immediate refund. ModelTriageOutput has
        # no field for that text, and extra="ignore" drops it rather than failing
        # validation and burning the repair call.
        provider = MockProvider([Script.FABRICATED_REFUND_PROMISE])
        result = _call(provider, **STRUCTURED)
        assert result.parsed is not None
        assert not hasattr(result.parsed, "recommended_action")
        assert result.parsed.kb_ids == ["KB-BILL-01"]
        # The fabricated sentence is present in the raw body and absent from the
        # validated object — which is exactly the containment being claimed.
        assert "immediately refund" in (result.text or "")
        assert "immediately refund" not in result.parsed.model_dump_json()


class TestCallCounting:
    def test_call_count_tracks_invocations(self) -> None:
        provider = MockProvider([Script.VALID])
        _call(provider)
        _call(provider)
        assert provider.call_count == 2

    def test_scripts_advance_then_repeat_the_last(self) -> None:
        provider = MockProvider([Script.MALFORMED_JSON, Script.VALID])
        first = _call(provider, **STRUCTURED)
        second = _call(provider, **STRUCTURED)
        third = _call(provider, **STRUCTURED)
        assert first.parsed is None
        assert second.parsed is not None
        assert third.parsed is not None

    def test_calls_are_recorded_for_the_run_record(self) -> None:
        provider = MockProvider([Script.VALID])
        _call(provider, **STRUCTURED)
        assert provider.calls[0]["structured"] is True
        assert provider.calls[0]["script"] == "valid"


class TestCanaryScript:
    def test_leak_script_echoes_the_configured_canary(self) -> None:
        provider = MockProvider([Script.CANARY_LEAK])
        provider.canary = "TP-CANARY-deadbeef"
        result = _call(provider, **STRUCTURED)
        assert "TP-CANARY-deadbeef" in (result.text or "")
