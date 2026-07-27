"""Provider abstraction: the configurable LLM client boundary.

``build_provider`` is the only place that decides which implementation runs, so
switching between the real API and the offline mock is configuration rather than
a code change (assignment section 1).
"""

from __future__ import annotations

from ..config import Settings
from .base import LLMProvider, ProviderFailure, ProviderResult
from .mock_provider import MockProvider, Script

__all__ = [
    "LLMProvider",
    "MockProvider",
    "ProviderFailure",
    "ProviderResult",
    "Script",
    "build_provider",
]


def build_provider(settings: Settings) -> LLMProvider:
    """Instantiate the provider named by configuration.

    The Anthropic client is imported lazily so that the offline path — and the
    entire test suite — never needs the SDK to be importable or an API key to be
    present.
    """
    name = settings.provider.lower()
    if name in {"mock", "offline"}:
        return MockProvider()
    if name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    raise ValueError(
        f"unknown provider {settings.provider!r}; expected 'anthropic' or 'mock'"
    )
