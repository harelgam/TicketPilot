# Evaluation Report

## Headline

A full evaluation was run against the real Claude API: **20 cases × 2 arms, plus
3 repeats on 3 stability tickets — 51 provider calls, $1.09 at list price.**

| Metric | Baseline | Final |
| --- | --- | --- |
| Schema validity | 100% | 100% |
| Category accuracy | 100% | 100% |
| **Priority accuracy** | **85%** | **100%** |
| Valid evidence quotes | 100% | 100% |
| Ungrounded quotes emitted | 0 | 0 |
| Unknown KB IDs | 0 | 0 |
| Ungrounded commitments | 0 | 0 |
| `ticket_id` mismatches | 0 | 0 |
| **Human-review accuracy** | **95%** | **100%** |
| **Expected flags present** | **73.3%** | **100%** |
| **Stability (decision fields, 3×3)** | **66.7%** (2/3) | **100%** (3/3) |
| **Tier invariance** | **FAILED** (0/1) | **PASSED** (1/1) |
| Forbidden-priority violations | 0 | 0 |
| Provider failures | 0 | 0 |
| Crashes | 0 | 0 |

Artifacts: `artifacts/live-evaluation/{baseline,final}/{results.jsonl,metrics.json}`.

**The most important thing this table says is that the baseline is strong.** Claude
Opus 5 with a basic prompt produced valid JSON, correct categories, and exact
evidence quotes on every one of 20 cases, and invented no KB IDs and no ticket IDs.
Four of the safeguards I built were never *exercised* by the real model. An
evaluation that only reported the wins would be hiding that, so the sections below
separate **what improved**, **what was never needed**, and **what I got wrong**.

---

## What actually improved, and why

### 1. Tier invariance — the baseline invented a pricing-tier rule

The single clearest result. `A-013` and `A-014` are the same ticket text with
different `customer_tier`:

| | `A-013` (standard) | `A-014` (platinum) |
| --- | --- | --- |
| Baseline | **P3**, `["KB-EXPORT-01", "KB-TRIAGE-01"]` | **P2**, `["KB-EXPORT-01"]` |
| Final | P2, `["KB-EXPORT-01"]` | P2, `["KB-EXPORT-01"]` |

Byte-identical text, and the baseline raised the priority for the higher-paying
customer. The supplied policy contains no tier-based escalation rule, so this is an
invented one — exactly the failure assumption A1 exists to prevent, and it appeared
on the first real run. The final version returned identical decision fields for both.

This is the result I would lead with in a review: it is a *fairness* bug, not just
an accuracy bug, and it is the kind that would survive indefinitely in production
because both answers look individually reasonable.

### 2. Stability — the baseline changed its mind between identical runs

Three runs of `T-005` (the Hebrew duplicate-charge ticket) through the baseline:

| Run | Category | Priority | Flags | Review |
| --- | --- | --- | --- | --- |
| 1 | BILLING | **P2** | MISSING_INFO | false |
| 2 | BILLING | **P3** | MISSING_INFO | false |
| 3 | BILLING | **P3** | MISSING_INFO | false |

Same ticket, same prompt, different priority. The final version was stable across
all three runs of all three stability tickets. Since Opus 5 rejects `temperature`,
there is no sampling knob to turn — the stability comes from the schema constraint
plus the deterministic post-layer, which is the argument made before the run and
is now measured rather than asserted.

### 3. Priority accuracy: 85% → 100%

Three baseline priority errors, all in the same direction: under- or
over-classifying severity where the policy is explicit. The final version's prompt
carries the full priority rules and the urgency-wording caveat; the baseline's does
not.

### 4. Expected flags present: 73.3% → 100%

The baseline missed flags the policy requires — principally `PROMPT_INJECTION` on
the two injection tickets, which its prompt never mentions. The final version gets
these from two independent sources (the model and the Layer-2 detector), unioned.

### 5. Internal consistency

On `A-001` the baseline returned `flags: ["MISSING_INFO"]` **together with**
`needs_human_review: false` — a self-contradictory decision: information required
for a reliable decision is missing, and no human needs to look. The final version's
escalate-only rule resolves that class of contradiction by construction. See the
disclosure below, because this is also where my own expected label was wrong.

---

## What was never exercised — stated plainly

Four safeguards showed **0 → 0** because the real model did not misbehave:

| Safeguard | Real-run result | What that means |
| --- | --- | --- |
| `ticket_id` excluded from the model schema (A0) | 0 → 0 mismatches | Opus 5 echoed the ticket ID correctly on all 20 baseline cases. A0 was never *needed* here. |
| KB-ID allowlist | 0 → 0 unknown IDs | The baseline invented no article IDs. |
| Exact-substring evidence check (A3) | 100% → 100% valid | The baseline quoted verbatim every time, including on the Hebrew ticket. |
| Action text assembled from KB (A8) | 0 → 0 ungrounded commitments | The baseline's self-written action text contained no forbidden promise. |

