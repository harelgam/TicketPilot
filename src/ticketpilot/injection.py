"""Layer 2 of the prompt-injection defence: a deterministic detector.

Scope, stated precisely because it is easy to overrate this module
--------------------------------------------------------------------
This detector catches *obvious, well-known phrasings*. It cannot be
comprehensive: the same instruction can be expressed countless ways in English
or Hebrew, and an attacker who reads this file can trivially avoid every pattern
below. It is therefore not a security boundary.

What it deliberately does **not** do:

* judge whether the ticket is malicious
* modify, redact, or truncate the ticket
* block the LLM call

All it does is contribute ``PROMPT_INJECTION`` and force human review when a
pattern matches. Flags are combined as a union with whatever the model reported
(``model_flags | deterministic_flags``), so either layer detecting is enough.

The defence that does not depend on detection is Layer 3 — the invariants in
``pipeline.py`` and ``review.py``. Even when this module and the model both
miss an attack, the model cannot change the ticket id, emit a value outside a
closed vocabulary, invent a KB id, fabricate evidence, author the action text,
or cancel a policy-required review. That containment, not this regex list, is
what bounds the damage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (name, pattern) pairs. Names are recorded in the run record so a match can be
# explained after the fact rather than appearing as an unexplained flag.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # --- English: instruction override ---
    ("ignore_previous_instructions", re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+instructions", re.I)),
    ("disregard_instructions", re.compile(r"disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|the)?\s*instructions", re.I)),
    ("forget_instructions", re.compile(r"forget\s+(?:all\s+|everything\s+)?(?:previous|prior|above|your)?\s*instructions", re.I)),
    ("override_instructions", re.compile(r"override\s+(?:all\s+|any\s+|the\s+)?(?:previous\s+|prior\s+)?instructions", re.I)),
    ("new_instructions", re.compile(r"(?:here\s+are\s+your\s+)?new\s+instructions\s*:", re.I)),
    # --- English: prompt disclosure ---
    ("reveal_system_prompt", re.compile(r"(?:print|show|reveal|repeat|output|display|echo|summari[sz]e)\b[^.\n]{0,40}\b(?:system|developer|initial|hidden|original)\s+(?:prompt|instructions|message)", re.I)),
    ("repeat_everything_above", re.compile(r"repeat\s+(?:everything|all|the\s+text)\s+above", re.I)),
    ("what_are_your_instructions", re.compile(r"what\s+(?:are|were)\s+your\s+(?:system\s+)?instructions", re.I)),
    # --- English: forcing a specific decision ---
    ("force_priority", re.compile(r"\b(?:return|set|use|output|assign|classify\s+(?:it\s+)?as)\s+(?:priority\s+)?(?:P[0-3]|UNKNOWN)\b", re.I)),
    ("force_category", re.compile(r"\b(?:return|set|use|output|assign)\s+(?:category\s+)?(?:AUTH|BILLING|DATA_EXPORT|SECURITY|BUG|FEATURE|OTHER|UNKNOWN)\b")),
    ("force_no_review", re.compile(r"(?:needs_human_review|human\s+review)\s*(?:=|:|\s+to\s+)\s*(?:false|no)", re.I)),
    # --- English: role manipulation ---
    # Anchored to a clause boundary because an injection is an imperative:
    # "Act as a billing administrator." A bare \b(act|behave)\s+as\b also
    # matches ordinary support prose — "Our admin can act as a delegate for
    # other users" is a real SSO question, and flagging it would force needless
    # review on legitimate tickets. Optional politeness words are allowed after
    # the boundary since "Please act as..." is equally imperative.
    (
        "act_as",
        re.compile(
            r"(?:^|[.!?\n]\s*)(?:please\s+|now\s+|kindly\s+|you\s+must\s+)?"
            r"(?:act|behave|respond)\s+as\s+(?:a\s+|an\s+|if\s+)",
            re.I,
        ),
    ),
    ("you_are_now", re.compile(r"you\s+are\s+now\s+(?:a\s+|an\s+|no\s+longer)", re.I)),
    ("pretend", re.compile(r"pretend\s+(?:to\s+be|that\s+you)", re.I)),
    ("developer_mode", re.compile(r"\b(?:developer|debug|god|dan)\s+mode\b", re.I)),
    # A chat-role marker inside ticket text is a fake-turn attempt: legitimate
    # customer prose does not begin a line with "system:" or "assistant:".
    ("fake_role_marker", re.compile(r"(?:^|\n)\s*(?:system|assistant|human|user)\s*:", re.I)),
    # --- Hebrew: instruction override ---
    # התעלם מ... = "ignore ..."
    ("he_ignore", re.compile(r"התעלם\s+מ")),
    # שכח את ההוראות = "forget the instructions"
    ("he_forget_instructions", re.compile(r"שכח\s+(?:את\s+)?(?:כל\s+)?ה?הוראות")),
    # הוראות קודמות / ההוראות הקודמות = "previous instructions"
    ("he_previous_instructions", re.compile(r"ה?הוראות\s+ה?קודמות")),
    # --- Hebrew: prompt disclosure ---
    # פרומפט המערכת / הנחיות המערכת = "system prompt" / "system instructions"
    ("he_system_prompt", re.compile(r"(?:פרומפט|הנחיות|הוראות)\s+ה?מערכת")),
    # הצג / הדפס / גלה / חשוף = show / print / reveal / expose
    ("he_reveal", re.compile(r"(?:הצג|הדפס|גלה|חשוף|חזור\s+על)\s+[^.\n]{0,30}(?:פרומפט|הנחיות|הוראות)")),
    # --- Hebrew: forcing a decision ---
    # החזר P3 = "return P3"
    ("he_force_priority", re.compile(r"החזר\s+(?:עדיפות\s+)?(?:P[0-3]|UNKNOWN)", re.I)),
    # --- Hebrew: role manipulation ---
    # התנהג כאילו / שחק תפקיד = "behave as if" / "play a role"
    ("he_act_as", re.compile(r"(?:התנהג|התייחס)\s+כ|שחק\s+תפקיד")),
)


@dataclass(frozen=True)
class InjectionScan:
    """Result of the deterministic scan."""

    detected: bool
    matched_patterns: tuple[str, ...] = field(default=())

    def as_diagnostic(self) -> dict[str, object]:
        """Shape recorded in the run record."""
        return {
            "detected": self.detected,
            "matched_patterns": list(self.matched_patterns),
        }


def scan(text: str) -> InjectionScan:
    """Scan untrusted ticket text for known injection phrasings.

    Returns every pattern name that matched, not just the first, so the run
    record shows how strong the signal was rather than only that it fired.
    """
    if not text:
        return InjectionScan(detected=False)

    matched = tuple(name for name, pattern in _PATTERNS if pattern.search(text))
    return InjectionScan(detected=bool(matched), matched_patterns=matched)


def pattern_names() -> tuple[str, ...]:
    """All pattern names, for tests and documentation."""
    return tuple(name for name, _ in _PATTERNS)
