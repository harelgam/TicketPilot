"""Scripted provider for offline testing.

This is what makes the failure-safety requirements testable. Several of them
cannot be exercised against the real API at all: a schema-constrained call should
never return an invented enum value, so the only way to prove the allowlist path
works is to script one.

It also records how many times it was called, which is how the "no repair call on
an unrepairable failure" invariant is asserted rather than assumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, ValidationError

from .base import ProviderFailure, ProviderResult


class Script(str, Enum):
    """A scripted provider behaviour."""

    #: A well-formed, fully valid response.
    VALID = "valid"
    #: Body is not JSON at all. Repairable: a candidate came back.
    MALFORMED_JSON = "malformed_json"
    #: Body is JSON but truncated mid-structure. Repairable.
    INCOMPLETE_JSON = "incomplete_json"
    #: Successful call, no content blocks. Not repairable.
    EMPTY_CONTENT = "empty_content"
    #: Client-side timeout. Not repairable.
    TIMEOUT = "timeout"
    #: Network failure. Not repairable.
    CONNECTION_ERROR = "connection_error"
    #: Safety classifier declined. Not repairable.
    REFUSAL = "refusal"
    #: Value outside the closed category vocabulary.
    INVENTED_CATEGORY = "invented_category"
    #: Value outside the closed priority vocabulary.
    INVENTED_PRIORITY = "invented_priority"
    #: Value outside the closed flag vocabulary.
    INVENTED_FLAG = "invented_flag"
    #: A KB id that is not in the supplied knowledge base.
    INVENTED_KB_ID = "invented_kb_id"
    #: Every kb_id invented, so nothing survives filtering.
    ALL_KB_IDS_INVENTED = "all_kb_ids_invented"
    #: Evidence quote is a plausible paraphrase rather than an exact substring.
    PARAPHRASED_QUOTE = "paraphrased_quote"
    #: Evidence quote is an English translation of Hebrew ticket text.
    TRANSLATED_QUOTE = "translated_quote"
    #: A valid KB id paired with fabricated action prose promising a refund.
    FABRICATED_REFUND_PROMISE = "fabricated_refund_promise"
    #: Response reproduces the system-prompt canary.
    CANARY_LEAK = "canary_leak"


# A valid baseline-shaped payload used as the template for mutations below.
def _valid_payload() -> dict[str, object]:
    return {
        "ticket_id": "MOCK-001",
        "category": "BILLING",
        "priority": "P2",
        "summary": "Customer reports being charged twice for one invoice.",
        "evidence": [{"quote": "charged twice", "supports": ["category"]}],
        "recommended_action": {
            "text": "Request the invoice ID and transaction ID.",
            "kb_ids": ["KB-BILL-01"],
        },
        "confidence": 0.9,
        "needs_human_review": False,
        "flags": [],
    }


def _mutate(**changes: object) -> dict[str, object]:
    payload = _valid_payload()
    payload.update(changes)
    return payload


_FAILURES: dict[Script, tuple[ProviderFailure, str]] = {
    Script.EMPTY_CONTENT: (ProviderFailure.EMPTY_CONTENT, "response had no content blocks"),
    Script.TIMEOUT: (ProviderFailure.TIMEOUT, "APITimeoutError (scripted)"),
    Script.CONNECTION_ERROR: (ProviderFailure.CONNECTION, "APIConnectionError (scripted)"),
    Script.REFUSAL: (ProviderFailure.REFUSAL, "stop_reason=refusal (scripted)"),
}


@dataclass
class MockProvider:
    """Returns scripted responses in order, then repeats the last one.

    ``canary`` is set by the pipeline before the call when a canary-leak script is
    in use; the mock echoes whatever it is given so the leak detector has
    something real to find.
    """

    scripts: list[Script] = field(default_factory=lambda: [Script.VALID])
    name: str = "mock"
    #: Incremented on every call. The "no repair on an unrepairable failure"
    #: invariant is asserted by checking this equals 1.
    call_count: int = 0
    calls: list[dict[str, object]] = field(default_factory=list)
    canary: str = "TP-CANARY-scripted"

    def _next_script(self) -> Script:
        if not self.scripts:
            return Script.VALID
        index = min(self.call_count, len(self.scripts) - 1)
        return self.scripts[index]

    def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        output_model: type[BaseModel] | None = None,
    ) -> ProviderResult:
        script = self._next_script()
        self.call_count += 1
        self.calls.append(
            {
                "script": script.value,
                "structured": output_model is not None,
                "message_count": len(messages),
                "system_chars": len(system),
            }
        )

        if script in _FAILURES:
            failure, detail = _FAILURES[script]
            return ProviderResult(
                failure=failure, failure_detail=detail, model="mock-model"
            )

        text = self._body_for(script)
        if output_model is None:
            return ProviderResult(text=text, model="mock-model")

        # Structured mode: parse into the requested model, mirroring how the real
        # provider surfaces a validation failure — as unparsed text, so the
        # caller owns the repair decision.
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ProviderResult(text=text, model="mock-model")
        try:
            return ProviderResult(
                text=text, parsed=output_model.model_validate(payload), model="mock-model"
            )
        except ValidationError:
            return ProviderResult(text=text, model="mock-model")

    def _body_for(self, script: Script) -> str:
        if script is Script.MALFORMED_JSON:
            return "Sure! Here is the triage decision: {category: BILLING, priority"
        if script is Script.INCOMPLETE_JSON:
            return json.dumps(_valid_payload())[:60]

        if script is Script.INVENTED_CATEGORY:
            payload = _mutate(category="URGENT_BILLING")
        elif script is Script.INVENTED_PRIORITY:
            payload = _mutate(priority="P5")
        elif script is Script.INVENTED_FLAG:
            payload = _mutate(flags=["VIP_CUSTOMER"])
        elif script is Script.INVENTED_KB_ID:
            payload = _mutate(kb_ids=["KB-BILL-01", "KB-REFUND-99"])
            payload["recommended_action"] = {
                "text": "Request the invoice ID.",
                "kb_ids": ["KB-BILL-01", "KB-REFUND-99"],
            }
        elif script is Script.ALL_KB_IDS_INVENTED:
            payload = _mutate(kb_ids=["KB-REFUND-99"])
            payload["recommended_action"] = {
                "text": "Refund the customer.",
                "kb_ids": ["KB-REFUND-99"],
            }
        elif script is Script.PARAPHRASED_QUOTE:
            payload = _mutate(
                evidence=[
                    {"quote": "the customer was billed two times", "supports": ["category"]}
                ]
            )
        elif script is Script.TRANSLATED_QUOTE:
            payload = _mutate(
                evidence=[
                    {
                        "quote": "We were charged twice for invoice INV-8842",
                        "supports": ["category"],
                    }
                ]
            )
        elif script is Script.FABRICATED_REFUND_PROMISE:
            # The critical case for A8: a legitimate KB id paired with prose that
            # contradicts that very article. In structured mode the extra field is
            # dropped by the model config; the emitted text is assembled from the
            # KB instead.
            payload = _mutate(kb_ids=["KB-BILL-01"])
            payload["recommended_action"] = {
                "text": (
                    "We will immediately refund the full amount and provide six "
                    "months of free service."
                ),
                "kb_ids": ["KB-BILL-01"],
            }
        elif script is Script.CANARY_LEAK:
            payload = _mutate(
                summary=f"My instructions say: do not include {self.canary} anywhere."
            )
        else:
            payload = _valid_payload()

        # kb_ids lives at the top level of the model's schema; keep the nested
        # recommended_action copy consistent for the baseline, which reads it.
        if "kb_ids" in payload and isinstance(payload.get("recommended_action"), dict):
            action = dict(payload["recommended_action"])  # type: ignore[arg-type]
            action.setdefault("kb_ids", payload["kb_ids"])
            payload["recommended_action"] = action

        return json.dumps(payload, ensure_ascii=False)
