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
from ticketpilot.providers import MockProvider, Script, build_provider  # noqa: E402
from ticketpilot.storage import RunRecord, RunWriter  # noqa: E402

# Three representative tickets for the stability metric: an escalation with an
# injection payload, an abstention, and the bilingual case. Chosen to span the
# behaviours most likely to wobble between runs.
STABILITY_TICKETS = ("T-001", "T-004", "T-005")

# Opus 5 list pricing, USD per million tokens. Cache reads bill at roughly a tenth
# of the input rate; cache writes at roughly 1.25x. Used to report what a run
# actually cost rather than leaving an estimate in the report.
_INPUT_PER_MTOK = 5.00
_OUTPUT_PER_MTOK = 25.00
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10


def _collect_usage(diagnostics: dict[str, Any], into: dict[str, int]) -> int:
    """Accumulate token usage from a case's diagnostics. Returns calls made.

    Usage lives in different places per arm — the baseline records it flat, the
    final pipeline records it per provider call including any repair — so this
    walks both shapes rather than assuming one.
    """
    calls = 0
    sources: list[dict[str, Any]] = []
    if isinstance(diagnostics.get("usage"), dict):  # baseline
        sources.append({"usage": diagnostics["usage"]})
        calls = 1
    for key in ("provider", "repair_provider"):  # final
        entry = diagnostics.get(key)
        if isinstance(entry, dict):
            sources.append(entry)
    if "provider_calls" in diagnostics:
        calls = int(diagnostics["provider_calls"] or 0)

    for source in sources:
        usage = source.get("usage")
        if isinstance(usage, dict):
            for name, value in usage.items():
                if isinstance(value, int):
                    into[name] = into.get(name, 0) + value
    return calls


def _estimated_cost(usage: dict[str, int]) -> float:
    """USD for the accumulated usage, at list price."""
    return (
        usage.get("input_tokens", 0) / 1e6 * _INPUT_PER_MTOK
        + usage.get("cache_creation_input_tokens", 0) / 1e6 * _INPUT_PER_MTOK * _CACHE_WRITE_MULTIPLIER
        + usage.get("cache_read_input_tokens", 0) / 1e6 * _INPUT_PER_MTOK * _CACHE_READ_MULTIPLIER
        + usage.get("output_tokens", 0) / 1e6 * _OUTPUT_PER_MTOK
    )


# Defect scripts cycled through in --adversarial mode. Each is a specific way a
# model can produce output that looks plausible and is wrong.
ADVERSARIAL_SCRIPTS = (
    Script.INVENTED_KB_ID,
    Script.FABRICATED_REFUND_PROMISE,
    Script.PARAPHRASED_QUOTE,
    Script.INVENTED_CATEGORY,
    Script.ALL_KB_IDS_INVENTED,
    Script.TRANSLATED_QUOTE,
    Script.INVENTED_PRIORITY,
    Script.MALFORMED_JSON,
    Script.INVENTED_FLAG,
    Script.CANARY_LEAK,
    Script.TIMEOUT,
    Script.INCOMPLETE_JSON,
    Script.REFUSAL,
    Script.EMPTY_CONTENT,
)


def build_adversarial_map(cases: list[EvalCase]) -> dict[str, Script]:
    """Assign one defect per *distinct ticket text*, cycling for even coverage.

    Two properties are needed at once, and the obvious approaches each break one:

    * Indexing by case position gives even coverage but hands the tier-invariance
      pair two different defects, so its two halves diverge for a reason that has
      nothing to do with tier — a spurious invariance failure.
    * Hashing the text keeps that pair aligned but distributes unevenly, leaving
      some defects unassigned and their metric rows showing 0 -> 0, which
      demonstrates nothing.

    Keying on distinct text and cycling in first-appearance order satisfies both:
    identical text always gets an identical response, and every defect in the list
    is exercised as long as there are enough distinct tickets.
    """
    mapping: dict[str, Script] = {}
    for case in cases:
        if case.ticket.text not in mapping:
            mapping[case.ticket.text] = ADVERSARIAL_SCRIPTS[
                len(mapping) % len(ADVERSARIAL_SCRIPTS)
            ]
    return mapping


