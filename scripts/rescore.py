#!/usr/bin/env python
"""Re-score a saved evaluation run against the current expectations.

Scoring is deterministic and ``results.jsonl`` holds every decision verbatim, so
correcting an expected label does not require paying for the model again. That
matters for honesty as much as for cost: without this, the only way to reflect a
fixed expectation is a fresh run, and the temptation is to leave a known-wrong
label in place because re-running is expensive.

    python scripts/rescore.py artifacts/live-evaluation/baseline/results.jsonl
    python scripts/rescore.py artifacts/live-evaluation/*/results.jsonl --write

``--write`` updates each run's ``metrics.json`` in place, adding a
``rescored_from`` note so a reader can tell that the numbers were recomputed from
stored decisions rather than produced by a fresh set of API calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ticketpilot.evaluation import (  # noqa: E402
    CaseScore,
    aggregate,
    load_cases,
    render_comparison,
    score_raw_decision,
    stability_report,
)
from ticketpilot.kb import KnowledgeBase  # noqa: E402


def rescore(path: Path, kb: KnowledgeBase) -> dict[str, Any]:
    """Re-score one results.jsonl against the current expectations."""
    # Keyed on ticket_id rather than on an embedded score: records deliberately do
    # not carry one, because a stored score goes stale as soon as a label changes.
    # case_id equals ticket_id for every case in this suite (asserted in
    # tests/test_eval_data.py).
    cases = {c.ticket.ticket_id: c for c in load_cases("all")}
    records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    first_pass: dict[str, CaseScore] = {}
    all_scores: list[CaseScore] = []
    fingerprints: dict[str, list[str]] = defaultdict(list)
    mode = records[0]["mode"] if records else "unknown"
    changed: list[str] = []
    uncomparable: set[str] = set()

    for record in records:
        # Tolerates legacy records that still embed a score, for artifacts
        # written before it was removed.
        old = record["diagnostics"].get("score") or {}
        case = cases.get(record.get("ticket_id"))
        if case is None:
            continue
        case_id = case.case_id

        score = score_raw_decision(
            case,
            record.get("decision"),
            kb,
            mode=record["mode"],
            provider_failure=record["diagnostics"].get("provider_failure"),
        )

        # Report any case whose verdict moved, so a reader can see exactly what the
        # expectation change did rather than only its effect on the totals.
        #
        # Only fields actually present in a stored score are comparable. Records
        # written after the embedded score was removed have none, and treating a
        # missing value as a previous verdict reported every field of every case as
        # "None -> True" — a hundred entries implying a hundred changes. A comparison
        # that has no baseline must report nothing, not everything.
        for field in ("category_correct", "priority_correct", "review_correct",
                      "expected_flags_present"):
            if field in old and old[field] != getattr(score, field):
                changed.append(
                    f"{case_id}.{field}: {old[field]} -> {getattr(score, field)}"
                )
        if not old:
            uncomparable.add(case_id)

        if record.get("run_index", 0) == 0:
            first_pass[case_id] = score
            all_scores.append(score)
        if record.get("run_index", 0) > 0 or case.ticket.ticket_id in {
            r["ticket_id"] for r in records if r.get("run_index", 0) > 0
        }:
            fingerprints[case.ticket.ticket_id].append(score.decision_fingerprint)

    supplied = [s for s in all_scores if s.source == "supplied"]
    authored = [s for s in all_scores if s.source == "authored"]

    result: dict[str, Any] = {
        "mode": mode,
        "overall": aggregate(all_scores),
        "authored_cases": aggregate(authored) if authored else None,
        "supplied_tickets": aggregate(supplied) if supplied else None,
        "verdict_changes": changed,
    }
    if uncomparable:
        result["verdict_changes_note"] = (
            f"{len(uncomparable)} of {len(first_pass)} cases carry no stored score to "
            "compare against, so no change could be detected for them. Records "
            "deliberately do not embed a score; the metrics here are recomputed from "
            "the stored decisions under the current expectations."
        )
    if fingerprints:
        result["stability"] = stability_report(dict(fingerprints))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--write", action="store_true", help="Update metrics.json in place.")
    args = parser.parse_args(argv)

    kb = KnowledgeBase.load()
    by_mode: dict[str, dict[str, Any]] = {}

    for path in args.results:
        if not path.is_file():
            print(f"skipping missing {path}")
            continue
        scored = rescore(path, kb)
        by_mode[scored["mode"]] = scored
        print(f"\n== {path.parent.name} ({scored['mode']}) ==")
        for change in scored["verdict_changes"]:
            print(f"  changed: {change}")
        if not scored["verdict_changes"]:
            print("  no verdict changed")
        if scored.get("verdict_changes_note"):
            print(f"  note: {scored['verdict_changes_note']}")

        if args.write:
            metrics_path = path.parent / "metrics.json"
            existing = (
                json.loads(metrics_path.read_text(encoding="utf-8"))
                if metrics_path.is_file()
                else {}
            )
            existing.update(
                {k: v for k, v in scored.items() if k != "mode"},
                rescored_from=str(path.name),
                rescored_at=datetime.now(timezone.utc).isoformat(),
                rescored_note=(
                    "Metrics recomputed from stored decisions after an expected "
                    "label was corrected. No new API calls were made; the "
                    "decisions themselves are unchanged from the original run."
                ),
            )
            metrics_path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(f"  wrote {metrics_path}")

    print("\n" + render_comparison(
        by_mode.get("baseline", {}).get("overall"),
        by_mode.get("final", {}).get("overall"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
