"""The committed JSON Schema files must match the models that generate them.

These exist because the schemas *were* stale: they had been generated before
``ModelTriageOutput`` moved from ``extra="forbid"`` to ``extra="ignore"`` and
before the docstrings changed, so the committed files described a contract the
code no longer implemented. Anyone reading ``schemas/`` to understand the API
would have been misled.

A generated artifact that is committed needs a test asserting it is current,
otherwise it silently rots. Regenerate with::

    python scripts/generate_schemas.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ticketpilot.config import repo_root
from ticketpilot.models import ModelTriageOutput, TriageDecision

SCHEMA_DIR = Path(repo_root()) / "schemas"

# Must mirror scripts/generate_schemas.py. A schema added there but not here is
# caught by test_every_committed_schema_is_covered below.
EXPECTED: dict[str, type[BaseModel]] = {
    "triage_decision.schema.json": TriageDecision,
    "model_triage_output.schema.json": ModelTriageOutput,
}

REGENERATE = "run `python scripts/generate_schemas.py` and commit schemas/"


def load_schema(filename: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))


class TestSchemasAreCurrent:
    @pytest.mark.parametrize(("filename", "model"), sorted(EXPECTED.items()))
    def test_committed_schema_matches_the_model(
        self, filename: str, model: type[BaseModel]
    ) -> None:
        assert load_schema(filename) == model.model_json_schema(), (
            f"{filename} is out of date with {model.__name__}: {REGENERATE}"
        )

    def test_every_committed_schema_is_covered(self) -> None:
        # A schema file with no model behind it would never be checked for
        # staleness, which is the failure mode this whole module exists for.
        on_disk = {p.name for p in SCHEMA_DIR.glob("*.schema.json")}
        assert on_disk == set(EXPECTED), (
            f"schemas/ and this test disagree: {on_disk ^ set(EXPECTED)}"
        )


class TestModelOutputContractShape:
    """Guards the specific properties the schema is supposed to encode."""

    def test_model_schema_omits_ticket_id_and_action_text(self) -> None:
        schema = load_schema("model_triage_output.schema.json")
        assert "ticket_id" not in schema["properties"]
        assert "recommended_action" not in schema["properties"]

    def test_every_model_output_field_is_required(self) -> None:
        # The bug this replaced: evidence, kb_ids and flags were defaulted, so a
        # response omitting them validated and Pydantic substituted empty lists —
        # making an incomplete response indistinguishable from a complete one that
        # genuinely had nothing to report, and skipping the repair it should earn.
        schema = load_schema("model_triage_output.schema.json")
        assert set(schema["required"]) == set(schema["properties"])
        for field in ("evidence", "kb_ids", "flags"):
            assert field in schema["required"], field

    def test_decision_schema_requires_all_nine_contract_fields(self) -> None:
        schema = load_schema("triage_decision.schema.json")
        assert set(schema["required"]) == {
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

    def test_recommended_action_requires_kb_ids(self) -> None:
        schema = load_schema("triage_decision.schema.json")
        action = schema["$defs"]["RecommendedAction"]
        assert set(action["required"]) == {"text", "kb_ids"}

    def test_closed_vocabularies_are_enums_in_the_schema(self) -> None:
        # The generation-time half of the closed-vocabulary guarantee: the API
        # constrains these, and code re-validates them afterwards regardless.
        schema = load_schema("model_triage_output.schema.json")
        assert set(schema["$defs"]["Category"]["enum"]) == {
            "AUTH", "BILLING", "DATA_EXPORT", "SECURITY",
            "BUG", "FEATURE", "OTHER", "UNKNOWN",
        }
        assert set(schema["$defs"]["Priority"]["enum"]) == {"P0", "P1", "P2", "P3", "UNKNOWN"}
        assert set(schema["$defs"]["Flag"]["enum"]) == {
            "PROMPT_INJECTION", "MISSING_INFO", "NO_KB_SUPPORT", "CONFLICTING_SIGNALS",
        }

    def test_confidence_bounds_are_published(self) -> None:
        schema = load_schema("model_triage_output.schema.json")
        confidence = schema["properties"]["confidence"]
        assert confidence["minimum"] == 0.0
        assert confidence["maximum"] == 1.0


class TestIncompleteOutputNowFailsValidation:
    """The behavioural consequence of making the list fields required."""

    def _complete(self) -> dict[str, Any]:
        return {
            "category": "BILLING",
            "priority": "P2",
            "summary": "Duplicate charge.",
            "evidence": [{"quote": "charged twice", "supports": ["category"]}],
            "kb_ids": ["KB-BILL-01"],
            "confidence": 0.9,
            "needs_human_review": False,
            "flags": [],
        }

    def test_complete_output_validates(self) -> None:
        assert ModelTriageOutput.model_validate(self._complete())

    @pytest.mark.parametrize("omitted", ["evidence", "kb_ids", "flags"])
    def test_omitting_a_list_field_is_now_a_validation_error(self, omitted: str) -> None:
        payload = self._complete()
        del payload[omitted]
        with pytest.raises(Exception):
            ModelTriageOutput.model_validate(payload)

    def test_empty_lists_are_still_accepted(self) -> None:
        # Empty is a legitimate answer; absent is not. The distinction is the
        # entire point of the change.
        payload = self._complete()
        payload.update(evidence=[], kb_ids=[], flags=[])
        assert ModelTriageOutput.model_validate(payload)
