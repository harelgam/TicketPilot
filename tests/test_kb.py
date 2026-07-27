"""Knowledge base loading, validation, and fidelity to the assignment.

The fidelity tests are deliberately concrete. Because the recommended-action
text is assembled from this file rather than written by the model, an error in
kb.json becomes an error in every recommendation — so the KB's contents are
part of the tested surface, not just configuration.
"""

from __future__ import annotations

import json

import pytest

from ticketpilot.kb import KnowledgeBase, KnowledgeBaseError

# The seven IDs listed in section 4 of the assignment.
EXPECTED_IDS = {
    "KB-AUTH-01",
    "KB-AUTH-02",
    "KB-EXPORT-01",
    "KB-BILL-01",
    "KB-SEC-01",
    "KB-PRODUCT-01",
    "KB-TRIAGE-01",
}


class TestShippedKnowledgeBase:
    def test_contains_exactly_the_supplied_articles(self, kb) -> None:
        assert kb.allowed_ids == EXPECTED_IDS

    def test_every_article_has_at_least_one_step(self, kb) -> None:
        # An article with no steps could not produce an action.
        for article in kb.all():
            assert article.steps, f"{article.id} has no steps"

    def test_prohibitions_may_be_empty(self, kb) -> None:
        # KB-TRIAGE-01 has none in the assignment; the loader must not require
        # them, or we would be inventing a prohibition to satisfy a schema.
        assert kb.get("KB-TRIAGE-01").prohibitions == ()

    def test_known_prohibitions_are_transcribed(self, kb) -> None:
        assert kb.get("KB-BILL-01").prohibitions == (
            "Do not promise a refund before investigation.",
        )
        assert kb.get("KB-PRODUCT-01").prohibitions == (
            "Do not promise a delivery date.",
        )
        assert kb.get("KB-EXPORT-01").prohibitions == (
            "Do not promise a resolution time.",
        )

    def test_applicability_bullet_is_not_stored_as_a_step(self, kb) -> None:
        # KB-AUTH-01's "Use when multiple customers cannot log in to production"
        # is a selection hint, not an action. If it were a step it would leak
        # into customer-facing recommendation text.
        article = kb.get("KB-AUTH-01")
        assert article.when_to_use is not None
        assert "Use when" in article.when_to_use
        assert not any(step.startswith("Use when") for step in article.steps)

    def test_no_article_promises_anything(self, kb) -> None:
        # A coarse guard on transcription, not a safety claim: a bare
        # "we will refund"-style sentence in a step would be a transcription
        # error, since no supplied article contains one.
        for article in kb.all():
            for step in article.steps:
                lowered = step.lower()
                assert "we will refund" not in lowered
                assert "guarantee" not in lowered


class TestRetrievalSeam:
    def test_retrieve_returns_every_article_today(self, kb) -> None:
        assert len(kb.retrieve("any ticket text")) == len(kb.all())

    def test_retrieve_is_independent_of_ticket_text(self, kb) -> None:
        # Documents current behaviour so that swapping in real retrieval is a
        # visible, deliberate test change rather than a silent behaviour drift.
        assert kb.retrieve("billing") == kb.retrieve("authentication")


class TestCanonicalOrdering:
    def test_orders_by_file_position_not_argument_order(self, kb) -> None:
        assert kb.canonical_order(["KB-TRIAGE-01", "KB-AUTH-01"]) == [
            "KB-AUTH-01",
            "KB-TRIAGE-01",
        ]

    def test_drops_unknown_ids(self, kb) -> None:
        assert kb.canonical_order(["KB-NOPE", "KB-BILL-01"]) == ["KB-BILL-01"]

    def test_collapses_duplicates(self, kb) -> None:
        assert kb.canonical_order(["KB-BILL-01", "KB-BILL-01"]) == ["KB-BILL-01"]


class TestPromptRendering:
    def test_rendered_prompt_includes_every_id(self, kb) -> None:
        rendered = kb.render_for_prompt("")
        for article_id in EXPECTED_IDS:
            assert article_id in rendered

    def test_rendered_prompt_labels_prohibitions(self, kb) -> None:
        # The model needs the constraint to be visible, not buried in a bullet.
        assert "Prohibition: Do not promise a refund before investigation." in kb.render_for_prompt("")

    def test_allowed_ids_render_matches_the_allowlist(self, kb) -> None:
        # The repair prompt's legal-ID list and the validation allowlist come
        # from the same object, so they cannot drift.
        rendered = {part.strip() for part in kb.render_allowed_ids().split(",")}
        assert rendered == kb.allowed_ids


class TestLoaderValidation:
    def _write(self, tmp_path, payload: object):
        path = tmp_path / "kb.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(KnowledgeBaseError, match="not found"):
            KnowledgeBase.load(tmp_path / "absent.json")

    def test_invalid_json_raises(self, tmp_path) -> None:
        path = tmp_path / "kb.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(KnowledgeBaseError, match="not valid JSON"):
            KnowledgeBase.load(path)

    def test_missing_articles_key_raises(self, tmp_path) -> None:
        with pytest.raises(KnowledgeBaseError, match="articles"):
            KnowledgeBase.load(self._write(tmp_path, {"version": "1.0"}))

    def test_empty_articles_raises(self, tmp_path) -> None:
        with pytest.raises(KnowledgeBaseError, match="no articles"):
            KnowledgeBase.load(self._write(tmp_path, {"articles": []}))

    def test_duplicate_ids_raise(self, tmp_path) -> None:
        entry = {"id": "KB-X-01", "title": "X", "steps": ["Do it."], "prohibitions": []}
        with pytest.raises(KnowledgeBaseError, match="duplicate"):
            KnowledgeBase.load(self._write(tmp_path, {"articles": [entry, dict(entry)]}))

    def test_empty_steps_raise(self, tmp_path) -> None:
        entry = {"id": "KB-X-01", "title": "X", "steps": [], "prohibitions": []}
        with pytest.raises(KnowledgeBaseError, match="steps"):
            KnowledgeBase.load(self._write(tmp_path, {"articles": [entry]}))

    def test_non_string_step_raises(self, tmp_path) -> None:
        entry = {"id": "KB-X-01", "title": "X", "steps": [42], "prohibitions": []}
        with pytest.raises(KnowledgeBaseError, match="non-empty string"):
            KnowledgeBase.load(self._write(tmp_path, {"articles": [entry]}))

    def test_missing_title_raises(self, tmp_path) -> None:
        entry = {"id": "KB-X-01", "steps": ["Do it."], "prohibitions": []}
        with pytest.raises(KnowledgeBaseError, match="title"):
            KnowledgeBase.load(self._write(tmp_path, {"articles": [entry]}))

    def test_minimal_valid_article_loads(self, tmp_path) -> None:
        entry = {"id": "KB-X-01", "title": "X", "steps": ["Do it."], "prohibitions": []}
        loaded = KnowledgeBase.load(self._write(tmp_path, {"articles": [entry]}))
        assert loaded.allowed_ids == {"KB-X-01"}
