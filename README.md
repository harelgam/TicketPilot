# TicketPilot

An AI-assisted support-ticket triage service. Given a ticket, it returns a
validated structured decision: category, priority, summary, evidence quoted from
the ticket, a KB-grounded recommended action, a confidence score, safety flags,
and whether a human must review it.

The design principle throughout is **the model proposes, the application decides.**
Every closed vocabulary, the ticket ID, and the recommended-action text are owned
and enforced by code. A model failure or an injected instruction degrades the
result into a reviewable abstention rather than an authoritative-looking
fabrication.

> **Evaluation status: complete, with a mixed result.** Full baseline-to-final
> comparison against the real API — 20 cases × 2 arms plus 3×3 stability, 51 calls,
> $1.09. The final version is better on priority accuracy (80% → 95%), required-flag
> recall (67% → 93%), self-consistency (5 → 0 self-contradictory decisions) and
> stability (67% → 100%); and **worse** on review accuracy (100% → 95%), one
> forbidden-priority violation the baseline got right (A-006), and one ruled-out KB
> article selected (A-012).
>
> A manual semantic review found problems no automated metric caught: assembled action
> text is always KB-grounded but context-insensitive in 6 of 20 cases, one flag is a
> false positive, and one summary states a detail the ticket does not support. See
> [`evaluation_report.md`](evaluation_report.md).

---

## Setup

Requires Python 3.11+ (developed on 3.13.2).

```bash
python -m venv .venv

# Windows
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pip install -e .

# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e .
```

`requirements.txt` is fully pinned (direct + transitive) and doubles as the lock
file, so recreating the environment reproduces the exact versions used.

For real API calls, copy the env template and fill in your key:

```bash
cp .env.example .env      # then edit .env and set ANTHROPIC_API_KEY
```

`.env` is git-ignored. **No API key is required for the test suite or for any
`--offline` command** — that is deliberate, and it is what makes the
clean-environment requirement verifiable.

Verified from a clean checkout (`git archive` of HEAD into an empty directory, fresh
venv, install from `requirements.txt` only): 351 tests pass with no API key, and pass
from a different working directory; the CLI, the `triage` entry point, batch mode, and
the schema generator all work.

> **Windows note.** Installing into a deeply nested directory can fail with
> `OSError: [Errno 2] No such file or directory` on one of the Anthropic SDK's
> long filenames — that is the 260-character `MAX_PATH` limit, not a packaging fault.
> Use a shorter path (e.g. `C:\projects\TicketPilot`) or enable long-path support.

---

## Running it

### Triage one ticket

```bash
python -m ticketpilot.cli \
  --ticket-id DEMO-001 \
  --text "Please add scheduled PDF reports to the administration dashboard." \
  --tier standard
```

Prints the nine-field contract as JSON on stdout and nothing else, so it composes
with `jq`. Diagnostics go to stderr with `--diagnostics`, never mixed into the
decision.

### Triage the supplied tickets

```bash
python -m ticketpilot.cli --file data/tickets/supplied.json
```

### Verify the real integration (one API call)

```bash
python scripts/verify_live.py
```

Makes exactly one call, enforced by a wrapper that refuses to exceed the budget
rather than trusting the pipeline to stay within it. Prints the decision, the
invariant checks, token usage, and an upper-bound cost estimate.

### Run the evaluation

```bash
# Containment comparison, no API key, no cost:
python run_eval.py --mode both --cases all --offline --adversarial

# Full accuracy comparison against the real API. The run reported in
# evaluation_report.md made 51 calls and cost $1.09 at list price; a re-run should
# be cheaper now that the prompt-cache bug is fixed.
python run_eval.py --mode both --cases all --runs 3
```

### Re-score a saved run without spending anything

Scoring is deterministic and `results.jsonl` holds every decision verbatim, so a
corrected expected label does not need a fresh run:

```bash
python scripts/rescore.py artifacts/live-evaluation/*/results.jsonl --write
```

### Run the tests

```bash
python -m pytest        # 351 tests, all offline
```

### Regenerate the JSON schemas