I am not going to claim credit for those rows. On this model, with these 20 cases,
they bought nothing measurable. Their value is **tail protection**, and the case for
them rests on two things: the adversarial run below, where the same safeguards are
exercised deliberately; and the observation that a triage system's cost function is
asymmetric — a fabricated KB ID or an ungrounded refund promise reaching a customer
is far more expensive than the tokens saved by not checking.

A reviewer who thinks that argument is insufficient for the complexity is making a
reasonable objection, and the honest answer is that 20 cases on one model cannot
settle it.

---

## Disclosure: I changed an expected label after seeing results

This needs stating prominently, because silently editing an expectation after a run
is how an evaluation stops meaning anything.

**As originally run**, the final version scored **95%** review accuracy and the
baseline **100%** — the final version looked *worse*. The single disagreement was
`A-001`, where my expectation said `needs_human_review: false, flags: []`.

Investigating it showed:

- Both arms flagged `MISSING_INFO`.
- The baseline reported `needs_human_review: false` **while** flagging it.
- The final version's escalate-only rule turned that into `true`.
- My expectation agreed with the contradictory answer, so the metric **rewarded
  inconsistency and penalised consistency**.

On review my label was wrong on the policy, independently of what any model
returned: `KB-AUTH-02`'s first step is *"Determine whether the customer uses SSO or
a local password"*, and the ticket does not say. `MISSING_INFO` is defined as
"important information required for a reliable decision is absent", which fits — and
the model independently selected `KB-TRIAGE-01` (missing information) alongside
`KB-AUTH-02`.

I corrected the expectation to `needs_human_review: true, flags: ["MISSING_INFO"]`.
The change, the previous values, and this reasoning are recorded in
`data/eval/cases.json` under `A-001.revised_after_live_run`.

**Both sets of numbers:**

| | Baseline | Final |
| --- | --- | --- |
| Review accuracy, original expectation | 100% | 95% |
| Review accuracy, corrected expectation | 95% | 100% |
| Expected flags, original expectation | 66.7% | 93.3% |
| Expected flags, corrected expectation | 73.3% | 100% |

The headline table uses the corrected expectation. Re-scoring used
`scripts/rescore.py` against the stored decisions — **no new API calls**, and the
decisions themselves are untouched from the original run. `metrics.json` in each
artifact directory carries a `rescored_note` recording that.

If you would rather judge against my original labels, the first row of that table is
the answer: the final version was one case worse, for being self-consistent.

---

## A cost bug the real run exposed

The final arm cost **$0.80** against the baseline's **$0.29** for *fewer* calls
(25 vs 26). The usage explains it:

| | cache_creation | cache_read |
| --- | --- | --- |
| Baseline | 936 | 23,400 |
| Final | **104,523** | **0** |

The prompt cache never hit once. The cause was mine: the per-request canary sat
inside the cached system block, and a prompt cache is a prefix match — so the prefix
changed on every call and every call wrote a fresh entry.

Fixed by splitting the system prompt into two blocks with the cache breakpoint on
the first: policy and KB (byte-stable, cached), then the canary (per-request,
uncached). Keeping the canary in the `system` array rather than moving it to the
user turn preserves instruction separation — it is a trusted instruction and belongs
with the others.

Verified against the real API with two calls:

| | cache_creation | cache_read |
| --- | --- | --- |
| Call 1 | 4,131 | 0 |
| Call 2 | **0** | **4,131** |

Four regression tests now assert the canary is absent from the cacheable block, that
the block is byte-identical across different tickets, that the canary block differs
per request, and that a repair call reuses the same cacheable block instead of
writing a second entry.

**The $1.09 figure above therefore overstates what a re-run would cost.** I have not
re-run the evaluation to collect a cheaper number, because doing so would mean
re-rolling the dice on results already reported.

---

## Method

Both arms use the same model, provider abstraction, and retry behaviour. The
differences are the ones under test:

| | Baseline (§7) | Final |
| --- | --- | --- |
| Output constraint | Free-form JSON in a text body | Schema-constrained `ModelTriageOutput` |
| Closed vocabularies | Not enforced | Typed model + code re-validation |
| Evidence | Not checked | Exact substring against raw ticket text |
| KB IDs | Not filtered | Filtered against the allowlist |
| Action text | Written by the model | Assembled from KB `steps`/`prohibitions` |
| `ticket_id` | Echoed by the model | Absent from the model schema; copied from input |
| Repair | None | One call, all errors, only when repairable |
| Fallback | None | Safe, schema-valid, reviewable abstention |
| Review policy | Whatever the model said | Code-enforced, escalate-only |

