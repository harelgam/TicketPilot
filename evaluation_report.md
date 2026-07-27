# Evaluation Report

## Results

Full baseline-to-final comparison against the real Claude API: 20 cases × 2 arms,
plus 3 repeats on 3 stability tickets. 51 provider calls, $1.09 at list price.

| Metric | Baseline | Final |
| --- | --- | --- |
| Schema validity | 100% | 100% |
| Category accuracy | 100% | 100% |
| Priority accuracy | 85% | **100%** |
| Valid evidence quotes | 100% | 100% |
| Ungrounded quotes emitted | 0 | 0 |
| Unknown KB IDs | 0 | 0 |
| Ungrounded commitments | 0 | 0 |
| `ticket_id` mismatches | 0 | 0 |
| Human-review accuracy | 95% | **100%** |
| Required flags present (inclusion) | 73.3% | **100%** |
| Cases with flags beyond the minimum | 10 | 5 |
| Stability, decision fields (3×3) | 66.7% | **100%** |
| Tier invariance (1 pair) | failed | passed |
| Forbidden-priority violations | 0 | 0 |
| Provider failures | 0 | 0 |
| Crashes | 0 | 0 |

Artifacts: `artifacts/live-evaluation/{baseline,final}/`. Reproduce with
`python run_eval.py --mode both --cases all --runs 3`.

**How to read two of these rows.**

*Required flags present* is an inclusion metric: it checks that every required flag
is present. It does **not** treat additional flags as errors, because the expected
lists specify minimum required flags rather than exhaustive sets. The adjacent row
counts cases where extra flags appeared, so 100% inclusion cannot be misread as
exact-match flag accuracy. In the final arm the five extras were `MISSING_INFO` on
A-005, A-007, A-010, A-012 and `NO_KB_SUPPORT` on A-011 — all defensible, and none
changed a review outcome, since every affected case expected review anyway.

*Category accuracy is 100% for both arms*, so nothing in this run distinguished a
correct category from an incorrect one. The checks verify legality, grounding, and
KB support — not correctness.

---

## Three findings

### 1. Observed tier-invariance failure in the baseline

`A-013` and `A-014` are the same ticket text with different `customer_tier`:

| | `A-013` (standard) | `A-014` (platinum) |
| --- | --- | --- |
| Baseline | P3, `["KB-EXPORT-01", "KB-TRIAGE-01"]` | P2, `["KB-EXPORT-01"]` |
| Final | P2, `["KB-EXPORT-01"]` | P2, `["KB-EXPORT-01"]` |

**This is evidence of potential tier sensitivity, not proof that the tier caused the
change.** Each tier was run once, and the same baseline showed run-to-run
instability elsewhere (finding 2), so a single paired observation cannot separate a
tier effect from ordinary variance. The final arm's pass is likewise one paired
observation.

Establishing a tier effect needs a repeated, counterbalanced paired experiment —
each tier run several times in alternating order, comparing distributions rather
than single draws. That is the first item in the next-steps list.

### 2. Run-to-run instability in the baseline

Three runs of `T-005` (Hebrew duplicate charge) through the baseline:

| Run | Category | Priority | Flags |
| --- | --- | --- | --- |
| 1 | BILLING | P2 | MISSING_INFO |
| 2 | BILLING | P3 | MISSING_INFO |
| 3 | BILLING | P3 | MISSING_INFO |

Same ticket, same prompt, different priority. The final arm was stable across all
three runs of all three stability tickets. Opus 5 rejects `temperature`, so there is
no sampling parameter involved; the stability comes from the schema constraint plus
the deterministic post-layer.

### 3. Priority accuracy and required flags

Priority 85% → 100%: the baseline prompt omits the priority rules and the
urgency-wording caveat. Required flags 73.3% → 100%: the baseline prompt never
mentions `PROMPT_INJECTION`, so it missed it on both injection tickets. The final
arm derives that flag from two independent sources (model plus regex detector),
unioned.

---

## Safeguards the live run did not exercise

Four safeguards measured 0 → 0 because the real model did not misbehave:

| Safeguard | Result |
| --- | --- |
| `ticket_id` excluded from the model schema (A0) | 0 → 0 mismatches |
| KB-ID allowlist | 0 → 0 unknown IDs |
| Exact-substring evidence check (A3) | 100% → 100% valid |
| Action text assembled from KB (A8) | 0 → 0 ungrounded commitments |

