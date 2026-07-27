"""Append-only run records.

The emitted decision contains exactly the nine contract fields (README A2), so
everything needed to explain *why* a decision came out that way lives here
instead: validation errors, repair attempts, detector hits, provider usage, and
the effective settings. Splitting them keeps the response contract clean without
giving up observability.

JSONL rather than a database: one line per decision, appendable, greppable, and
diffable in review. Nothing about this workload needs more, and section 6 rewards
proportionality.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings, runs_dir


def _jsonable(value: Any) -> Any:
    """Best-effort conversion for record values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    return value


@dataclass
class RunRecord:
    """One triage attempt, decision plus diagnostics."""

    ticket_id: str
    mode: str
    #: The emitted TriageDecision, or the raw baseline dict.
    decision: Any = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""
    run_index: int = 0

    def to_json(self) -> str:
        payload = {
            "timestamp": self.timestamp or datetime.now(timezone.utc).isoformat(),
            "ticket_id": self.ticket_id,
            "mode": self.mode,
            "run_index": self.run_index,
            "decision": _jsonable(self.decision),
            "diagnostics": _jsonable(self.diagnostics),
            "settings": _jsonable(self.settings),
        }
        # ensure_ascii=False keeps Hebrew readable in the artifact rather than
        # storing escape sequences a reviewer cannot skim.
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class RunWriter:
    """Writes run records to ``runs/<name>/results.jsonl``."""

    def __init__(self, name: str, settings: Settings | None = None) -> None:
        self.directory = runs_dir() / name
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "results.jsonl"
        self._settings = (
            {
                "provider": settings.provider,
                "model": settings.model,
                "effort": settings.effort,
                "max_tokens": settings.max_tokens,
                "confidence_threshold": settings.confidence_threshold,
            }
            if settings
            else {}
        )

    def append(self, record: RunRecord) -> None:
        record.settings = record.settings or dict(self._settings)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")

    def write_json(self, filename: str, payload: Any) -> Path:
        """Write a sidecar artifact (metrics, report) next to the records."""
        path = self.directory / filename
        path.write_text(
            json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return path

    def write_text(self, filename: str, text: str) -> Path:
        path = self.directory / filename
        path.write_text(text, encoding="utf-8")
        return path


def read_records(path: Path) -> list[dict[str, Any]]:
    """Read a results.jsonl back, skipping blank lines."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