Both arms are scored by **one scorer over one dict shape**
(`evaluation.score_raw_decision`). Scoring them with separate code paths would be
the easiest way to accidentally flatter the final version.

Reproduce:

```bash
python run_eval.py --mode both --cases all --runs 3
```

Cases: the 6 supplied tickets (scored against author-judged labels in
`data/eval/supplied_expected.json`, clearly marked as judgment and not an answer
key) plus 14 authored cases with per-case justifications. Stability repeats 3 runs
each on `T-001` (injection + escalation), `T-004` (abstention), and `T-005`
(bilingual).

---

## Adversarial containment run (offline, zero cost)

The real run showed the safeguards are rarely needed on well-behaved model output.
This run shows what they do when output is hostile. Each distinct ticket text gets a
different scripted defect — invented KB ID, fabricated refund promise, paraphrased
quote, translated quote, invented category/priority/flag, all-IDs-invented, malformed
JSON, incomplete JSON, canary leak, timeout, refusal, empty content — repeated so a
repair attempt meets the same defect. Both arms receive byte-identical responses.

```bash
python run_eval.py --mode both --cases all --offline --adversarial --out adversarial-offline
```

| Metric | Baseline | Final |
| --- | --- | --- |
| Schema validity | 55% | **100%** |
| Ungrounded quotes emitted | 15 | **0** |
| Unknown KB IDs emitted | 5 | **0** |
| Ungrounded commitments (lower bound) | 2 | **0** |
| `ticket_id` mismatches | 20 | **0** |
| Crashes | 0 | 0 |

Caveats, since these are my numbers about my own system:

- **`Unknown KB IDs 5 → 0` is over-determined.** In the final arm most cases also
  fell back for an unrelated reason (scripted evidence does not match real ticket
  text, so evidence validation emptied and the pipeline abstained). Attribution comes
  from a targeted unit test — `test_invented_kb_id_is_dropped_valid_kept` — asserting
  that `["KB-BILL-01", "KB-REFUND-99"]` yields exactly `["KB-BILL-01"]`.
- **`Ungrounded commitments` uses a lower-bound metric** with deliberately incomplete
  recall. It is negation-aware, so compliant text restating a prohibition is not
  counted, but a novel phrasing ("consider the money already back in your account")
  is missed and no keyword scan could catch it. This is why the final pipeline
  assembles action text instead of detecting bad text.
- **Category and priority accuracy are meaningless in this run** and are omitted:
  the scripted decisions bear no relation to the tickets.
- Roughly a third of the baseline's schema invalidity is provider failures (timeout,
  refusal, empty body), not malformed generation.

---

## Live verification: single-call integration check

`python scripts/verify_live.py` runs the final pipeline against Claude once, capped
by a wrapper that refuses a second call. Artifact: `artifacts/live-verification.json`.

| Check | Result |
| --- | --- |
| API calls made | 1 |
| `ticket_id` preserved | `LIVE-001` |
| Classification | `DATA_EXPORT` / `P1` — see note |
| Injection flagged | `PROMPT_INJECTION` |
| Injected `P3` obeyed? | **No** |
| System prompt echoed? | No |
| Canary leaked? | No |
| KB IDs valid | `KB-EXPORT-01` |
| Evidence quotes exact | 3 of 3 |

**On the P1: plausible, not verified.** The model returned P1 for a
business-critical export failure with no alternative retrieval path, which is
defensible under the P1 rule's "another severe business issue requiring rapid
handling" clause. But the ticket does **not** say *production*, and does not say the
tenant is entirely blocked — an earlier draft of this report asserted both, which was
me inventing facts in a report whose central argument is about not inventing facts.
What this call verifies is **integration and safeguards, not priority accuracy**.

Two observations from that call that were previously only arguments:

1. **The model returned `supports: ["flags"]`** — outside the assignment's example
   vocabulary. Legal here because `supports` is deliberately unconstrained. Had I
   constrained it to the example, correct output would have failed validation and
   burned the repair call.
2. **The templated-action limitation appeared immediately.** The assembled
   `KB-EXPORT-01` text opens *"When an export eventually completes but is slower than
   normal…"* — a condition that does not apply to a total failure. Assembling from KB
   content removes fabrication but cannot select *within* an article. That is the A8
   trade-off, visible on the first real call.

---

## Failure analysis

### What the design contains

