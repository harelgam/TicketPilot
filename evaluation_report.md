# Evaluation Report

## Read this first: what was and was not measured

This project was built under a hard constraint that shapes every number below:
**the API budget was one call.** That has a direct consequence I want to state
before any table, rather than let a reader discover it in a footnote.

| Claim | Status |
| --- | --- |
| The real Claude integration works end to end | Verifiable with one call — `python scripts/verify_live.py` |
| Hostile or malformed model output is contained | **Measured**, reproducibly, at zero API cost (table below) |
| Failure modes degrade safely instead of crashing | **Measured**, 277 offline tests plus the adversarial run |
| Category and priority accuracy, baseline vs final | **Not measured.** Requires real model output. Command given below. |
| Run-to-run stability (3 tickets × 3 runs) | **Not measured.** Harness built and tested; needs real calls. |
| Confidence threshold chosen from data | **Not done.** Currently the documented default of 0.75. |

Two further honesty notes:

1. **No real baseline failures were observed.** Section 7 of the assignment asks
   for improvements made *after observing* baseline failures. I did not get to do
   that. The final design was derived from the assignment's stated requirements
   and from adversarial scripted testing, not from watching a real model fail. I
   am not going to write a retrospective failure narrative that reads as though I
   had. What follows distinguishes measurement from design intent throughout.
2. **The accuracy rows in the table below are meaningless and are shown struck
   through.** The scripted provider returns decisions unrelated to the tickets, so
   a category-accuracy figure from it measures nothing. They appear only because
   suppressing rows from a committed artifact would be worse.

---

## Method

Both arms run the same model, the same provider abstraction, and the same retry
behaviour. The only differences are the ones under test:

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

### The adversarial offline run

Each distinct ticket text is assigned one scripted defect, cycling for even
coverage: invented KB ID, fabricated refund promise, paraphrased quote,
translated quote, invented category, invented priority, invented flag, all-KB-IDs
invented, malformed JSON, incomplete JSON, canary leak, timeout, refusal, empty
content. Each script is repeated so that a repair attempt meets **the same**
defect — a defect that conveniently vanished on retry would overstate containment.

Both arms receive byte-identical hostile responses.

Reproduce:

```bash
python run_eval.py --mode both --cases all --offline --adversarial --out adversarial-offline
```

Committed output: `artifacts/adversarial-offline/{baseline,final}/`
(`results.jsonl` + `metrics.json`).

---

## Containment results (20 cases, zero API cost)

| Metric | Baseline | Final | Attribution |
| --- | --- | --- | --- |
| Schema validity | 55% | **100%** | Typed model + repair + always-schema-valid fallback |
| Ungrounded quotes emitted | 15 | **0** | Exact-substring check (A3) |
| Unknown KB IDs emitted | 5 | **0** | Allowlist filter — but see caveat |
| Ungrounded commitments (lower bound) | 2 | **0** | Action text assembled, not authored (A8) |
| `ticket_id` mismatches | 20 | **0** | Field absent from the model schema (A0) |
| Crashes | 0 | 0 | Neither arm crashes; the baseline simply returns nothing |
| Tier-invariance pairs holding | 1/1 | 1/1 | Both correct here |
| ~~Category accuracy~~ | ~~6.7%~~ | ~~25%~~ | **Meaningless** — scripted decisions are unrelated to tickets |
| ~~Priority accuracy~~ | ~~33.3%~~ | ~~25%~~ | **Meaningless** — same reason |

### Caveats on these numbers, stated plainly

**`ticket_id` 20 → 0 is the cleanest result.** It is fully attributable and
model-independent: the field does not exist in the model's output schema, so there
is no mechanism by which a wrong value could appear. The baseline's 20/20 failure
rate is an artifact of the mock always returning `MOCK-001`, but the *final* zero
is a structural guarantee, not a sample statistic.

