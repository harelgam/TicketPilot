# Evaluation Report

## Results

Full baseline-to-final comparison against the real Claude API: 20 cases × 2 arms,
plus 3 repeats on 3 stability tickets. 51 provider calls, $1.09 at list price.

| Metric | Baseline | Final |
| --- | --- | --- |
| Schema validity | 100% | 100% |
| Category accuracy | 100% | 100% |
| Priority accuracy | 85% | **95%** |
| Forbidden-priority violations | 0 | **1** |
| Valid evidence quotes (exact substring) | 100% | 100% |
| Ungrounded quotes emitted | 0 | 0 |
| Unknown KB IDs | 0 | 0 |
| `ticket_id` mismatches | 0 | 0 |
| Human-review accuracy | **100%** | **95%** |
| Required flags present (inclusion) | 66.7% | **93.3%** |
| Cases with flags beyond the minimum | 11 | 6 |
| Self-contradictory decisions (flags + no review) | **5** | **0** |
| Stability, decision fields (3×3) | 66.7% | **100%** |
| Tier invariance (1 pair) | failed | passed |
| Action grounding | 100% | 100% |
| Action contextual relevance | not automated | **14/20** by manual review |
| Flag justification (extras) | not automated | **4/6 extras defensible**, 2 unjustified |
| Summary factual accuracy | not automated | **19/20** clear by manual review |
| Provider failures | 0 | 0 |
| Crashes | 0 | 0 |

Artifacts: `artifacts/live-evaluation/{baseline,final}/`. Reproduce with
`python run_eval.py --mode both --cases all --runs 3`.

**The final version is better on four metrics and worse on two.** It wins on priority
accuracy, required-flag recall, self-consistency, and stability. It loses on
review accuracy (one over-escalation) and forbidden-priority violations (one case
where the baseline was right and it was wrong). The rows marked *not measured* are
where the automated scorer cannot see the problem at all; a manual review of all 20
final-arm outputs supplied those figures, and that review is the most important part of
this report.

### How to read three of these rows

*Required flags present* is an **inclusion** metric: it checks that every required
flag is present and does **not** penalise additional flags, because the expected
lists specify minimum required sets rather than exhaustive ones. So 93.3% inclusion
is not 93.3% flag accuracy. The two rows beneath it supply what inclusion omits: 6
cases carried extra flags, of which manual review judged 2 unjustified.

*Category accuracy is 100% for both arms*, so nothing in this run distinguished a
correct category from an incorrect one.

*Action grounding is 100% for the final arm by construction*, not by measurement: the
text is assembled from KB content, so it cannot be ungrounded. That says nothing
about whether it was *relevant*, which is a separate and worse-performing property.

---

## Manual semantic review

The scorer checks structural conformance and agreement with expected labels. It does
not read the output. Reading it by hand found three classes of problem that every
automated metric passed.

### 1. Assembled action text is grounded but often context-insensitive

The final version's `recommended_action.text` is assembled from whole KB articles, so
it cannot select *within* an article or notice what the ticket already supplies:

| Case | Problem |
| --- | --- |
| T-005 | Asks for the invoice ID, which the ticket states (`INV-8842`) |
| A-002 | Asks for invoice and transaction IDs, both already provided |
| T-006 | Instructs revoke/rotate, though the ticket says the key was already revoked |
| A-007 | Opens "When an export eventually completes but is slower than normal" — the export fails every time |
| A-010 | Same slow-export branch, though the export never completes |
| A-012 | Asks whether the customer uses SSO or a local password — the ticket says SSO and no local passwords |
| A-012 | Asks whether a workaround exists — the ticket says there is no way in |

That is 6 of 20 cases with redundant or conditionally irrelevant steps. **All emitted
action text was grounded in the supplied KB. However, deterministic article-level
assembly sometimes produced redundant or conditionally irrelevant steps.** A-012 is
the worst: the final version selected KB-AUTH-02 (single-user login) for a
whole-tenant outage, so the entire action reads wrongly for the situation.

### 2. The baseline's action text was more contextual — and sometimes invented