These safeguards were not exercised in the 20-case live run. Their measured value
comes from the adversarial tests below, so the live sample alone cannot justify
their complexity. The remaining argument for keeping them is the asymmetric cost of
the failures they prevent — a fabricated KB ID or an ungrounded refund promise
reaching a customer is more expensive than the checks.

---

## Disclosure: one expected label was corrected after the run

As originally run, the final arm scored **95%** review accuracy against the
baseline's **100%**. The single disagreement was `A-001`, where my expectation said
`needs_human_review: false, flags: []`.

What the data showed: both arms flagged `MISSING_INFO`, and the baseline paired that
flag with `needs_human_review: false` — a self-contradictory decision. My
expectation agreed with it, so the metric penalised the final arm for resolving the
contradiction.

The label was wrong on the policy, independent of what any model returned:
`KB-AUTH-02`'s first step is *"Determine whether the customer uses SSO or a local
password"*, which the ticket does not state. `MISSING_INFO` is defined as important
information being absent, which fits. I corrected the expectation to
`needs_human_review: true, flags: ["MISSING_INFO"]`; the change and previous values
are recorded in `data/eval/cases.json` under `A-001.revised_after_live_run`.

| | Baseline | Final |
| --- | --- | --- |
| Review accuracy, original labels | 100% | 95% |
| Review accuracy, corrected labels | 95% | 100% |
| Required flags, original labels | 66.7% | 93.3% |
| Required flags, corrected labels | 73.3% | 100% |

The table above uses the corrected labels. Re-scoring used `scripts/rescore.py`
against stored decisions — no new API calls, and the decisions are unchanged.

---

## A prompt-cache bug the run exposed

The final arm cost $0.80 against the baseline's $0.29 for fewer calls (25 vs 26):

| | cache_creation | cache_read |
| --- | --- | --- |
| Baseline | 936 | 23,400 |
| Final | 104,523 | 0 |

The cache never hit. The per-request canary sat inside the cached system block, and
a prompt cache is a prefix match, so every call wrote a fresh entry.

Fixed by splitting the system prompt into two blocks with the breakpoint on the
first: policy and KB (byte-stable, cached), then the canary (per-request, uncached).
The canary stays in `system` rather than moving to the user turn, so instruction
separation is preserved. Verified live — call 1: 4,131 written, 0 read; call 2: 0
written, 4,131 read. Four regression tests pin the placement.

The $1.09 above therefore overstates a re-run. The evaluation was not re-run at the
lower cost, because that would replace results already reported with a fresh set.

---

## Method

Both arms use the same model, provider abstraction, and retry behaviour. Differences
under test:

| | Baseline | Final |
| --- | --- | --- |
| Output | Free-form JSON in text | Schema-constrained `ModelTriageOutput` |
| Vocabularies | Not enforced | Typed model + code re-validation |
| Evidence | Not checked | Exact substring, raw text |
| KB IDs | Not filtered | Allowlist |
| Action text | Model-written | Assembled from KB |
| `ticket_id` | Model-echoed | Copied from input |
| Repair / fallback | None | One repair; safe abstention |
| Review | Model's answer | Code-enforced, escalate-only |

Both arms are scored by one function over one dict shape
(`evaluation.score_raw_decision`), so neither can be scored more leniently than the
other.

Cases: 6 supplied tickets scored against author-judged labels in
`data/eval/supplied_expected.json` (marked as judgment, not an answer key), plus 14
authored cases with per-case justifications. Stability repeats `T-001` (injection),
`T-004` (abstention), `T-005` (bilingual).

---

## Adversarial containment (offline, zero cost)

The live run shows the safeguards are rarely needed on well-behaved output. This run
shows what they do when output is hostile. Each distinct ticket text receives a
different scripted defect — invented KB ID, fabricated refund promise, paraphrased
and translated quotes, invented enum values, malformed JSON, canary leak, timeout,
refusal, empty content — repeated so a repair meets the same defect. Both arms
receive identical responses.

```bash
python run_eval.py --mode both --cases all --offline --adversarial --out adversarial-offline
```

| Metric | Baseline | Final |
| --- | --- | --- |
| Schema validity | 55% | 100% |
| Ungrounded quotes emitted | 15 | 0 |
| Unknown KB IDs emitted | 5 | 0 |
| Ungrounded commitments (lower bound) | 2 | 0 |
| `ticket_id` mismatches | 20 | 0 |
| Crashes | 0 | 0 |

Caveats:

- `Unknown KB IDs 5 → 0` is over-determined: most final-arm cases also fell back
  because scripted evidence does not match real ticket text. Attribution comes from
  `test_invented_kb_id_is_dropped_valid_kept`, which asserts
  `["KB-BILL-01", "KB-REFUND-99"]` yields exactly `["KB-BILL-01"]`.
