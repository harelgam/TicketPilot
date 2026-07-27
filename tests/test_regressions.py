"""Regression tests for defects found by review rather than by the suite.

Each class here corresponds to a bug that shipped and was caught by someone
reading the code or the artifacts. They are grouped together because what they
have in common is more instructive than their individual subjects: the existing
suite tested behaviour thoroughly and left the *boundaries* — a wrapper in
scripts/, the scorer's own validity check, an expectation field that was loaded
but never read — unguarded.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from ticketpilot.config import Settings, repo_root
from ticketpilot.evaluation import load_cases, score_raw_decision
from ticketpilot.models import TicketInput
from ticketpilot.pipeline import triage
from ticketpilot.providers import MockProvider, Script
from ticketpilot.providers.base import ProviderResult

SETTINGS = Settings(
    provider="mock",
    model="mock-model",
    effort="low",
    max_tokens=2048,
    timeout_seconds=10.0,
    confidence_threshold=0.75,
)
TICKET = TicketInput(
    ticket_id="REG-1",
    text="We were charged twice for the same invoice last month.",
    customer_tier="standard",
)


def _load_verify_live():
    """Import scripts/verify_live.py as a module.

    Loaded by path because scripts/ is not a package; this is the only way to test
    it, and its absence from the suite is why the bug below shipped.
    """
    path = Path(repo_root()) / "scripts" / "verify_live.py"
    spec = importlib.util.spec_from_file_location("verify_live_under_test", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclass field resolution can find the module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestVerifyLiveWrapperHonoursTheProtocol:
    """The documented `python scripts/verify_live.py` command was broken.

    When the canary was split into a separate system block to fix prompt caching,
    ``system_suffix`` was added to the provider protocol and to both providers, but
    not to the ``CallCappedProvider`` wrapper in scripts/. Every invocation died
    with ``TypeError: got an unexpected keyword argument 'system_suffix'`` before
    reaching the API.
    """

    def test_wrapper_accepts_system_suffix(self) -> None:
        module = _load_verify_live()
        provider = module.CallCappedProvider(MockProvider([Script.VALID]), max_calls=1)
        result = provider.generate(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            output_model=None,
            system_suffix="CANARY BLOCK",
        )
        assert isinstance(result, ProviderResult)

    def test_wrapper_forwards_system_suffix_to_the_inner_provider(self) -> None:
        # Accepting the argument and dropping it would silently disable the canary
        # check, which is worse than the TypeError because nothing would complain.
        module = _load_verify_live()
        inner = MockProvider([Script.VALID])
        provider = module.CallCappedProvider(inner, max_calls=1)
        provider.generate(
            system="sys",
            messages=[{"role": "user", "content": "hi"}],
            output_model=None,
            system_suffix="CANARY BLOCK",
        )
        assert inner.calls[0]["has_system_suffix"] is True

    def test_full_pipeline_runs_through_the_wrapper(self, kb) -> None:
        # The end-to-end assertion the original bug would have failed.
        module = _load_verify_live()
        provider = module.CallCappedProvider(MockProvider([Script.VALID]), max_calls=1)
        outcome = triage(TICKET, kb, provider, SETTINGS)
        assert outcome.decision.ticket_id == "REG-1"
        assert provider.call_count == 1

    def test_call_cap_is_enforced(self, kb) -> None:
        # With a cap of 1, a repair attempt must be refused rather than billed.
        module = _load_verify_live()
        provider = module.CallCappedProvider(
            MockProvider([Script.MALFORMED_JSON]), max_calls=1
        )
        outcome = triage(TICKET, kb, provider, SETTINGS)
        assert provider.call_count == 1
        assert outcome.decision.category.value == "UNKNOWN"


class TestPipelineNeverRaises:
    """`triage()` promises "never raises". A broken provider used to break it."""

    def test_provider_raising_typeerror_yields_a_fallback(self, kb) -> None:
        class BadSignature:
            name = "bad"

            def generate(self, *, system, messages, output_model=None):  # type: ignore[no-untyped-def]
                raise AssertionError("should not be reached")

        outcome = triage(TICKET, kb, BadSignature(), SETTINGS)
        assert outcome.decision.category.value == "UNKNOWN"
        assert outcome.decision.needs_human_review is True
        assert outcome.decision.ticket_id == "REG-1"

    def test_provider_raising_arbitrary_exception_yields_a_fallback(self, kb) -> None:
        class Exploding:
            name = "boom"

            def generate(self, **kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError("network on fire")

        outcome = triage(TICKET, kb, Exploding(), SETTINGS)
        assert outcome.decision.category.value == "UNKNOWN"
        assert "provider raised RuntimeError" in str(outcome.diagnostics)

    def test_broken_provider_produces_no_flags(self, kb) -> None:
        # An infrastructure failure says nothing about the ticket or the KB.
        class Exploding:
            name = "boom"

            def generate(self, **kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError("boom")

        assert triage(TICKET, kb, Exploding(), SETTINGS).decision.flags == []


class TestScorerValidatesTheWholeContract:
    """"Schema validity" checked nine field names and three enums, nothing more."""

    def _case(self):
        return {c.case_id: c for c in load_cases("authored")}["A-002"]

    def test_structurally_broken_decision_is_invalid(self, kb) -> None:
        # Every field here has the wrong type or shape. This scored as valid.
        broken = {
            "ticket_id": 123,
            "category": "BILLING",
            "priority": "P2",
            "summary": [],
            "evidence": "not-a-list",
            "recommended_action": "not-an-object",
            "confidence": 99,
            "needs_human_review": "no",
            "flags": [],
        }
        score = score_raw_decision(self._case(), broken, kb, mode="test")
        assert score.schema_valid is False
        # Closed vocabularies are reported separately: these enums *are* legal, and
        # collapsing a type error into the same number would hide which failed.
        assert score.closed_vocab_valid is True

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("recommended_action", "a string, not an object"),
            ("confidence", 1.5),
            ("confidence", -0.1),
            ("evidence", "not-a-list"),
            ("summary", ""),
            ("ticket_id", ""),
            ("ticket_id", None),
        ],
    )
    def test_each_structural_defect_is_caught(self, kb, field: str, value: object) -> None:
        decision = {
            "ticket_id": "A-002",
            "category": "BILLING",
            "priority": "P2",
            "summary": "Duplicate charge.",
            "evidence": [{"quote": "billed twice", "supports": ["category"]}],
            "recommended_action": {"text": "x", "kb_ids": ["KB-BILL-01"]},
            "confidence": 0.9,
            "needs_human_review": False,
            "flags": [],
        }
        decision[field] = value
        assert score_raw_decision(self._case(), decision, kb, mode="test").schema_valid is False

    def test_string_boolean_is_coerced_not_rejected(self, kb) -> None:
        """A documented limit of this check, not an oversight.

        Pydantic's default (lax) mode coerces the string "false" to ``False``, and
        coerces it *correctly*, so a model returning a quoted boolean is accepted
        rather than counted as a contract violation. Strict mode would catch it but
        would also reject harmless widenings such as an integer where a float is
        expected. The looser behaviour is the deliberate choice; recording it here
        so the metric is not read as stricter than it is.
        """
        decision = {
            "ticket_id": "A-002",
            "category": "BILLING",
            "priority": "P2",
            "summary": "Duplicate charge.",
            "evidence": [{"quote": "billed twice", "supports": ["category"]}],
            "recommended_action": {"text": "x", "kb_ids": ["KB-BILL-01"]},
            "confidence": 0.9,
            "needs_human_review": "false",
            "flags": [],
        }
        assert score_raw_decision(self._case(), decision, kb, mode="test").schema_valid is True

    def test_evidence_item_missing_supports_is_caught(self, kb) -> None:
        decision = {
            "ticket_id": "A-002",
            "category": "BILLING",
            "priority": "P2",
            "summary": "Duplicate charge.",
            "evidence": [{"quote": "billed twice"}],
            "recommended_action": {"text": "x", "kb_ids": ["KB-BILL-01"]},
            "confidence": 0.9,
            "needs_human_review": False,
            "flags": [],
        }
        assert score_raw_decision(self._case(), decision, kb, mode="test").schema_valid is False

    def test_extra_top_level_field_is_caught(self, kb) -> None:
        decision = {
            "ticket_id": "A-002",
            "category": "BILLING",
            "priority": "P2",
            "summary": "Duplicate charge.",
            "evidence": [{"quote": "billed twice", "supports": ["category"]}],
            "recommended_action": {"text": "x", "kb_ids": ["KB-BILL-01"]},
            "confidence": 0.9,
            "needs_human_review": False,
            "flags": [],
            "internal_debug": "should not be here",
        }
        assert score_raw_decision(self._case(), decision, kb, mode="test").schema_valid is False

    def test_a_valid_decision_still_passes(self, kb) -> None:
        decision = {
            "ticket_id": "A-002",
            "category": "BILLING",
            "priority": "P2",
            "summary": "Duplicate charge.",
            "evidence": [{"quote": "billed twice", "supports": ["category"]}],
            "recommended_action": {"text": "x", "kb_ids": ["KB-BILL-01"]},
            "confidence": 0.9,
            "needs_human_review": False,
            "flags": [],
        }
        score = score_raw_decision(self._case(), decision, kb, mode="test")
        assert score.schema_valid is True
        assert score.closed_vocab_valid is True


class TestKbSelectionIsScored:
    """`expected_kb_ids_include` was loaded from the case file and never read.

    The allowlist rejects an invented id, but a legitimate article chosen for the
    wrong situation passed every check. That is what happened on A-012, where
    KB-AUTH-02 (single-user login) was selected for a whole-tenant outage.
    """

    def _base(self, kb_ids: list[str]) -> dict:
        return {
            "ticket_id": "A-002",
            "category": "BILLING",
            "priority": "P2",
            "summary": "Duplicate charge.",
            "evidence": [],
            "recommended_action": {"text": "x", "kb_ids": kb_ids},
            "confidence": 0.9,
            "needs_human_review": False,
            "flags": [],
        }

    def _case(self, case_id: str):
        return {c.case_id: c for c in load_cases("authored")}[case_id]

    def test_expected_article_present(self, kb) -> None:
        score = score_raw_decision(
            self._case("A-002"), self._base(["KB-BILL-01"]), kb, mode="test"
        )
        assert score.expected_kb_ids_present is True

    def test_valid_but_wrong_article_is_now_caught(self, kb) -> None:
        # Passes the allowlist, fails the expectation. Previously invisible.
        score = score_raw_decision(
            self._case("A-002"), self._base(["KB-PRODUCT-01"]), kb, mode="test"
        )
        assert score.kb_ids_unknown == 0
        assert score.expected_kb_ids_present is False

    def test_ruled_out_article_is_detected(self, kb) -> None:
        # A-012 rules out KB-AUTH-02: it is the single-user login article, and its
        # steps actively misfire on a whole-tenant outage.
        case = self._case("A-012")
        assert "KB-AUTH-02" in case.must_not_kb_ids
        decision = self._base(["KB-AUTH-02"])
        decision["ticket_id"] = "A-012"
        score = score_raw_decision(case, decision, kb, mode="test")
        assert score.forbidden_kb_ids_used == ("KB-AUTH-02",)

    def test_permitted_article_is_not_flagged_as_ruled_out(self, kb) -> None:
        case = self._case("A-012")
        decision = self._base(["KB-AUTH-01"])
        decision["ticket_id"] = "A-012"
        score = score_raw_decision(case, decision, kb, mode="test")
        assert score.forbidden_kb_ids_used == ()


class TestConfigBounds:
    """Out-of-range overrides fall back to the default rather than applying."""

    @pytest.mark.parametrize(
        ("name", "value", "attr"),
        [
            ("TICKETPILOT_MAX_TOKENS", "-10", "max_tokens"),
            ("TICKETPILOT_MAX_TOKENS", "0", "max_tokens"),
            ("TICKETPILOT_MAX_TOKENS", "999999", "max_tokens"),
            ("TICKETPILOT_TIMEOUT_SECONDS", "0", "timeout_seconds"),
            ("TICKETPILOT_TIMEOUT_SECONDS", "-5", "timeout_seconds"),
            ("TICKETPILOT_CONFIDENCE_THRESHOLD", "7", "confidence_threshold"),
            ("TICKETPILOT_CONFIDENCE_THRESHOLD", "-1", "confidence_threshold"),
        ],
    )
    def test_out_of_range_falls_back(self, monkeypatch, name: str, value: str, attr: str) -> None:
        monkeypatch.delenv(name, raising=False)
        default = getattr(Settings.from_env(), attr)
        monkeypatch.setenv(name, value)
        assert getattr(Settings.from_env(), attr) == default

    @pytest.mark.parametrize(
        ("name", "value", "attr", "expected"),
        [
            ("TICKETPILOT_MAX_TOKENS", "4096", "max_tokens", 4096),
            ("TICKETPILOT_TIMEOUT_SECONDS", "30", "timeout_seconds", 30.0),
            ("TICKETPILOT_CONFIDENCE_THRESHOLD", "0.5", "confidence_threshold", 0.5),
            ("TICKETPILOT_CONFIDENCE_THRESHOLD", "0", "confidence_threshold", 0.0),
            ("TICKETPILOT_CONFIDENCE_THRESHOLD", "1", "confidence_threshold", 1.0),
        ],
    )
    def test_in_range_is_honoured(
        self, monkeypatch, name: str, value: str, attr: str, expected: object
    ) -> None:
        monkeypatch.setenv(name, value)
        assert getattr(Settings.from_env(), attr) == expected


class TestRunRecordsCarryNoStaleScore:
    """A score embedded in a run record goes stale when a label is corrected."""

    def test_committed_live_records_have_no_embedded_score(self) -> None:
        import json

        for mode in ("baseline", "final"):
            path = Path(repo_root()) / "artifacts" / "live-evaluation" / mode / "results.jsonl"
            if not path.is_file():
                pytest.skip(f"{mode} artifact not present")
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    assert "score" not in json.loads(line)["diagnostics"], mode

    def test_absent_stored_score_is_not_reported_as_a_verdict_change(self) -> None:
        """No stored score means nothing to compare, not everything changed.

        Removing the embedded score left ``rescore.py`` comparing every field against
        ``None``, so it reported ``category_correct: None -> True`` for all four fields
        of all twenty cases. A reader of ``metrics.json`` would see a hundred entries
        under ``verdict_changes`` and conclude the label corrections moved a hundred
        verdicts. They moved three.
        """
        import json

        for mode in ("baseline", "final"):
            path = Path(repo_root()) / "artifacts" / "live-evaluation" / mode / "metrics.json"
            if not path.is_file():
                pytest.skip(f"{mode} artifact not present")
            metrics = json.loads(path.read_text(encoding="utf-8"))
            changes = metrics.get("verdict_changes", [])
            assert not [c for c in changes if "None ->" in c], (mode, changes[:3])
            # A comparison that could not run has to say so rather than stay silent.
            assert metrics.get("verdict_changes_note"), mode