The comparison is not one-sided. The baseline's model-written text handled these same
cases better: *"Confirm receipt of invoice ID INV-77120 and transaction ID 4417-QA"*
(A-002), *"Treat as a failing (never completing) export"* (A-007), *"open a production
incident and notify the on-call team"* (A-012).

But it also invented remediation steps. `KB-SEC-01` permits exactly three things
(revoke or rotate; open a security incident; never ask for the secret). The baseline's
A-006 action added *"remove the file from the public repository"*, *"noting that
history rewriting alone is insufficient once exposed"*, and *"audit recent usage of
the key for unauthorized access"*. T-006 added *"confirm the public issue/content was
removed"*. §4 forbids inventing a remediation step.

**The ungrounded-commitments metric reported 0 for the baseline and missed all of
this**, because it only matches refund, delivery-date, resolution-time and
ask-for-secret phrasings. That row is therefore misleading for the baseline, and its
stated incomplete recall is not a hypothetical caveat — it demonstrably failed here.

So the A8 trade-off is a genuine wash rather than a win: assembled text is always
grounded and sometimes irrelevant; model-written text is contextual and sometimes
fabricated.

### 3. Flag justification: 4 of 6 extras defensible, 2 not

All 20 final-arm decisions were reviewed by hand. Six carried flags beyond the
expected minimum, which the inclusion metric does not penalise:

| Case | Extra flag | Judgement |
| --- | --- | --- |
| A-005 | MISSING_INFO | Defensible — the ticket contradicts itself, so the true state is unknown |
| A-007 | MISSING_INFO | Defensible — KB-EXPORT-01 asks for export ID and start time; absent |
| A-010 | MISSING_INFO | Defensible — export ID and environment absent |
| A-011 | NO_KB_SUPPORT | Correct — no supplied article covers blanket HTTP 500 on all API calls |
| A-001 | MISSING_INFO | **Not justified** — AUTH/P2 is determinable; SSO-versus-password is needed for handling, not triage |
| A-012 | MISSING_INFO | **Not justified** — the ticket states production, single tenant, all users, SSO, no local passwords, no workaround |

**A-001 shows that flag precision and review precision are the same problem.** Its
unjustified `MISSING_INFO` is what forced the review escalation counted against the
final version. The escalate-only rule is correct — every allowed flag does warrant
review — but it therefore *amplifies* a flag false positive into a review false
positive. The single review error in the results table is downstream of a flag error,
not independent of it. That makes flag precision more important than treating it as a
secondary metric implies.

### 4. Summary factual accuracy: 19 of 20 clear

All 20 final-arm summaries were compared against their ticket text.

One clear unsupported detail: **A-006** says the developer *"accidentally committed a
file"* where the ticket says *uploaded* (העלה). Committing is a plausible route to
GitHub but is not stated; *"uploaded"* or *"exposed a file in a public GitHub
repository"* would be supported. Both arms made this error, so it is a model
tendency rather than a final-version one.

Two borderline: **T-001** renders "seven customer tenants … on production login" as
"seven **production** tenants", re-attaching the adjective; **A-001** adds the
inference "indicating an account-specific issue rather than an outage", which is
reasonable analysis but not stated.

This exposes a hole: **the summary is not validated at all.** Evidence quotes are
checked character-for-character, but §3 also requires the summary to be factual and
not invent facts, and nothing in the pipeline checks it. Evidence grounding at 100%
and summary factuality are independent properties; a deterministic check cannot
catch a substituted verb like "committed", so closing this needs an entailment check.

---

## Findings from the automated metrics

### 1. Self-consistency: 5 -> 0

The baseline returned at least one flag alongside `needs_human_review: false` in 5 of
20 cases. Every allowed flag names a condition warranting review, so those decisions
contradict themselves. The final version's escalate-only rule makes that impossible.

This is measured separately from review accuracy on purpose. On A-001 the baseline
matched the expected review outcome while being internally inconsistent, and the final
version was internally consistent while over-escalating. Both facts are real, and
collapsing them into one number hides one of them.

### 2. Priority accuracy: 85% → 95%, with one final-version error