`schemas/` is a generated artifact. After changing anything in `models.py`:

```bash
python scripts/generate_schemas.py
```

`tests/test_schemas.py` fails if the committed files drift from the models, so this
cannot be forgotten silently.

---

## Architecture

```
ticket ──> pre-checks         empty text short-circuits; injection regex scan
             │
             v
        prompt builder        system[0] = policy + KB   (trusted, cached)
                              system[1] = canary        (trusted, per-request)
                              user      = <untrusted_ticket>…  (untrusted)
             │
             v
        LLMProvider           AnthropicProvider | MockProvider   (configurable)
             │                schema-constrained ModelTriageOutput
             v
        parse + validate ───> ONE repair call, all errors, only if repairable
             │                              │ fails
             v                              v
        deterministic post-layer        safe fallback
             │                              │
             v <────────────────────────────┘
        TriageDecision  +  run record (diagnostics)
```

| Module | Responsibility |
| --- | --- |
| `models.py` | The three closed vocabularies and both output contracts, declared once |
| `kb.py` | KB loading, structural validation, ID allowlist, prompt rendering, retrieval seam |
| `actions.py` | Assembles action text from KB `steps`/`prohibitions`; `SAFE_GENERIC_ACTION` |
| `prompts.py` | Trusted system prompt, delimited untrusted ticket, repair prompt, canary |
| `injection.py` | Layer-2 detector (English + Hebrew) |
| `validation.py` | Exact-substring evidence, allowlists, clamping, canary check |
| `review.py` | Escalate-only review policy and flag provenance |
| `pipeline.py` | The final pipeline and the Layer-3 invariants |
| `baseline.py` | The §7 comparison arm: one call, basic prompt, no validation |
| `evaluation.py` | Scoring instruments and metrics |
| `providers/` | The configurable client boundary; real and scripted implementations |
| `storage.py` | JSONL run records |
| `cli.py` | Command-line entry point |

### Two output contracts, deliberately different

`ModelTriageOutput` is what the model is asked for. `TriageDecision` is what the
service returns. The difference is the design:

| Field | In the model's schema? |
| --- | --- |
| `category`, `priority`, `flags` | Yes — as closed enums |
| `summary`, `evidence`, `kb_ids`, `confidence`, `needs_human_review` | Yes |
| `ticket_id` | **No** — copied from the input |
| `recommended_action.text` | **No** — assembled from the KB |

A field the model never sees cannot be hallucinated and cannot be redirected by an
instruction injected into a ticket.

### Prompt-injection defence, in three layers

1. **Instruction separation.** Policy and KB live only in the system prompt; the
   ticket appears only in the user turn, inside `<untrusted_ticket>` tags, labelled
   as data. The tags are not a security mechanism — they mark the trust boundary.
2. **Deterministic detector.** Known phrasings in English and Hebrew. It does not
   judge the ticket harmful, modify it, or block the call — it adds
   `PROMPT_INJECTION` and forces review. Flags union with the model's, so either
   layer detecting is enough. **Not comprehensive and not a security boundary**; a
   test asserts an unlisted paraphrase slips through.
3. **Invariants that do not depend on detection.** Even when both layers miss, the
   model cannot change the `ticket_id`, emit an out-of-vocabulary value, invent a KB
   ID, fabricate an evidence quote, author the action text, cancel a policy-required
   review, or make the application echo the system prompt (a canary fails the
   response closed if it appears in output).

Layer 3 bounds the damage.

### The KB as data, and the growth path

`data/kb.json` is the single source of truth. One loader feeds the prompt text,
the ID allowlist, and the action strings, so they cannot drift apart when an
article changes. Each article is stored as `steps[]` + `prohibitions[]`, which is
the literal shape of the bullets in the assignment, so the data is verifiably
faithful to the source rather than a paraphrase.

`KnowledgeBase.retrieve()` is the retrieval seam. It returns every article today,
which at 7 articles (~600 cached tokens) beats any retrieval scheme on both
accuracy and simplicity — there is no recall risk. Migration triggers, in order:
inline until the KB no longer fits comfortably in the cached prefix or precision
degrades → keyword/BM25 → embeddings only if lexical matching demonstrably fails.
No vector database is used and none is warranted at this size.