- The ungrounded-commitment metric is a lower bound with incomplete recall. It is
  negation-aware, so compliant text restating a prohibition is not counted, but a
  novel phrasing is missed. This is why the pipeline assembles action text rather
  than detecting bad text.
- Category and priority accuracy are meaningless here and omitted; the scripted
  decisions bear no relation to the tickets.
- About a third of the baseline's schema invalidity is provider failures, not
  malformed generation.

---

## Limitations

1. **A plausible-but-wrong classification passes every check.** Category accuracy
   was 100% for both arms, so nothing here distinguished right from wrong. The
   checks verify legality, grounding, and support. This is the largest residual
   risk, and the reason `SECURITY` and P0/P1 force review unconditionally.
2. **Small sample.** Every percentage has a denominator of 20 or fewer; stability
   rests on 3 tickets × 3 runs; tier invariance on one pair. Indicative, not
   statistically conclusive.
3. **The injection detector is evadable.** A test asserts an unlisted paraphrase
   slips through. Containment rests on the deterministic layer.
4. **Templated action text** may ask for information the ticket already supplied —
   observed on `A-002` and in the live verification call, where the assembled
   `KB-EXPORT-01` text opens with a condition that does not apply to a total
   failure. Assembly removes fabrication but cannot select within an article.
5. **The confidence threshold is inert at 0.75.** No case was reviewed solely on
   confidence — every review was triggered by a flag, an escalated priority, or an
   UNKNOWN. The threshold could be raised substantially or dropped to zero without
   changing an outcome. Choosing it from data needs cases where confidence is the
   deciding signal, which this set lacks. That is a gap in case design.
6. **The repair path is barely exercised.** Schema-constrained output needed almost
   no repairs, so "one attempt is enough" remains largely untested.
7. **`supports` is unvalidated** — the assignment defines no vocabulary for it. The
   live call returned `supports: ["flags"]`, outside the assignment's example;
   constraining the field would have failed correct output.

---

## Defects found during development

1. **Injection false positive.** `act_as` flagged *"Our admin can act as a delegate
   for other users"*. Found by a deliberate false-positive test class. Fixed by
   anchoring to a clause boundary.
2. **Mock fixture shape mismatch.** The canned payload carried `kb_ids` only nested
   inside `recommended_action`, so structured calls parsed as zero KB IDs and
   reported `NO_KB_SUPPORT`.
3. **Spurious tier-invariance failure in the harness.** Adversarial scripts assigned
   by case index gave the tier pair different defects, so they diverged for a reason
   unrelated to tier. Fixed by keying on distinct ticket text.
4. **`verify_live.py` printed `canary not leaked: False`** on provider-failure paths
   where the check never runs.
5. **`python-dotenv` declared and never called.** A key in `.env` was silently
   ignored. No test caught it because there was no test.
6. **Committed JSON schemas were stale**, describing a contract the code no longer
   implemented.
7. **Three fields optional that should have been required.** `evidence`, `kb_ids`,
   `flags` carried `default_factory=list`, so an omitted field validated with an
   empty list substituted — making incomplete output indistinguishable from complete
   output with nothing to report, and skipping the repair it should have earned.
8. **The canary defeated prompt caching**, costing 2.7× on the final arm.

Items 3 and 8 were bugs in the measurement and the economics rather than the system.
Item 3 would have produced a false claim in this report.

---

## Test suite

313 tests, all offline, no API key required.

```bash
python -m pytest
```

| File | Tests | Area |
| --- | --- | --- |
| `test_pipeline.py` | 54 | Deterministic-layer invariants, repair budget, failure safety, cache placement |
| `test_injection.py` | 36 | Detection (EN + HE), false positives |
| `test_evaluation.py` | 33 | Scoring instruments, prohibition scan, stability |
| `test_review.py` | 33 | Review policy, escalate-only, flag provenance |
| `test_validation.py` | 27 | Evidence grounding, allowlists, clamping, canary |
| `test_eval_data.py` | 26 | Data integrity as build failures |
| `test_providers.py` | 24 | Provider boundary, scripted defects |
| `test_kb.py` | 23 | KB loading and fidelity to the assignment |
| `test_config.py` | 18 | Settings, path resolution, `.env` loading |
| `test_baseline.py` | 14 | Baseline is weak in the intended ways |
| `test_schemas.py` | 14 | Committed schemas match the models |
| `test_actions.py` | 11 | Action assembly, safe generic text |