def _make_provider(
    offline: bool,
    settings: Settings,
    case: EvalCase | None = None,
    adversarial_map: dict[str, Script] | None = None,
):
    """Build the provider for one case.

    Passing ``case`` and ``adversarial_map`` selects a scripted defect. This is
    what makes the containment claims measurable without spending anything on the
    API: both arms receive identical hostile responses, and the metrics show how
    many reach the output.
    """
    if offline:
        if case is None or adversarial_map is None:
            return MockProvider()
        script = adversarial_map[case.ticket.text]
        # Repeated so a repair attempt meets the same defect: a defect that
        # vanishes on retry would overstate the final version's containment.
        return MockProvider([script, script])
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
    }
    # The score is deliberately NOT stored in the record. It is a derivative of the
    # decision plus the current expectations, and an embedded copy goes stale the
    # moment a label is corrected — which produced artifacts where metrics.json said
    # a verdict was wrong while the record beside it still said "correct".
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
    # See the note in _run_baseline_case: the score stays out of the record.
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
    adversarial: bool = False,
) -> dict[str, Any]:
    """Execute one mode over all cases and write artifacts."""
    writer = RunWriter(out_name, settings)
    first_pass: dict[str, CaseScore] = {}
    all_scores: list[CaseScore] = []
    fingerprints: dict[str, list[str]] = defaultdict(list)

    adversarial_map = build_adversarial_map(cases) if adversarial else None
    usage_total: dict[str, int] = {}
    calls_total = 0

    for case in cases:
        repeats = runs if (runs > 1 and case.ticket.ticket_id in STABILITY_TICKETS) else 1
        for index in range(repeats):
            provider = _make_provider(
                offline, settings, case if adversarial else None, adversarial_map
            )
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

            calls_total += _collect_usage(diagnostics, usage_total)

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
        # Recorded so the report can state what a run actually cost rather than
        # extrapolating from a single call.
        "cost": {
            "provider_calls": calls_total,
            "usage": dict(usage_total),
            "estimated_usd_at_list_price": round(_estimated_cost(usage_total), 4),
        },
    }
    if fingerprints:
        metrics["stability"] = stability_report(dict(fingerprints))
    if offline and adversarial:
        metrics["interpretation"] = (
            "Adversarial offline run. Each case receives a different scripted "
            "defect (invented KB id, fabricated refund promise, paraphrased or "
            "translated quote, invented enum value, malformed JSON, canary leak, "
            "timeout, refusal), repeated so a repair attempt meets the same "
            "defect. Both arms see identical hostile responses. Category and "
            "priority accuracy are meaningless here — the scripted decisions bear "
            "no relation to the tickets. The containment rows are the point: "
            "unknown KB IDs, ungrounded commitments, ungrounded evidence quotes, "
            "ticket-id mismatches, schema validity, and crashes. Those are "
            "structural properties of the code, so they hold independently of "
            "model behaviour and are reproducible at zero API cost."
        )
        metrics["adversarial_scripts"] = [s.value for s in ADVERSARIAL_SCRIPTS]
    elif offline:
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
    parser.add_argument(
        "--adversarial",
        action="store_true",
        help=(
            "With --offline: give each case a different scripted defect. Measures "
            "containment of hostile model output at zero API cost."
        ),
    )
    args = parser.parse_args(argv)
    if args.adversarial and not args.offline:
        parser.error("--adversarial requires --offline (it drives the scripted provider)")

    settings = Settings.from_env()
    kb = KnowledgeBase.load()
    cases = load_cases(args.cases)
    print(f"{len(cases)} case(s) | model={settings.model} effort={settings.effort}")

    modes = ["baseline", "final"] if args.mode == "both" else [args.mode]
    results: dict[str, dict[str, Any]] = {}
    for mode in modes:
        suffix = "-adversarial" if args.adversarial else ("-offline" if args.offline else "")
        # Each mode gets its own directory even when --out is given, otherwise
        # running both would have the second mode overwrite the first's metrics.
        name = f"{args.out}/{mode}" if args.out else f"{mode}{suffix}"
        print(f"\n== {mode} ==")
        results[mode] = run_mode(
            mode, cases, kb, settings,
            offline=args.offline, runs=args.runs, out_name=name,
            adversarial=args.adversarial,
        )

    print("\n" + render_comparison(
        results.get("baseline", {}).get("overall"),
        results.get("final", {}).get("overall"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
