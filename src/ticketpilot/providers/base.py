"""The provider contract.

One method serves both pipelines: passing ``output_model`` asks for
schema-constrained output (the final pipeline), omitting it asks for free-form
text (the baseline). That keeps the two paths comparable — same client, same
model, same retry behaviour — so the evaluation measures the engineering rather
than a difference in plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class ProviderFailure(str, Enum):
    """Why a call produced nothing usable.

    The distinction that matters downstream is whether a *repairable candidate*
    came back. ``MALFORMED`` is not in this enum because malformed JSON is a
    successful call whose body failed to parse — it has a candidate, so it earns
    the repair attempt. Everything here has no candidate at all and goes straight
    to the safe fallback.
    """

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    EMPTY_CONTENT = "empty_content"
    REFUSAL = "refusal"
    API_ERROR = "api_error"


@dataclass(frozen=True)
class ProviderResult:
    """Outcome of one provider call."""

    #: Raw response text. Present in text mode; also populated in structured mode
    #: when available, for the run record.
    text: str | None = None
    #: Validated model instance. Only populated in structured mode.
    parsed: BaseModel | None = None
    #: Set when the call yielded nothing usable. None on success.
    failure: ProviderFailure | None = None
    #: Human-readable detail for the run record (exception class, stop reason).
    failure_detail: str | None = None
    #: Model id that actually served the response.
    model: str | None = None
    #: Token usage, when the provider reports it.
    usage: dict[str, int] = field(default_factory=dict)
    #: Raw stop reason. Recorded because "max_tokens" explains a truncated body
    #: that would otherwise look like an inexplicable parse failure.
    stop_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    def as_diagnostic(self) -> dict[str, object]:
        """Shape recorded in the run record. Excludes response text by default,
        which is stored separately so a record stays readable."""
        return {
            "ok": self.ok,
            "failure": self.failure.value if self.failure else None,
            "failure_detail": self.failure_detail,
            "model": self.model,
            "usage": dict(self.usage),
            "stop_reason": self.stop_reason,
        }


@runtime_checkable
class LLMProvider(Protocol):
    """What the pipelines require of a model client."""

    name: str

    def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        output_model: type[BaseModel] | None = None,
    ) -> ProviderResult:
        """Produce one response.

        ``messages`` is a list of ``{"role": ..., "content": ...}`` turns, so the
        repair call can pass the original exchange plus the correction request
        rather than re-deriving context.

        ``output_model`` requests schema-constrained output. Implementations must
        never raise for a provider-side failure: they return a
        ``ProviderResult`` carrying a ``ProviderFailure`` instead, because
        failure safety is a contract of this boundary rather than something each
        caller re-implements.
        """
        ...
