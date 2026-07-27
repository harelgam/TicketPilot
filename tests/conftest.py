"""Shared fixtures.

Every test in this suite runs offline with no ANTHROPIC_API_KEY set. That is a
requirement, not a convenience: the assignment asks for a project that runs from
documented commands on a clean environment, and a suite that needs a key cannot
demonstrate that.
"""

from __future__ import annotations

import pytest

from ticketpilot.kb import KnowledgeBase


@pytest.fixture(scope="session")
def kb() -> KnowledgeBase:
    """The real, shipped knowledge base.

    Loaded from data/kb.json rather than a stub, so these tests also assert that
    the shipped KB is structurally valid and that its IDs match what the rest of
    the code expects.
    """
    return KnowledgeBase.load()


# The Hebrew ticket text, in *logical* order. The assignment PDF renders the
# invoice number as "-8842INV" because a bidi-aware renderer displays the LTR
# run reversed inside RTL text; the underlying characters are "INV-8842". Tests
# that care about exact-substring matching must use the logical form, which is
# what a real ticket payload would contain.
HEBREW_TICKET = "חויבנו פעמיים עבור חשבונית INV-8842.\nהמערכת ממשיכה לעבוד כרגיל."


@pytest.fixture
def hebrew_ticket() -> str:
    return HEBREW_TICKET