---

## Human-review policy

`needs_human_review` is `true` when **any** of:

- `priority` is P0 or P1
- `category` is `SECURITY`
- `category` or `priority` is `UNKNOWN`
- `confidence` is below the threshold (config, default 0.75)
- any flag is present: `PROMPT_INJECTION`, `MISSING_INFO`, `NO_KB_SUPPORT`, `CONFLICTING_SIGNALS`
- schema validation failed, even if a repair later succeeded
- any evidence quote was dropped, or no evidence survived
- `kb_ids` was empty after filtering
- the provider call failed, timed out, returned empty content, or was refused
- the safe fallback was emitted

**Escalate-only.** Code may turn `false` into `true`; it never turns `true` into
`false`. A model-produced `false` cannot override a policy rule — tested against a
P0 `SECURITY` response that reports `needs_human_review: false`.

### Flag provenance

Every flag asserts something specific, so the degraded paths carry different flags
even though they share one output shape:

| Path | `flags` | Why |
| --- | --- | --- |
| Empty ticket | `["MISSING_INFO"]` | The information genuinely is absent **from the ticket** |
| KB IDs parsed, none valid | `["NO_KB_SUPPORT"]` | The KB **was** consulted and nothing applied |
| Schema/evidence validation failed | `[]` | A **model-output** failure — says nothing about the ticket |
| Timeout / refusal / empty body | `[]` | An **infrastructure** failure — and no evidence about the KB, since the lookup never ran |

The two empty rows are deliberate. The flag vocabulary is closed and has no member
for "the model or the network failed", so staying silent is the only truthful
option. Borrowing `MISSING_INFO` for a schema failure, or `NO_KB_SUPPORT` for a
timeout, would put a false statement in the response.

### How to read `confidence`

A self-assessed score from 0.0 to 1.0 for how certain the model is that the
category + priority pair is correct given only the ticket text. The system prompt
asks for it in exactly those terms. It is **not** a calibrated probability and
**not** a measure of grounding quality — a decision can be confident and ungrounded,
which is why the evidence and KB checks are independent of it. Used as one review
trigger among several.

In the live run no case was reviewed solely on confidence, so at 0.75 the threshold
is currently inert. See the evaluation report.

---

## Assumptions and design decisions

| # | Assumption |
| --- | --- |
| A0 | `ticket_id` is application-owned, excluded from the model schema, copied from input. Asserted on every path. |
| A1 | `customer_tier` is context only and does not change category or priority — the supplied policy defines no tier escalation. Enforced by a tier-invariance pair. |
| A2 | The decision carries exactly the nine contract fields; diagnostics live in a separate run record. |
| A3 | Evidence matching is exact substring against raw text, **no normalisation**. A diagnostic records whether a rejected quote would have matched normalised. |
| A4 | The confidence threshold is configuration, not a constant, so it can be chosen from data. |
| A5 | Whitespace-only text short-circuits before any model call. |
| A6 | Both arms use the same model, so the comparison isolates engineering. |
| A7 | Author-judged labels for the supplied tickets live in a data file marked as judgment, never in `src/`. No supplied ticket ID appears anywhere under `src/` (there is a test). |
| A8 | `recommended_action.text` is excluded from the model schema and assembled from KB content. |

**Why no agent framework.** The KB is 7 articles that fit in a cached system prompt,
so a tool-calling loop adds latency, cost, and run-to-run nondeterminism for no gain
— and nondeterminism degrades the stability metric the assignment grades.
Classification and priority are a fixed policy, better expressed as prompt policy
plus a deterministic rule layer than as tools the model may or may not call.

**Why the action text is assembled rather than validated.** A valid KB ID paired with
contradicting prose is still ungrounded, and detecting that in free text does not
work: a keyword check also rejects the correct sentence, because compliant text
restates the prohibition. Details in [`AI_USAGE.md`](AI_USAGE.md).