The final version returned **P1 on A-006**, where P0 is required: `P0` covers "an
active, verified security or data-exposure incident", and the ticket has all three —
verified exposure, still active ("the file is still there"), production API key. The
baseline returned P0 correctly. This is the one forbidden-priority violation in the
table.

### 3. Stability: 66.7% → 100%

Three baseline runs of `T-005` returned P2, P3, P3 — same ticket, same prompt. The
final arm was stable across all three runs of all three stability tickets. Opus 5
rejects `temperature`, so no sampling parameter is involved; the stability comes from
the schema constraint plus the deterministic post-layer.

### 4. Observed tier-invariance failure in the baseline

`A-013` and `A-014` are the same text with different `customer_tier`:

| | `A-013` (standard) | `A-014` (platinum) |
| --- | --- | --- |
| Baseline | P3, `["KB-EXPORT-01", "KB-TRIAGE-01"]` | P2, `["KB-EXPORT-01"]` |
| Final | P2, `["KB-EXPORT-01"]` | P2, `["KB-EXPORT-01"]` |

**Evidence of potential tier sensitivity, not proof the tier caused the change.** Each
tier was run once and the same baseline showed run-to-run instability, so one paired
observation cannot separate a tier effect from variance. Establishing it needs
repeated counterbalanced paired runs comparing distributions.

---

## Safeguards the live run did not exercise

| Safeguard | Result |
| --- | --- |
| `ticket_id` excluded from the model schema (A0) | 0 → 0 mismatches |
| KB-ID allowlist | 0 → 0 unknown IDs |
| Exact-substring evidence check (A3) | 100% → 100% valid |

These were not exercised in the 20-case live run. Their measured value comes from the
adversarial tests below, so the live sample alone cannot justify their complexity. The
remaining argument is the asymmetric cost of the failures they prevent.

A8 (assembled action text) is a different case: it *was* exercised, and the result is
mixed rather than absent — see the manual review.

---

## Expectation corrections, disclosed

Two expected labels changed after the run. Both are recorded in
`data/eval/cases.json` under `revised_after_live_run`, and both move the numbers
**against** the final version.

**A-006: `any_of ["P0","P1"]` → `P0` exactly.** The original hedge was wrong and
masked a real error. The case exists as the active-exposure mirror of T-006's
contained exposure; accepting P1 removed the distinction it was built to test.
Effect: final priority accuracy 100% → 95%, and one forbidden-priority violation.

**A-001: reverted to the original expectation.** I had briefly changed this to expect
`MISSING_INFO` and review after the live run showed it as the final version's only
review miss. That was wrong on the policy — `MISSING_INFO` concerns information
required for a reliable *decision*, and AUTH/P2 is determinable without knowing SSO
versus local password, which matters for downstream handling — and wrong on method,
because revising an expectation that penalised my own system is not defensible even
when disclosed. Effect: final review accuracy 100% → 95%; the baseline's inconsistency
on the same case is captured by the self-consistency metric instead.

Re-scoring used `scripts/rescore.py` against stored decisions: no new API calls, and
the decisions are unchanged.

---

## A prompt-cache bug the run exposed

The final arm cost $0.80 against the baseline's $0.29 for fewer calls (25 vs 26):

| | cache_creation | cache_read |
| --- | --- | --- |
| Baseline | 936 | 23,400 |
| Final | 104,523 | 0 |

The cache never hit. The per-request canary sat inside the cached system block, and a
prompt cache is a prefix match, so every call wrote a fresh entry. Fixed by splitting
the system prompt into a cached policy/KB block and an uncached canary block, keeping
the canary in `system` so instruction separation survives. Verified live — call 1:
4,131 written, 0 read; call 2: 0 written, 4,131 read. Four regression tests pin the
placement.

The $1.09 above therefore overstates a re-run. The evaluation was not re-run at the
lower cost, because that would replace reported results with a fresh set.

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
(`evaluation.score_raw_decision`), so neither can be scored more leniently.

Cases: 6 supplied tickets scored against author-judged labels in
`data/eval/supplied_expected.json` (marked as judgment, not an answer key), plus 14
authored cases with per-case justifications. Stability repeats `T-001` (injection),
`T-004` (abstention), `T-005` (bilingual).

