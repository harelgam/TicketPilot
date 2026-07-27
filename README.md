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

> **Evaluation status: complete.** A full baseline-to-final comparison was run
> against the real API — 20 cases × 2 arms plus 3×3 stability, 51 calls, $1.09.
> Headline results: priority accuracy 85% → 100%, expected flags 73% → 100%,
> stability 67% → 100%, and **tier invariance FAILED for the baseline** (identical
> ticket text, P3 for `standard` and P2 for `platinum`) while the final version
> passed.
>
> [`evaluation_report.md`](evaluation_report.md) also records what the run showed
> *against* me: four safeguards were never exercised because the model did not
> misbehave, one expected label of mine was wrong, and a canary-placement bug of
> mine defeated prompt caching and made the final arm cost 2.7× the baseline.

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
python -m pytest        # 313 tests, all offline
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
   as data. The tags are not a security mechanism — they make the trust boundary
   unambiguous to the model.
2. **Deterministic detector.** Known phrasings in English and Hebrew. It does not
   judge the ticket harmful, does not modify it, and does not block the call — it
   only adds `PROMPT_INJECTION` and forces review. Flags combine as a union with
   the model's, so either layer detecting is enough. **It is not comprehensive and
   is not a security boundary**; there is a test asserting that an unlisted
   paraphrase slips through.
3. **Invariants that do not depend on detection.** Even when both layers miss, the
   model cannot change the `ticket_id`, emit a value outside a closed vocabulary,
   invent a KB ID, fabricate an evidence quote, author the action text, cancel a
   policy-required review, or make the application echo the system prompt (a canary
   in the system prompt fails the response closed if it appears in output).

Layer 3 is the one that bounds the damage.

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

A **self-assessed score** from 0.0 to 1.0 for how certain the model is that the
**category + priority pair** is correct given only the ticket text. The prompt asks
for it in exactly those terms, so the request and this documentation agree: it is
explicitly **not** a calibrated probability, and **not** a measure of grounding
quality — a decision can be confident and ungrounded, which is why the evidence and
KB checks are separate and independent of it. It is used solely as one review
trigger among many.

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

**Why no agent framework.** The KB is 7 articles that fit in a cached system
prompt, so a tool-calling loop buys nothing while adding latency, cost, and
run-to-run nondeterminism — and nondeterminism directly degrades the stability
metric the assignment asks for. Classification and priority are a fixed policy,
better expressed as prompt policy plus a deterministic rule layer than as tools
the model may or may not choose to call. Considered and rejected deliberately.

**Why the action text is assembled rather than validated.** A valid KB ID paired
with contradicting prose is still ungrounded. Detecting that in free text does not
work: a keyword check rejects the *correct* sentence too, since compliant text
restates the prohibition ("…and do not promise a refund before investigation"), and
no keyword scan catches "the refund is already approved". Doing it properly needs
semantic grounding validation, which is non-deterministic and harder to defend.
Assembling from trusted content makes the failure impossible instead.

---

## Known limitations and failure modes

1. **A plausible-but-wrong classification passes every check.** Nothing verifies
   the category is *right*, only that it is legal, quoted from a real substring,
   and backed by a real article. This is the largest residual risk, and the reason
   `SECURITY` and P0/P1 force review unconditionally.
2. **The injection detector is evadable by design of the problem, not by oversight.**
   Containment rests on Layer 3.
3. **Action text is templated, not tailored.** It may ask for information the
   ticket already supplied.
4. **Accuracy and stability are unmeasured** (one-call API budget). See the
   evaluation report.
5. **One repair attempt is a reasoned assumption, not a measured finding.**
6. **`supports` is unvalidated** — the assignment defines no vocabulary for it.
7. **Confidence is uncalibrated.** The 0.75 threshold is a default, not a result.
8. **Single-language detector coverage is uneven.** English patterns outnumber
   Hebrew ones; a Hebrew paraphrase is more likely to evade Layer 2.

## What I would do next, in priority order

1. **Fund the evaluation** — populate accuracy, stability, and the threshold sweep.
   Everything is one command away; this is the biggest gap.
2. **Attack the plausible-but-wrong failure** — an LLM-judge or NLI check that the
   `summary` and `category` are entailed by the quoted evidence, gated behind a
   flag so it stays optional.
3. **Calibrate confidence** against outcomes rather than trusting self-report, and
   set the threshold from the curve.
4. **Model-authored action text with semantic grounding validation**, to recover
   ticket-specific wording without reintroducing fabrication.
5. **Widen Hebrew detector coverage**, ideally from real ticket data rather than
   invented phrasings.
6. **Prompt-cache metrics in the run record** — `cache_read_input_tokens` is
   captured but nothing asserts the prefix is actually being reused.
7. **An HTTP surface** if this needed to be a service rather than a CLI; the
   pipeline is already a pure function of (ticket, kb, provider, settings).

## Unfinished work

Carried deliberately, not overlooked:

- **The confidence threshold is still the default 0.75, not a value chosen from
  data.** The real run showed why: no case was reviewed *solely* because of the
  confidence rule, so at 0.75 the threshold is currently inert — every review was
  triggered by a flag, an escalated priority, or an UNKNOWN. Choosing it properly
  needs cases where confidence is the deciding signal, which this set does not
  contain. That is a gap in my case design, not in the harness.
- **The repair path is barely exercised.** With schema-constrained output the real
  run needed almost no repairs, so "one attempt is enough" remains largely untested
  against real malformed output.
- **Every percentage has a denominator of 20 or fewer**, and the stability finding
  rests on 3 tickets × 3 runs. Indicative, not statistically meaningful.
- **Four safeguards were never exercised by the real model** (`ticket_id`
  protection, KB-ID allowlist, evidence check, action assembly all measured 0 → 0).
  Their justification is tail protection plus the adversarial run, not the live
  numbers.