**The measured cost of that choice.** The live run shows this is a real trade-off, not
a free win. Assembled text was grounded in every case but context-insensitive in 6 of
20 — asking for an invoice ID the ticket supplies, or opening with a slow-export
condition when the export never completes. The baseline's model-written text read
better on those same cases, but invented remediation steps absent from `KB-SEC-01` on
two others. Documented rather than fixed, because changing assembly would invalidate
the reported evaluation.

---

## Known limitations and failure modes

1. **The automated scorer does not read the output.** It checks structure and label
   agreement. A manual review found context-insensitive action text, a false-positive
   flag, and an unsupported summary detail — none of which any metric caught. The
   headline numbers are a floor on quality problems, not a ceiling.
2. **The summary is unvalidated.** Evidence quotes are checked character-for-character;
   the summary is not checked at all, though §3 requires it to be factual. A-006 shows
   an unsupported detail ("committed" where the ticket says "uploaded") surviving
   alongside 100% exact evidence.
3. **Flag precision, action relevance, and summary factuality are measured only by
   hand.** A manual review of all 20 outputs found action text context-insensitive in
   6 cases, 2 unjustified flags, and 1 unsupported summary detail. No code computes
   these, so they will not be recomputed on a future run. One item from that review
   *was* promoted into code: choosing a legitimate KB article for the wrong situation
   is now scored via `must_not_kb_ids`, which is how A-012 became an automated metric
   rather than a note. It still only catches wrong choices a case author anticipated.
4. **A flag false positive becomes a review false positive.** The escalate-only rule
   is correct but amplifies flag errors — the single review error is downstream of an
   unjustified `MISSING_INFO` on A-001, not independent of it.
5. **A plausible-but-wrong classification passes every check.** Category accuracy was
   100% for *both* arms, so nothing measured here distinguishes a right category from a
   wrong one. Largest residual risk, and the reason `SECURITY` and P0/P1 force review
   unconditionally.
6. **Accuracy and stability were measured on only 20 cases and three stability
   tickets, and tier invariance on a single pair. The results are indicative, not
   statistically conclusive.**
7. **The injection detector is evadable.** A test asserts an unlisted paraphrase slips
   through. Containment rests on the deterministic layer, not detection.
8. **The confidence threshold is inert at 0.75.** No case in the live run was reviewed
   solely on confidence, so it could be raised substantially or dropped to zero without
   changing an outcome. Choosing it from data needs cases where confidence decides,
   which this set lacks.
9. **One repair attempt is a reasoned assumption.** Schema-constrained output needed
   almost no repairs, so it remains largely untested.
10. **`supports` is unvalidated** — the assignment defines no vocabulary for it.
11. **Hebrew detector coverage is thinner than English.** A Hebrew paraphrase is more
    likely to evade the Layer-2 detector.
12. **Three safeguards were never exercised by the real model.** The `ticket_id`
    protection, KB-ID allowlist, and evidence check all measured 0 → 0 in the live run.
    Their measured value comes from the adversarial tests, so the live sample alone
    cannot justify their complexity.

## What I would do next, in priority order

1. **Add semantic metrics** — flag precision, action relevance, and a
   summary-entailment check. The manual review found more real problems than the whole
   automated suite; that gap should close before anything else is added.
2. **Repeat the tier-invariance experiment** with counterbalanced repeated pairs, so a
   tier effect can be separated from the run-to-run variance the baseline also showed.
3. **Make action assembly context-aware** — select applicable steps within an article,
   or return to model-authored text gated behind an entailment check against the KB.
   Not changed now, because it would invalidate the reported evaluation.
4. **Expand the case set**, particularly cases where confidence is the deciding review
   signal, so the threshold can be chosen from data.
5. **Calibrate confidence** against outcomes rather than self-report.
6. **Widen Hebrew detector coverage** from real ticket data rather than invented
   phrasings.
7. **An HTTP surface** if this became a service; the pipeline is already a pure
   function of (ticket, kb, provider, settings).

## Time spent

Approximately 4 hours.