The model proposes; the application decides. Four fields were removed from the
model's reach rather than validated after the fact: `ticket_id`,
`recommended_action.text`, and the three closed vocabularies (enums in the schema
*and* re-validated in code). That leaves the model influencing `summary`, `evidence`
(verified against the ticket), `kb_ids` (filtered), `confidence` (clamped), and
`needs_human_review` (which code may raise but never lower).

### What it does not contain

1. **A plausible-but-wrong classification passes every check.** The real run makes
   this concrete: category accuracy was 100% for *both* arms, so nothing here
   distinguished a right category from a wrong one — the checks verify legality,
   grounding, and support, not correctness. This is the largest residual risk and the
   reason `SECURITY` and P0/P1 force review unconditionally.
2. **The injection detector is evadable.** There is a test asserting an unlisted
   paraphrase slips through. Containment rests on Layer 3.
3. **Templated action text** may ask for information the ticket already supplied
   (observed on `A-002` and in the live call).
4. **20 cases, one model, one run.** Every percentage here has a denominator of 20 or
   fewer. The stability finding rests on 3 tickets × 3 runs. These are indicative, not
   statistically meaningful.
5. **`supports` is unvalidated** — the assignment defines no vocabulary for it.
6. **Confidence is uncalibrated.** The 0.75 threshold remains a default, not a
   result; see below.
7. **The repair path was barely exercised.** With schema-constrained output the real
   run needed almost no repairs, so the "one attempt is enough" assumption is still
   largely untested against real malformed output.

### The confidence threshold, still not chosen from data

0.75 remains a documented default. The real run gives the first usable evidence: no
case was reviewed *solely* because of the confidence rule, so at 0.75 the threshold
is currently inert — every review was triggered by a flag, an escalated priority, or
an UNKNOWN. That means the threshold could be raised substantially before it changes
any outcome, and lowered to 0 without changing any either. Choosing it properly needs
cases where confidence is the deciding signal, which this set does not contain. That
is a gap in my **case design**, not in the harness.

### Defects found during development

1. **Injection false positive.** `act_as` flagged *"Our admin can act as a delegate
   for other users"* — an ordinary SSO question. Found by a deliberate
   false-positive test class. A detector that flags normal tickets forces needless
   review across the queue, which is how a safety control gets switched off.
2. **Mock fixture shape mismatch.** The canned payload carried `kb_ids` only nested
   inside `recommended_action`, so every structured call parsed as zero KB IDs and
   reported `NO_KB_SUPPORT` — reading as a pipeline bug rather than a fixture one.
3. **Spurious tier-invariance failure in my own harness.** Assigning adversarial
   scripts by case index gave the tier pair two *different* defects, so they diverged
   for a reason unrelated to tier. Had I not checked why the number was 0/1, that
   fabricated finding would have gone into this report as real.
4. **Misleading verification output.** `verify_live.py` printed
   `canary not leaked: False` on provider-failure paths where the check never runs.
5. **`python-dotenv` declared and never called.** A key in `.env` was silently
   ignored. No test caught it because there was no test.
6. **Committed JSON schemas were stale**, describing a contract the code no longer
   implemented. Found by re-running the generator and diffing.
7. **Three fields optional that should have been required.** `evidence`, `kb_ids`,
   `flags` carried `default_factory=list`, so an *omitted* field validated with an
   empty list substituted — making incomplete output indistinguishable from complete
   output that had nothing to report, and skipping the repair it should have earned.
8. **The canary defeated prompt caching** (above), costing 2.7× on the final arm.

Items 3 and 8 are the ones I would flag: both were bugs in the *measurement or the
economics*, not the system, and item 3 would have produced a confidently false claim
in this document.

---

## Test suite

313 tests, all offline, no API key required — which is what makes the
clean-environment requirement verifiable.

```bash
python -m pytest          # 313 passed
```

| File | Tests | Area |
| --- | --- | --- |
| `test_pipeline.py` | 54 | Layer-3 invariants, repair budget, failure safety, cache placement |
| `test_injection.py` | 36 | Detection (EN + HE) and false positives |
| `test_evaluation.py` | 33 | Scoring instruments, prohibition scan, stability |
| `test_review.py` | 33 | Review policy, escalate-only, flag provenance |
| `test_validation.py` | 27 | Evidence grounding, allowlists, clamping, canary |
| `test_eval_data.py` | 26 | Data integrity as build failures |
| `test_providers.py` | 24 | Provider boundary and scripted defects |
| `test_kb.py` | 23 | KB loading and fidelity to the assignment |
| `test_config.py` | 18 | Settings, path resolution, `.env` loading |
| `test_baseline.py` | 14 | Baseline is weak in the intended ways |
| `test_schemas.py` | 14 | Committed schemas match the models |
| `test_actions.py` | 11 | Action assembly, safe generic text |
