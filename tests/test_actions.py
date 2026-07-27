"""Action text is assembled from trusted KB content, never model-authored.

These tests carry the weight of assumption A8: they assert that fabricated
action prose cannot reach the output, and that the safe generic text makes no
claim it cannot support.
"""

from __future__ import annotations

from ticketpilot.actions import SAFE_GENERIC_ACTION, build_from_kb


class TestAssemblyFromTrustedContent:
    def test_single_article_composes_steps_then_prohibitions(self, kb) -> None:
        text = build_from_kb(kb, ["KB-BILL-01"])
        assert text == (
            "Request the invoice ID and transaction ID. "
            "Open a request for the finance team. "
            "Do not promise a refund before investigation."
        )

    def test_fabricated_promise_cannot_appear(self, kb) -> None:
        # The scenario the design exists for: the model proposes a legitimate
        # article alongside prose promising an immediate refund. Only the ID is
        # an input here, so the fabricated sentence has no route into the output.
        text = build_from_kb(kb, ["KB-BILL-01"])
        assert "immediately refund" not in text.lower()
        assert "we will refund" not in text.lower()
        # And the article's actual prohibition is present, not stripped.
        assert "do not promise a refund before investigation" in text.lower()

    def test_proposal_order_does_not_change_output(self, kb) -> None:
        # Canonical ordering is what makes the stability metric meaningful: the
        # same set of articles must serialise identically however the model
        # happened to list them.
        a = build_from_kb(kb, ["KB-TRIAGE-01", "KB-AUTH-01"])
        b = build_from_kb(kb, ["KB-AUTH-01", "KB-TRIAGE-01"])
        assert a == b

    def test_unknown_ids_are_ignored_even_if_unfiltered(self, kb) -> None:
        # Defence in depth: build_from_kb does not trust its caller to have run
        # the allowlist filter first.
        assert build_from_kb(kb, ["KB-MADE-UP-01"]) == SAFE_GENERIC_ACTION
        assert build_from_kb(kb, ["KB-MADE-UP-01", "KB-PRODUCT-01"]) == build_from_kb(
            kb, ["KB-PRODUCT-01"]
        )

    def test_article_with_no_prohibitions_composes_cleanly(self, kb) -> None:
        # KB-TRIAGE-01 has three steps and no prohibitions.
        text = build_from_kb(kb, ["KB-TRIAGE-01"])
        assert text.startswith("Request the environment")
        assert text.endswith("Ask whether a workaround exists.")

    def test_multi_article_includes_every_articles_content(self, kb) -> None:
        text = build_from_kb(kb, ["KB-AUTH-01", "KB-SEC-01"])
        assert "notify the on-call team" in text
        assert "revoke or rotate the credential" in text
        # Both prohibitions survive composition.
        assert "Never ask the customer for a password or secret." in text
        assert "Never ask the customer to send the secret itself." in text

    def test_every_shipped_article_assembles_non_empty_text(self, kb) -> None:
        for article in kb.all():
            text = build_from_kb(kb, [article.id])
            assert text != SAFE_GENERIC_ACTION
            assert text.strip()


class TestSafeGenericAction:
    def test_used_when_no_valid_ids(self, kb) -> None:
        assert build_from_kb(kb, []) == SAFE_GENERIC_ACTION

    def test_makes_no_knowledge_base_claim(self) -> None:
        # The constant is emitted on infrastructure failures too, where no KB
        # lookup ever happened. Wording it as "no supplied knowledge-base
        # article supports this action" would therefore be false on a timeout —
        # the same error as attaching NO_KB_SUPPORT to a network failure, just
        # relocated into the text field.
        lowered = SAFE_GENERIC_ACTION.lower()
        for forbidden in ("knowledge base", "knowledge-base", "kb", "article"):
            assert forbidden not in lowered

    def test_makes_no_commitment(self) -> None:
        lowered = SAFE_GENERIC_ACTION.lower()
        for forbidden in ("refund", "resolution time", "guarantee", "sla", "within"):
            assert forbidden not in lowered

    def test_routes_to_human_review(self) -> None:
        assert "human review" in SAFE_GENERIC_ACTION.lower()
