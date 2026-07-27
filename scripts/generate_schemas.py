"""Regenerate JSON Schema files from the Pydantic models.

The schemas in ``schemas/`` are build artifacts, not hand-written sources —
regenerating them is how they stay in step with ``models.py``. Run:

    python scripts/generate_schemas.py

Two schemas are emitted because the model's output contract and the service's
response contract are deliberately different objects (see README A0 and A8):
the model never produces ``ticket_id`` or the recommended-action text.

These files document the **Pydantic models**, not the bytes sent to the API. The
SDK applies its own transform before transmission — notably re-adding
``additionalProperties: false``, which ``model_json_schema()`` omits because
``ModelTriageOutput`` uses ``extra="ignore"``. Both are correct for their purpose:
the wire schema constrains generation, while ``extra="ignore"`` governs what
happens to an unexpected field that arrives by some other route (the baseline
path, or a provider that does not enforce the schema).

``tests/test_schemas.py`` asserts these files stay in step with the models. They
were stale once, describing a contract the code no longer implemented.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ticketpilot.models import ModelTriageOutput, TriageDecision  # noqa: E402

SCHEMAS = {
    "triage_decision.schema.json": TriageDecision,
    "model_triage_output.schema.json": ModelTriageOutput,
}


def main() -> int:
    out_dir = Path(__file__).resolve().parents[1] / "schemas"
    out_dir.mkdir(exist_ok=True)
    for filename, model in SCHEMAS.items():
        schema = model.model_json_schema()
        path = out_dir / filename
        # sort_keys keeps the file byte-stable across regenerations so a diff
        # only ever shows a real schema change.
        path.write_text(
            json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path.relative_to(out_dir.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
