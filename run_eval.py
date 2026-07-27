#!/usr/bin/env python
"""Evaluation harness: run the baseline and/or the final pipeline and score both.

Examples
--------
Baseline against the real API, all cases::

    python run_eval.py --mode baseline --cases all

Final pipeline with three runs per ticket for the stability metric::

    python run_eval.py --mode final --cases all --runs 3

Smoke-test the harness with no API key (scripted provider)::

    python run_eval.py --mode baseline --cases authored --offline

Offline runs exercise the plumbing and the malformed-output paths. They are not
accuracy measurements: the scripted provider returns the same canned decision for
every ticket, so category and priority figures from an offline run are meaningless
by construction and are labelled as such in the output.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ticketpilot.baseline import triage_baseline  # noqa: E402
from ticketpilot.config import Settings  # noqa: E402
from ticketpilot.evaluation import (  # noqa: E402
    CaseScore,
    EvalCase,
    aggregate,
    load_cases,
    render_comparison,
    score_raw_decision,
    stability_report,
)
from ticketpilot.kb import KnowledgeBase  # noqa: E402
from ticketpilot.providers import MockProvider, build_provider  # noqa: E402
from ticketpilot.storage import RunRecord, RunWriter  # noqa: E402

# Three representative tickets for the stability metric: an escalation with an
# injection payload, an abstention, and the bilingual case. Chosen to span the
# behaviours most likely to wobble between runs.
STABILITY_TICKETS = ("T-001", "T-004", "T-005")


def _make_provider(offline: bool, settings: Settings):
    if offline:
        return MockProvider()
    return build_provider(settings)


def _run_baseline_case(
    case: EvalCase, kb: KnowledgeBase, provider: Any
) -> tuple[CaseScore, dict[str, Any], Any]:
    result = triage_baseline(case.ticket, kb, provider)
    score = score_raw_decision(
        case,
        result.decision,
        kb,
        mode="baseline",
        provider_failure=result.provider_failure,
    )
    diagnostics = {
        "parse_ok": result.parse_ok,
        "parse_error": result.parse_error,
        "provider_failure": result.provider_failure,
        "provider_detail": result.provider_detail,
        "usage": result.usage,
        "model": result.model,
        "raw_text": result.raw_text,
        "score": score,
    }
    return score, diagnostics, result.decision


def _run_final_case(
    case: EvalCase, kb: KnowledgeBase, provider: Any, settings: Settings
) -> tuple[CaseScore, dict[str, Any], Any]:
    # Imported lazily so `--mode baseline` works before the final pipeline exists
    # and never pays for importing it.
    from ticketpilot.pipeline import triage

    outcome = triage(case.ticket, kb, provider, settings)
    decision_dict = outcome.decision.model_dump(mode="json")
    score = score_raw_decision(
        case,
        decision_dict,
        kb,
        mode="final",
        provider_failure=outcome.diagnostics.get("provider_failure"),
    )
    diagnostics = dict(outcome.diagnostics)
    diagnostics["score"] = score
    return score, diagnostics, decision_dict


def _tier_invariance(scores_by_case: dict[str, CaseScore], cases: list[EvalCase]) -> dict[str, Any]:
    """Compare the decision fields of each declared tier-invariance pair."""
    checked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for case in cases:
        partner_id = case.tier_invariance_pair
        if not partner_id or case.case_id in seen:
            continue
        seen.update({case.case_id, partner_id})
        a = scores_by_case.get(case.case_id)
        b = scores_by_case.get(partner_id)
        if not a or not b:
            continue
        checked.append(
            {
                "pair": [case.case_id, partner_id],
                "identical_decision_fields": a.decision_fingerprint == b.decision_fingerprint,
                "fingerprint_a": a.decision_fingerprint,
                "fingerprint_b": b.decision_fingerprint,
            }
        )
    return {
        "pairs_checked": len(checked),
        "pairs_invariant": sum(1 for c in checked if c["identical_decision_fields"]),
        "detail": checked,
    }


def run_mode(
    mode: str,
    cases: list[EvalCase],
    kb: KnowledgeBase,
    settings: Settings,
    *,
    offline: bool,
    runs: int,
    out_name: str,
) -> dict[str, Any]:
    """Execute one mode over all cases and write artifacts."""
    writer = RunWriter(out_name, settings)
    first_pass: dict[str, CaseScore] = {}
    all_scores: list[CaseScore] = []
    fingerprints: dict[str, list[str]] = defaultdict(list)

    runner = _run_baseline_case if mode == "baseline" else _run_final_case

    for case in cases:
        repeats = runs if (runs > 1 and case.ticket.ticket_id in STABILITY_TICKETS) else 1
        for index in range(repeats):
            provider = _make_provider(offline, settings)
            try:
                if mode == "baseline":
                    score, diagnostics, decision = _run_baseline_case(case, kb, provider)
                else:
                    score, diagnostics, decision = _run_final_case(case, kb, provider, settings)
            except Exception as exc:  # pragma: no cover - harness safety net
                # A crash is itself a measurement (section 6 failure safety), so
                # it is recorded rather than aborting the whole run.
                score = CaseScore(
                    case_id=case.case_id, source=case.source, mode=mode, crashed=True
                )
                diagnostics = {"crash": f"{type(exc).__name__}: {exc}"}
                decision = None
                print(f"  !! {case.case_id} crashed: {type(exc).__name__}: {exc}")

            writer.append(
                RunRecord(
                    ticket_id=case.ticket.ticket_id,
                    mode=mode,
                    run_index=index,
                    decision=decision,
                    diagnostics=diagnostics,
                )
            )

            if index == 0:
                first_pass[case.case_id] = score
                all_scores.append(score)
            if repeats > 1:
                fingerprints[case.ticket.ticket_id].append(score.decision_fingerprint)

            marker = "ok " if score.schema_valid else "BAD"
            suffix = f" (run {index + 1}/{repeats})" if repeats > 1 else ""
            print(f"  [{marker}] {case.case_id}{suffix}")

    supplied = [s for s in all_scores if s.source == "supplied"]
    authored = [s for s in all_scores if s.source == "authored"]

    metrics: dict[str, Any] = {
        "mode": mode,
        "offline": offline,
        "settings": {
            "provider": "mock" if offline else settings.provider,
            "model": settings.model,
            "effort": settings.effort,
            "confidence_threshold": settings.confidence_threshold,
        },
        "overall": aggregate(all_scores),
        "authored_cases": aggregate(authored) if authored else None,
        "supplied_tickets": aggregate(supplied) if supplied else None,
        "tier_invariance": _tier_invariance(first_pass, cases),
    }
    if fingerprints:
        metrics["stability"] = stability_report(dict(fingerprints))
    if offline:
        metrics["warning"] = (
            "Offline run: the scripted provider returns the same canned decision "
            "for every ticket. Accuracy figures here are meaningless by "
            "construction; this mode exercises plumbing and failure paths only."
        )

    writer.write_json("metrics.json", metrics)
    print(f"\n  artifacts -> {writer.directory}")
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["baseline", "final", "both"], default="final")
    parser.add_argument("--cases", choices=["supplied", "authored", "all"], default="all")
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Repeats per stability ticket, for the run-to-run agreement metric.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the scripted provider. No API key needed; not an accuracy measurement.",
    )
    parser.add_argument("--out", default=None, help="Artifact directory name under runs/.")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    kb = KnowledgeBase.load()
    cases = load_cases(args.cases)
    print(f"{len(cases)} case(s) | model={settings.model} effort={settings.effort}")

    modes = ["baseline", "final"] if args.mode == "both" else [args.mode]
    results: dict[str, dict[str, Any]] = {}
    for mode in modes:
        suffix = "-offline" if args.offline else ""
        name = args.out or f"{mode}{suffix}"
        print(f"\n== {mode} ==")
        results[mode] = run_mode(
            mode, cases, kb, settings,
            offline=args.offline, runs=args.runs, out_name=name,
        )

    print("\n" + render_comparison(
        results.get("baseline", {}).get("overall"),
        results.get("final", {}).get("overall"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