---

## Adversarial containment (offline, zero cost)

The live run shows the safeguards are rarely needed on well-behaved output. This shows
what they do when output is hostile. Each distinct ticket text receives a different
scripted defect — invented KB ID, fabricated refund promise, paraphrased and
translated quotes, invented enum values, malformed JSON, canary leak, timeout,
refusal, empty content — repeated so a repair meets the same defect. Both arms receive
identical responses.

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

Caveats: `Unknown KB IDs 5 → 0` is over-determined, since most final-arm cases also
fell back because scripted evidence does not match real ticket text — attribution
comes from `test_invented_kb_id_is_dropped_valid_kept`. The ungrounded-commitment
metric is a lower bound whose recall failure is demonstrated in the manual review
above. Category and priority accuracy are meaningless here and omitted. About a third
of the baseline's schema invalidity is provider failures.

---

## Limitations

1. **The scorer does not read the output.** It checks structure and label agreement.
   Every problem in the manual review section passed all automated metrics. This is
   the most important limitation, because it means the headline table is a floor on
   quality problems, not a ceiling.
2. **The summary is unvalidated.** Evidence quotes are checked character-for-character
   but the summary is not checked at all, and §3 requires it to be factual. A-006
   shows an unsupported detail surviving alongside 100% exact evidence.
3. **Flag precision is not measured.** The metric is inclusion-only; A-012 shows a
   false-positive flag it cannot see.
4. **Action relevance is not measured.** Grounding is guaranteed by construction;
   relevance is not, and 6 of 20 cases show redundant or irrelevant steps.
5. **A plausible-but-wrong classification passes every check.** Category accuracy was
   100% for both arms, so nothing here distinguished right from wrong.
6. **Small sample.** Denominators of 20 or fewer; stability on 3 tickets × 3 runs;
   tier invariance on one pair. Indicative, not statistically conclusive.
7. **The injection detector is evadable.** A test asserts an unlisted paraphrase slips
   through. Containment rests on the deterministic layer.
8. **The confidence threshold is inert at 0.75.** No case was reviewed solely on
   confidence, so it could be raised substantially or dropped to zero without changing
   an outcome. Choosing it from data needs cases where confidence decides, which this
   set lacks.
9. **The repair path is barely exercised.** Schema-constrained output needed almost no
   repairs, so "one attempt is enough" remains largely untested.
10. **`supports` is unvalidated** — the assignment defines no vocabulary for it.

## What I would do next

1. **Add semantic metrics**: flag precision, an action-relevance judgement, and a
   summary-entailment check. The manual review found more real problems than the whole
   automated suite; that gap should be closed before adding features.
2. **Repeat the tier-invariance experiment** with counterbalanced repeated pairs.
3. **Make action assembly context-aware** — select applicable steps within an article,
   or return to model-authored text gated behind an entailment check against the KB.
   Not changed now, because it would invalidate the reported evaluation.
4. **Expand the case set**, particularly cases where confidence is the deciding review
   signal.
5. **Widen Hebrew detector coverage** from real ticket data.

---

## Defects found during development

1. **Injection false positive.** `act_as` flagged *"Our admin can act as a delegate
   for other users"*. Found by a deliberate false-positive test class.
2. **Mock fixture shape mismatch** — `kb_ids` nested only inside
   `recommended_action`, so structured calls parsed as zero KB IDs.
3. **Spurious tier-invariance failure in the harness** — adversarial scripts assigned
   by case index gave the tier pair different defects. Would have produced a false
   claim in this report.
4. **`verify_live.py` printed `canary not leaked: False`** on paths where the check
   never runs.
5. **`python-dotenv` declared and never called.** A key in `.env` was silently ignored.
6. **Committed JSON schemas were stale.**
7. **Three fields optional that should have been required** — an omitted `evidence`,
   `kb_ids` or `flags` validated with an empty list substituted, skipping the repair it
   should have earned.
8. **The canary defeated prompt caching**, costing 2.7× on the final arm.
9. **Two expected labels were wrong** (A-006 hedged, A-001 revised in my own favour),
   both corrected against the final version's score.

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
