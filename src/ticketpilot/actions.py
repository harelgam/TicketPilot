"""Assembly of recommended-action text from trusted knowledge-base content.

The model proposes *which* articles apply (``kb_ids``); it never writes the
action text. That is the whole point of this module, and it is why an ungrounded
promise cannot appear in the output rather than merely being likely to be
caught.

Why assembly rather than validation
-----------------------------------
A valid KB ID paired with contradicting prose is still ungrounded — for example
``{"text": "We will immediately refund the full amount.", "kb_ids":
["KB-BILL-01"]}``, where KB-BILL-01 in fact says not to promise a refund before
investigation.

Detecting that in free text does not work reliably. A keyword check rejects the
*correct* sentence too, since compliant text routinely restates the
prohibition ("...and do not promise a refund before investigation"). Adding
negation-awareness narrows the fragility without removing it, and no keyword
detector catches "the refund is already approved", "you will receive the money
shortly", or "finance has guaranteed reimbursement" — promises containing no
word such a detector would know. Doing it properly means semantic grounding
validation (NLI or an LLM judge), which is non-deterministic and hard to
defend.

So the text is composed from the articles' own ``steps`` and ``prohibitions``.
The trade-off, documented in the README, is that action text is templated
rather than tailored to the individual ticket.
"""

from __future__ import annotations

from .kb import KnowledgeBase

# Emitted byte-identically on every path that cannot produce a grounded
# recommendation: no valid KB IDs, malformed model output, connection error,
# empty content, or provider refusal.
#
# It deliberately says nothing about the knowledge base. Wording it as "no
# supplied knowledge-base article supports this action" would be *false* after
# a timeout, where no KB lookup ever happened — the same error as attaching
# NO_KB_SUPPORT to an infrastructure failure, just relocated into the text
# field. The flags carry the specific reason when one is actually known; this
# text stays true on every path.
#
# It promises no refund, no resolution time, no remediation, no service level,
# and no step absent from the KB.
SAFE_GENERIC_ACTION = (
    "Automated triage could not produce a validated recommendation. "
    "Route the ticket for human review."
)


def build_from_kb(kb: KnowledgeBase, validated_ids: list[str]) -> str:
    """Compose action text from already-validated KB IDs.

    ``validated_ids`` must have passed the allowlist filter; unknown IDs are
    ignored here rather than trusted, so a caller that forgets to filter still
    cannot inject content. Ordering is canonical (KB file order), not the order
    the model proposed, so the same set of articles always yields byte-identical
    text — a precondition for the stability metric.

    Returns ``SAFE_GENERIC_ACTION`` when no valid article remains.
    """
    ordered = kb.canonical_order(validated_ids)
    sentences: list[str] = []
    for article_id in ordered:
        article = kb.get(article_id)
        if article is None:  # pragma: no cover - canonical_order filters these
            continue
        # Steps first, then prohibitions: do-this before do-not-do-that reads
        # naturally, and every KB bullet is already a complete sentence.
        sentences.extend(article.steps)
        sentences.extend(article.prohibitions)

    if not sentences:
        return SAFE_GENERIC_ACTION
    return " ".join(sentence.strip() for sentence in sentences)
