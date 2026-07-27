"""Command-line interface.

Section 1 of the assignment accepts a CLI or a simple HTTP API; this is the CLI.
It prints the nine-field decision contract as JSON on stdout and nothing else, so
it composes with ``jq`` and can be diffed between runs. Diagnostics go to a run
record or, with ``--diagnostics``, to stderr — never mixed into the decision.

Examples
--------
    python -m ticketpilot.cli --ticket-id DEMO-001 \\
        --text "Please add scheduled PDF reports to the dashboard." --tier standard

    python -m ticketpilot.cli --file data/tickets/supplied.json

    python -m ticketpilot.cli --ticket-id X-1 --text "..." --offline --diagnostics
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .baseline import triage_baseline
from .config import Settings
from .kb import KnowledgeBase
from .models import TicketInput
from .pipeline import triage
from .providers import MockProvider, build_provider
from .storage import RunRecord, RunWriter


def _load_tickets(path: Path) -> list[TicketInput]:
    """Read one ticket or a batch from JSON.

    Accepts the assignment's single-object input contract, a bare list, or the
    ``{"tickets": [...]}`` wrapper used by data/tickets/supplied.json.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "tickets" in payload:
        entries = payload["tickets"]
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = [payload]
    return [
        TicketInput(
            ticket_id=entry["ticket_id"],
            text=entry.get("text", ""),
            customer_tier=entry.get("customer_tier"),
        )
        for entry in entries
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ticketpilot",
        description="Triage a support ticket into a validated structured decision.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="Ticket text.")
    source.add_argument("--file", type=Path, help="JSON file with one ticket or a batch.")

    parser.add_argument("--ticket-id", help="Required with --text.")
    parser.add_argument("--tier", dest="customer_tier", default=None)
    parser.add_argument(
        "--mode",
        choices=["final", "baseline"],
        default="final",
        help="Which pipeline to run. 'baseline' is the section 7 comparison arm.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use the scripted provider. No API key required; for smoke tests only.",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Write the diagnostics envelope to stderr. Never mixed into stdout.",
    )
    parser.add_argument(
        "--record",
        metavar="NAME",
        default=None,
        help="Also append a run record to runs/NAME/results.jsonl.",
    )
    args = parser.parse_args(argv)

    if args.text is not None and not args.ticket_id:
        parser.error("--ticket-id is required with --text")

    tickets = (
        _load_tickets(args.file)
        if args.file
        else [
            TicketInput(
                ticket_id=args.ticket_id,
                text=args.text,
                customer_tier=args.customer_tier,
            )
        ]
    )

    settings = Settings.from_env()
    kb = KnowledgeBase.load()
    writer = RunWriter(args.record, settings) if args.record else None

    decisions: list[dict] = []
    for ticket in tickets:
        # A fresh provider per ticket keeps call accounting per-decision, which is
        # what makes the "no repair on an unrepairable failure" check meaningful.
        provider = MockProvider() if args.offline else build_provider(settings)

        if args.mode == "final":
            outcome = triage(ticket, kb, provider, settings)
            decision = outcome.decision.model_dump(mode="json")
            diagnostics = outcome.diagnostics
        else:
            result = triage_baseline(ticket, kb, provider)
            # The baseline emits whatever the model produced, unrepaired — that is
            # the point of the comparison arm.
            decision = result.decision
            diagnostics = {
                "parse_ok": result.parse_ok,
                "parse_error": result.parse_error,
                "provider_failure": result.provider_failure,
                "raw_text": result.raw_text,
            }

        decisions.append(decision)
        if writer:
            writer.append(
                RunRecord(
                    ticket_id=ticket.ticket_id,
                    mode=args.mode,
                    decision=decision,
                    diagnostics=diagnostics,
                )
            )
        if args.diagnostics:
            print(
                json.dumps(
                    {"ticket_id": ticket.ticket_id, "diagnostics": diagnostics},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                file=sys.stderr,
            )

    payload = decisions[0] if len(decisions) == 1 else decisions
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if writer:
        print(f"run record: {writer.path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