**`Unknown KB IDs 5 → 0` is over-determined.** The allowlist filter would have
produced this, but in the final arm most cases also fell back for an unrelated
reason (the scripted evidence quote does not appear in real ticket text, so
evidence validation emptied and the pipeline abstained). I cannot attribute the
zero to the filter *from this run alone*. The attribution comes instead from a
targeted unit test — `test_invented_kb_id_is_dropped_valid_kept` — which asserts
that `["KB-BILL-01", "KB-REFUND-99"]` yields exactly `["KB-BILL-01"]`, keeping the
valid ID rather than discarding both.

**`Ungrounded commitments 2 → 0` uses a lower-bound metric.** The detector is
negation-aware, so compliant text restating a prohibition ("…and do not promise a
refund before investigation") is not counted. Its recall is *not* complete: a
novel phrasing such as "consider the money already back in your account" is not
caught, and no keyword scan could be. This is precisely why the final pipeline
does not rely on detection — it assembles action text from KB content, making the
failure structurally impossible rather than probabilistically caught. The metric
exists to put a defensible floor under the baseline figure, not to certify the
final one.

**Schema validity 55% is partly provider failures.** Three of the twenty cases got
a timeout/refusal/empty-content script. The baseline has no fallback, so those
produce no decision and count as invalid. The final arm returns a valid
abstention. That difference is real and is exactly what "failure safety" means,
but it is worth knowing that ~3 of the 9 baseline invalidities are infrastructure
rather than malformed generation.

---

## What is pending, and the exact command

Populating the accuracy and stability rows needs real model output. Estimated cost
at Opus 5 list pricing (~$0.02/call, less with the cached KB prefix):

```bash
# Full comparison, 20 cases x 2 arms + 3x3 stability. ~46 calls, under $1.
python run_eval.py --mode both --cases all --runs 3

# Cheaper: the six supplied tickets only, both arms. ~12 calls, ~$0.28.
python run_eval.py --mode both --cases supplied
```

The harness then fills in category accuracy, priority accuracy, review accuracy,
expected-flags-present, the 3×3 stability figure, and a real tier-invariance
check, into `runs/{baseline,final}/metrics.json`.

### Choosing the confidence threshold from data

Currently 0.75, which is a documented default and **not** an empirical choice.
`confidence` is configurable (`TICKETPILOT_CONFIDENCE_THRESHOLD`) precisely so it
can be set from evidence rather than baked in. The intended procedure, once real
confidences exist: sweep the threshold over the recorded values and pick the point
that maximises review recall on cases whose expected outcome is
`needs_human_review = true`, subject to an acceptable review rate on the cases
expected to pass unreviewed. Two of the authored cases (A-002, A-013/A-014) exist
specifically to keep that second constraint honest — a threshold that flags
everything is not a good threshold.

---

## Failure analysis

### What the design contains, and why

The load-bearing idea is that **the model proposes and the application decides**.
Four fields the model might get wrong were removed from its reach entirely rather
than validated after the fact:

- `ticket_id` — not in its schema; copied from input.
- `recommended_action.text` — not in its schema; assembled from KB content.
- `category`, `priority`, `flags` — closed enums in the schema *and* re-validated
  in code, because the code should never assume the API enforced anything.

That leaves the model influencing only `summary`, `evidence` (verified against the
ticket), `kb_ids` (filtered), `confidence` (clamped), and `needs_human_review`
(which code may raise but never lower). The blast radius of a successful prompt
injection is bounded by that list, which is the point of Layer 3.

### What it does not contain — real limitations

1. **The injection detector is not comprehensive and is not a security boundary.**
   It matches known phrasings in English and Hebrew. An attacker who reads
   `injection.py` can evade every pattern; there is an executable test asserting
   exactly that (`test_detector_is_not_claimed_to_be_complete`). Containment rests
   on Layer 3, not on detection.
2. **A plausible-but-wrong classification passes cleanly.** If the model returns
   `BILLING`/`P2` with a genuine exact-substring quote and a valid KB ID for a
   ticket that is really a security incident, every check passes. Nothing here
   verifies that the *category is right* — only that it is legal, grounded in a
   real quote, and supported by a real article. That is the largest residual risk
   and the reason `SECURITY` and P0/P1 force review unconditionally.
3. **Templated action text.** Assembling from `steps`/`prohibitions` removes
   fabrication but produces text that is not tailored to the ticket. A-002 shows
   the cost: the ticket already supplies the invoice and transaction IDs, and the
   assembled action asks for them anyway.
4. **Evidence strictness is untested against a real bilingual response.** The
   exact-substring rule is deliberately strict, with no normalisation. A
   diagnostic counter records whether a rejected quote *would* have matched under
   NFC + whitespace collapse, so a real run can say whether strictness ever costs
   anything on the Hebrew ticket. With no real run, that counter is empty and the
   question is open.
5. **`supports` is unvalidated.** The assignment illustrates
   `["category", "priority"]` but never defines a vocabulary, so constraining it
   would have invented a requirement. Only the quote is checked.
6. **One repair attempt is an assumption, not a finding.** The choice of exactly
   one was reasoned (a second adds cost, latency, and uncertainty) but the
   evaluation was supposed to test whether one suffices. It has not.

### Defects found during development, by testing rather than inspection

These are real, and each is fixed with a regression test:

1. **Injection false positive.** The `act_as` pattern flagged *"Our admin can act
   as a delegate for other users"* — an ordinary SSO question. A detector that
   flags normal tickets forces needless review across the whole queue, which is
   how a safety control gets switched off in production. Fixed by anchoring to a
   clause boundary, since an injection is an imperative while legitimate prose
   uses "can act as" mid-sentence.
2. **Mock fixture shape mismatch.** The canned payload carried `kb_ids` only
   nested inside `recommended_action` (the baseline's shape), so every structured
   call parsed as zero KB IDs and reported `NO_KB_SUPPORT` — which looked like a
   pipeline bug. The mock must satisfy both output shapes.
3. **Spurious tier-invariance failure in my own harness.** Assigning adversarial
   scripts by case index gave the tier-invariance pair two *different* defects, so
   the two halves diverged for a reason unrelated to tier and the harness reported
   an invariance failure that was not real. Fixed by keying script assignment on
   distinct ticket text.
4. **Misleading verification output.** `verify_live.py` printed
   `canary not leaked: False` on provider-failure paths, where the canary check
   never runs — displaying a not-applicable result as a breached safety check.

Item 3 is the one I would flag to a reviewer: it was a bug in the *measurement*,
not the system, and it would have produced a confidently wrong claim in this
report.

---

## Test suite

277 tests, all offline, no API key required — which is also what makes the
clean-environment requirement verifiable.

```bash
python -m pytest          # 277 passed
```

Coverage by file:

| File | Tests | Area |
| --- | --- | --- |
| `test_pipeline.py` | 50 | Layer-3 invariants, repair budget, failure safety |
| `test_injection.py` | 36 | Detection (EN + HE) and false positives |
| `test_evaluation.py` | 33 | Scoring instruments, prohibition scan, stability |
| `test_review.py` | 33 | Review policy, escalate-only, flag provenance |
| `test_validation.py` | 27 | Evidence grounding, allowlists, clamping, canary |
| `test_eval_data.py` | 26 | Data integrity as build failures |
| `test_providers.py` | 24 | Provider boundary and scripted defects |
| `test_kb.py` | 23 | KB loading and fidelity to the assignment |
| `test_baseline.py` | 14 | Baseline is weak in the intended ways |
| `test_actions.py` | 11 | Action assembly, safe generic text |

Three of those files are worth a reviewer's attention specifically:

- `tests/test_pipeline.py` — every Layer-3 invariant, stated as a property of the
  output rather than of the model.
- `tests/test_review.py` — flag provenance across all four degraded paths,
  including the two that must carry no flags.
- `tests/test_eval_data.py` — turns assignment requirements into build failures
  (minimum eight authored cases, legal expected values, no supplied ticket ID
  anywhere under `src/`).
