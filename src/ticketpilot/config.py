"""Runtime configuration and filesystem-independent path resolution.

Every tunable is an environment variable with a documented default, so the
service is configurable without code edits (assignment section 1) and the test
suite can run with nothing set at all.

Paths are discovered by walking up from this module until a directory
containing ``data/kb.json`` is found, so nothing depends on the current working
directory or on a hard-coded absolute path (assignment section 9).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Model and generation defaults. Opus 5 rejects temperature/top_p/top_k with a
# 400, so there is no sampling-determinism lever here by design; run-to-run
# stability comes from schema constraints plus the deterministic post-layer.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "low"
# Headroom matters: max_tokens caps thinking *plus* response text, and thinking
# is on by default on Opus 5. A tight ceiling truncates the JSON body mid-object,
# which surfaces as an inexplicable parse failure rather than an obvious limit.
DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 60.0

# Initial value only. The final threshold is selected from evaluation data and
# recorded in evaluation_report.md, which is why this is configuration rather
# than a constant buried in the review rules (A4).
DEFAULT_CONFIDENCE_THRESHOLD = 0.75

_ROOT_MARKER = Path("data") / "kb.json"


def repo_root() -> Path:
    """Return the project root by searching upward for ``data/kb.json``.

    Falls back to the current working directory if no marker is found, which
    keeps the failure mode a clear "file not found" rather than a confusing
    path built from the wrong base.
    """
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if candidate.is_dir() and (candidate / _ROOT_MARKER).is_file():
            return candidate
    return Path.cwd()


def _load_dotenv_once() -> None:
    """Load ``.env`` from the project root, if present.

    Called at import time so that every entry point — CLI, run_eval, the live
    verification script — picks up a local ``.env`` without each having to
    remember to. Without this, ``python-dotenv`` sits in the dependency list doing
    nothing and a key placed in ``.env`` is silently ignored, surfacing as an
    opaque authentication error rather than an obvious missing-key one.

    ``override=False`` gives a real environment variable precedence over the file,
    which is the conventional order and keeps CI explicit.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a declared dependency
        return
    env_path = repo_root() / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


_load_dotenv_once()


def api_key_status() -> str:
    """Describe the credential situation for a human-readable error message.

    Distinguishes "no key at all" from "a key that is present but empty", because
    an empty ``ANTHROPIC_API_KEY`` still occupies its precedence slot and will be
    sent as an empty credential — producing an authentication failure that looks
    like a bad key rather than an unfilled template.
    """
    raw = os.environ.get("ANTHROPIC_API_KEY")
    if raw is None:
        return "missing"
    if not raw.strip():
        return "empty"
    return "present"


def data_dir() -> Path:
    """Directory holding the KB, supplied tickets, and evaluation cases."""
    override = os.environ.get("TICKETPILOT_DATA_DIR")
    return Path(override) if override else repo_root() / "data"


def runs_dir() -> Path:
    """Directory for append-only run records and generated reports."""
    override = os.environ.get("TICKETPILOT_RUNS_DIR")
    return Path(override) if override else repo_root() / "runs"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        # A malformed override falls back to the default rather than crashing
        # the service on startup; the effective value is recorded per run.
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Effective configuration for one run.

    Snapshotted into every run record so a result can always be traced back to
    the settings that produced it.
    """

    provider: str
    model: str
    effort: str
    max_tokens: int
    timeout_seconds: float
    confidence_threshold: float

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            provider=os.environ.get("TICKETPILOT_PROVIDER", "anthropic").strip() or "anthropic",
            model=os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL,
            effort=os.environ.get("TICKETPILOT_EFFORT", "").strip() or DEFAULT_EFFORT,
            max_tokens=_env_int("TICKETPILOT_MAX_TOKENS", DEFAULT_MAX_TOKENS),
            timeout_seconds=_env_float("TICKETPILOT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            confidence_threshold=_env_float(
                "TICKETPILOT_CONFIDENCE_THRESHOLD", DEFAULT_CONFIDENCE_THRESHOLD
            ),
        )
