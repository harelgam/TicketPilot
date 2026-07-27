"""Typed models for the triage contract.

Two distinct output models exist deliberately:

``ModelTriageOutput``
    What the LLM is asked to produce. It omits ``ticket_id`` and the
    recommended-action *text* entirely, because both are owned by the
    application (see README assumptions A0 and A8). A field the model never
    sees cannot be hallucinated, and cannot be redirected by an instruction
    injected into the ticket.

``TriageDecision``
    The nine-field response contract from section 3 of the assignment. It is
    constructed by the deterministic layer, never parsed from model output.

The three closed vocabularies below are the application's, not the model's.
They are declared here once and used in three places: the schema-constrained
request, post-parse re-validation, and the repair prompt's list of legal
values. Declaring them once is what keeps those three from drifting apart.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Category(str, Enum):
    """Allowed category values (assignment section 3)."""

    AUTH = "AUTH"
    BILLING = "BILLING"
    DATA_EXPORT = "DATA_EXPORT"
    SECURITY = "SECURITY"
    BUG = "BUG"
    FEATURE = "FEATURE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class Priority(str, Enum):
    """Allowed priority values (assignment section 3)."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    UNKNOWN = "UNKNOWN"


class Flag(str, Enum):
    """Allowed safety/quality flags (assignment section 3).

    This set is closed and contains no member meaning "the model or the
    provider failed". That absence is load-bearing: on such a failure the
    system emits an empty flag list rather than borrowing a flag that would
    assert something untrue about the ticket or the knowledge base. See
    ``review.py`` for the provenance rules.
    """

    PROMPT_INJECTION = "PROMPT_INJECTION"
    MISSING_INFO = "MISSING_INFO"
    NO_KB_SUPPORT = "NO_KB_SUPPORT"
    CONFLICTING_SIGNALS = "CONFLICTING_SIGNALS"


# Convenience frozensets for the deterministic re-validation pass.
CATEGORY_VALUES: frozenset[str] = frozenset(c.value for c in Category)
PRIORITY_VALUES: frozenset[str] = frozenset(p.value for p in Priority)
FLAG_VALUES: frozenset[str] = frozenset(f.value for f in Flag)


class TicketInput(BaseModel):
    """The input contract (assignment section 2)."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1)
    text: str
    customer_tier: str | None = None


class EvidenceItem(BaseModel):
    """A quote from the ticket plus what it supports.

    ``quote`` must be an exact substring of the original ticket text; that is
    enforced in ``validation.py``, not here, because Pydantic has no access to
    the source ticket.

    ``supports`` is intentionally unconstrained. The assignment illustrates
    ``["category", "priority"]`` but never defines a closed vocabulary for it,
    so constraining it would invent a requirement — and would spend repair
    calls rejecting values that are not actually invalid. Only the quote is
    validated, which is what section 3 requires.
    """

    model_config = ConfigDict(extra="forbid")

    quote: str = Field(min_length=1)
    supports: list[str] = Field(min_length=1)


class RecommendedAction(BaseModel):
    """Action text plus KB IDs.

    Both fields are produced by the application: ``kb_ids`` is the model's
    proposal after allowlist filtering, and ``text`` is assembled from those
    validated articles by ``actions.build_from_kb``. The model never authors
    this text (A8).
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    kb_ids: list[str] = Field(default_factory=list)


class ModelTriageOutput(BaseModel):
    """What the LLM is asked to return.

    Deliberately absent:

    ``ticket_id``
        Application-owned metadata, copied from the input (A0).
    ``recommended_action.text``
        Assembled from trusted KB content; the model proposes only which
        articles apply, via ``kb_ids`` (A8).
    """

    model_config = ConfigDict(extra="forbid")

    category: Category
    priority: Priority
    summary: str = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    kb_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human_review: bool
    flags: list[Flag] = Field(default_factory=list)


class TriageDecision(BaseModel):
    """The nine-field response contract (assignment section 3).

    Field order matches the assignment's example output so that a serialised
    decision is directly comparable to the contract. Exactly these nine
    fields are emitted — diagnostics live in a separate run record (A2).
    """

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    category: Category
    priority: Priority
    summary: str
    evidence: list[EvidenceItem]
    recommended_action: RecommendedAction
    confidence: float = Field(ge=0.0, le=1.0)
    needs_human_review: bool
    flags: list[Flag]
