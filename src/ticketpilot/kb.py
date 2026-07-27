"""Knowledge base: the single source of truth for KB content.

One loader feeds four consumers, which is the point of keeping the KB out of
the prompt source:

* the rendered KB section of the system prompt
* the ID allowlist used to filter model-proposed ``kb_ids``
* the ``steps``/``prohibitions`` used to assemble recommended-action text
* the list of legal IDs restated in the repair prompt

Because all four derive from the same parsed objects, the prompt and the
validation logic cannot drift apart when an article is added or edited.

Growth path (see README): ``KnowledgeBase.retrieve`` is the seam where
retrieval would go. Today it returns every article, which is strictly better
than retrieval at this size — 7 articles cost a few hundred tokens and inlining
them has no recall risk. When the KB outgrows the cached prefix, or precision
degrades, replace that one method with keyword/BM25 matching; reach for
embeddings only if lexical matching demonstrably fails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import data_dir


class KnowledgeBaseError(ValueError):
    """Raised when kb.json is structurally invalid.

    This is a startup/configuration failure, not a runtime triage failure: a
    malformed KB means the service cannot ground anything, so failing loudly at
    load time is correct. Runtime model failures are handled quite differently
    (see pipeline.py).
    """


@dataclass(frozen=True)
class Article:
    """One knowledge-base article."""

    id: str
    title: str
    steps: tuple[str, ...]
    prohibitions: tuple[str, ...]
    when_to_use: str | None = None

    def render_for_prompt(self) -> str:
        """Render this article for the system prompt.

        Includes ``when_to_use`` so the model can judge applicability, and
        labels prohibitions explicitly so the constraint is visible rather than
        buried in a bullet list.
        """
        lines = [f"{self.id} - {self.title}"]
        if self.when_to_use:
            lines.append(f"  Applicability: {self.when_to_use}")
        for step in self.steps:
            lines.append(f"  Step: {step}")
        for prohibition in self.prohibitions:
            lines.append(f"  Prohibition: {prohibition}")
        return "\n".join(lines)


class KnowledgeBase:
    """Loaded, validated knowledge base."""

    def __init__(self, articles: list[Article]) -> None:
        self._articles = list(articles)
        # Preserves canonical (file) order, which is what makes multi-article
        # action text deterministic regardless of the order the model proposed.
        self._by_id = {article.id: article for article in self._articles}

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: Path | None = None) -> KnowledgeBase:
        """Load and validate kb.json.

        Validation is deliberately narrow and structural: required keys,
        correct types, non-empty steps, unique IDs. It makes no attempt to
        judge whether the *wording* of a step or prohibition is safe — a text
        scan cannot honestly claim to catch every bad edit (a promise can be
        phrased without any word a detector knows). Fidelity of the wording to
        the assignment is established by human review, and the action text is
        never model-authored, so a review-time mistake is the only way bad
        wording enters at all.
        """
        kb_path = path or (data_dir() / "kb.json")
        try:
            raw = json.loads(kb_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KnowledgeBaseError(f"knowledge base not found at {kb_path}") from exc
        except json.JSONDecodeError as exc:
            raise KnowledgeBaseError(f"{kb_path} is not valid JSON: {exc}") from exc

        if not isinstance(raw, dict) or not isinstance(raw.get("articles"), list):
            raise KnowledgeBaseError(f"{kb_path} must be an object with an 'articles' list")

        articles: list[Article] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw["articles"]):
            article = cls._parse_article(entry, index, kb_path)
            if article.id in seen:
                raise KnowledgeBaseError(f"{kb_path}: duplicate article id {article.id!r}")
            seen.add(article.id)
            articles.append(article)

        if not articles:
            raise KnowledgeBaseError(f"{kb_path} contains no articles")
        return cls(articles)

    @staticmethod
    def _parse_article(entry: object, index: int, kb_path: Path) -> Article:
        where = f"{kb_path}: articles[{index}]"
        if not isinstance(entry, dict):
            raise KnowledgeBaseError(f"{where} must be an object")

        def _require_str(key: str) -> str:
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise KnowledgeBaseError(f"{where}: {key!r} must be a non-empty string")
            return value

        def _require_str_list(key: str, *, allow_empty: bool) -> tuple[str, ...]:
            value = entry.get(key)
            if not isinstance(value, list):
                raise KnowledgeBaseError(f"{where}: {key!r} must be a list")
            if not allow_empty and not value:
                raise KnowledgeBaseError(f"{where}: {key!r} must not be empty")
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise KnowledgeBaseError(
                        f"{where}: every entry in {key!r} must be a non-empty string"
                    )
            return tuple(value)

        when_to_use = entry.get("when_to_use")
        if when_to_use is not None and not isinstance(when_to_use, str):
            raise KnowledgeBaseError(f"{where}: 'when_to_use' must be a string or null")

        return Article(
            id=_require_str("id"),
            title=_require_str("title"),
            # An article with no steps could not produce an action, so an empty
            # steps list is a KB authoring error. Prohibitions may legitimately
            # be empty (KB-TRIAGE-01 has none).
            steps=_require_str_list("steps", allow_empty=False),
            prohibitions=_require_str_list("prohibitions", allow_empty=True),
            when_to_use=when_to_use or None,
        )

    # ------------------------------------------------------------- accessors

    def all(self) -> list[Article]:
        """Every article, in canonical order."""
        return list(self._articles)

    def retrieve(self, ticket_text: str) -> list[Article]:  # noqa: ARG002
        """Articles to place in the prompt for this ticket.

        The retrieval seam. Returns everything today: at 7 articles, inlining
        the whole KB has no recall risk and costs a few hundred cached tokens,
        which beats any retrieval scheme on both accuracy and simplicity. The
        ``ticket_text`` argument is unused on purpose — it is the parameter a
        future implementation needs, and having it here means swapping in
        retrieval touches this method only.
        """
        return self.all()

    @property
    def allowed_ids(self) -> frozenset[str]:
        """The ID allowlist. Anything outside this set is an invented ID."""
        return frozenset(self._by_id)

    def get(self, article_id: str) -> Article | None:
        return self._by_id.get(article_id)

    def canonical_order(self, article_ids: list[str]) -> list[str]:
        """Sort IDs into canonical KB order, dropping unknown and duplicate IDs.

        Canonical ordering is what makes assembled action text identical for
        the same set of articles regardless of the order the model listed them
        — a precondition for the stability metric.
        """
        wanted = set(article_ids)
        return [article.id for article in self._articles if article.id in wanted]

    # ----------------------------------------------------------- prompt views

    def render_for_prompt(self, ticket_text: str = "") -> str:
        """Render the KB section of the system prompt."""
        return "\n\n".join(
            article.render_for_prompt() for article in self.retrieve(ticket_text)
        )

    def render_allowed_ids(self) -> str:
        """Comma-separated legal IDs, for the repair prompt's list of values."""
        return ", ".join(article.id for article in self._articles)
