"""The real Claude client.

Notes that shaped this implementation, verified against the installed SDK
(anthropic 0.120.0) rather than assumed:

* ``messages.parse(output_format=Model)`` builds the JSON-Schema constraint from
  the Pydantic model and returns a validated instance on ``parsed_output``. It
  also normalises schema features the API does not accept, which is why it is
  preferred over hand-building ``output_config.format``.
* ``output_config`` and ``output_format`` compose: the SDK merges them
  (``{**output_config, "format": ...}``), so ``effort`` and schema-constrained
  output can be set together.
* **Opus 5 rejects ``temperature``, ``top_p`` and ``top_k`` with a 400.** None is
  sent. There is therefore no sampling-determinism lever available, which is why
  run-to-run stability has to come from the schema constraint plus the
  deterministic post-layer.
* ``stop_reason == "refusal"`` arrives as a normal HTTP 200. It must be checked
  *before* reading ``content``, which may be empty or partial.

The SDK's built-in retry (default 2 attempts, exponential backoff on 429/408/5xx
and connection errors) is the retry layer. It is deliberately distinct from the
pipeline's single *repair* call: a retry re-sends an unchanged request after a
transport fault, while a repair sends a new instruction after a semantically bad
response.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..config import Settings
from .base import ProviderFailure, ProviderResult


class AnthropicProvider:
    """Provider backed by the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        if client is not None:
            self._client = client
        else:
            import anthropic

            # Credentials resolve from the environment (ANTHROPIC_API_KEY, or an
            # `ant auth login` profile). No key is read or stored by this code.
            self._client = anthropic.Anthropic(timeout=settings.timeout_seconds)

    def generate(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        output_model: type[BaseModel] | None = None,
    ) -> ProviderResult:
        import anthropic

        request: dict[str, Any] = {
            "model": self._settings.model,
            "max_tokens": self._settings.max_tokens,
            # A single cache breakpoint on the system block. The policy and
            # knowledge base are byte-stable across requests, so repeated
            # evaluation runs read the prefix from cache instead of re-paying for
            # it. This is also why the canary goes last in the system prompt and
            # why the KB is serialised deterministically.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self._settings.effort},
        }

        try:
            if output_model is not None:
                response = self._client.messages.parse(
                    output_format=output_model, **request
                )
            else:
                response = self._client.messages.create(**request)
        except anthropic.APITimeoutError as exc:
            return self._failure(ProviderFailure.TIMEOUT, exc)
        except anthropic.APIConnectionError as exc:
            return self._failure(ProviderFailure.CONNECTION, exc)
        except anthropic.APIStatusError as exc:
            # Includes 4xx that the SDK does not retry and 5xx that survived
            # retries. Either way there is no candidate response to repair.
            return self._failure(
                ProviderFailure.API_ERROR, exc, detail=f"{type(exc).__name__}: {exc.status_code}"
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Failure safety is a contract of this boundary: an unexpected
            # exception must still become a ProviderResult, never propagate and
            # crash the service.
            return self._failure(ProviderFailure.API_ERROR, exc)

        return self._interpret(response, output_model)

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _failure(
        kind: ProviderFailure, exc: BaseException, detail: str | None = None
    ) -> ProviderResult:
        return ProviderResult(
            failure=kind,
            failure_detail=detail or f"{type(exc).__name__}: {exc}",
        )

    def _interpret(
        self, response: Any, output_model: type[BaseModel] | None
    ) -> ProviderResult:
        stop_reason = getattr(response, "stop_reason", None)
        usage = self._usage(response)
        model = getattr(response, "model", None) or self._settings.model

        # Checked before touching content: on a refusal the content list may be
        # empty or hold a partial answer, and indexing it would raise.
        if stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return ProviderResult(
                failure=ProviderFailure.REFUSAL,
                failure_detail=f"stop_reason=refusal category={category}",
                model=model,
                usage=usage,
                stop_reason=stop_reason,
            )

        text = self._text_of(response)
        if not text:
            return ProviderResult(
                failure=ProviderFailure.EMPTY_CONTENT,
                failure_detail=f"no text content (stop_reason={stop_reason})",
                model=model,
                usage=usage,
                stop_reason=stop_reason,
            )

        parsed = getattr(response, "parsed_output", None) if output_model else None
        # A successful call whose body did not validate is *not* a provider
        # failure: a candidate came back, so it earns the repair attempt. Left as
        # text with parsed=None for the pipeline to decide.
        return ProviderResult(
            text=text,
            parsed=parsed,
            model=model,
            usage=usage,
            stop_reason=stop_reason,
        )

    @staticmethod
    def _text_of(response: Any) -> str:
        """Concatenate text blocks, skipping thinking and any other block type.

        Thinking blocks precede text and carry empty content by default on Opus 5
        (``display`` defaults to ``"omitted"``), so filtering by type rather than
        indexing ``content[0]`` is required, not merely tidier.
        """
        parts: list[str] = []
        for block in getattr(response, "content", None) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts).strip()

    @staticmethod
    def _usage(response: Any) -> dict[str, int]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        return {
            name: value
            for name in fields
            if isinstance(value := getattr(usage, name, None), int)
        }
