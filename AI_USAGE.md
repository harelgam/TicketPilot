# AI Usage

Built with Claude Code.

## Division of work

Claude Code drafted the module implementations after I defined the architecture and
invariants. I reviewed the generated changes module by module, ran the offline and
live evaluations, investigated failures, rejected unsafe suggestions, and accepted
or modified the implementation based on observed behaviour. The design decisions
recorded as A0–A8 in the README came out of that review loop, several of them
replacing Claude's first proposal.

Specifically delegated:

- Extracting the assignment PDF into a requirements checklist, with inferred items
  separated out as marked assumptions.
- Drafting the modules once the invariants were fixed.
- Writing tests, including the false-positive and negative cases I asked for
  explicitly — those are what caught the real defects.
- Verifying SDK behaviour against the installed package rather than recalled API
  shapes.

## Two prompts that shaped the result

**1. Refusing the default reach for an agent.**

> "Do not add a vector database, UI, or unrelated infrastructure unless you can
> justify why it is necessary. What do you think about using an agent? … We can also
> use deterministic for things like ticket_id, category from allowlist only,
> priority from allowlist only, flags from allowlist only, KB IDs from provided KB
> list only, each evidence quote is an exact substring of the ticket…"

The response argued against the agent — 7 articles fit in a cached prompt, and a
tool-calling loop adds nondeterminism that degrades the stability metric the
assignment grades — and for pushing each listed invariant into deterministic code.
The "model proposes, application decides" framing came from this exchange.

**2. Making closed vocabularies structural.**

> "Category is a closed application-owned enum… validated using a typed schema.
> Unknown or invented category values trigger one repair attempt. If repair fails,
> the service returns category=UNKNOWN, priority=UNKNOWN, needs_human_review=true.
> … The model proposes values, but the application maintains the legal lists and
> prevents any invented values."

This forced a distinction I had not made: `kb_ids` degrades gracefully (drop
invalid, keep valid) because it is list-valued and has a designated flag for the
empty case, whereas `category` and `priority` are scalars with no partial credit and
hard-fail into abstention.

## A suggestion I rejected

Claude proposed a negation-aware regex scanner over `recommended_action.text` to
catch refund promises, delivery dates, and resolution times, skipping matches
preceded by "do not" or "never".

I rejected it for two reasons. The proposed test asserted that every KB action string
"passes the scan, so a future bad edit cannot introduce an unnegated promise" — an
unfalsifiable guarantee about an incomplete detector. *"The refund is already
approved"* and *"finance has guaranteed reimbursement"* contain no negatable verb the
detector knows. And the fragility is inherent: a keyword check also rejects the
*correct* sentence, because compliant output restates the prohibition.

The replacement: the model no longer writes action text. It proposes only `kb_ids`,
and the application assembles the text from each article's `steps[]` and
`prohibitions[]`, making an ungrounded promise structurally impossible. Storing the
KB that way also mirrors the assignment's own bullet structure, so the data is
verifiably faithful to the source.

The scanner survived as an *evaluation* instrument with its recall limitation stated,
used to put a floor under the baseline's ungrounded-commitment count. It is not on
the runtime path.

Smaller rejection: NFC-normalising evidence quotes before the substring check, to be
lenient about the Hebrew ticket. Section 3 says "exact substring"; normalising first
weakens it. Kept strict, with the normalisation result recorded as a diagnostic.

## Bugs found by testing rather than review

**Injection false positive.** The generated `act_as` pattern

```python
re.compile(r"\b(?:act|behave|respond)\s+as\s+(?:a\s+|an\s+|if\s+)", re.I)
```

correctly caught `"Act as a billing administrator."` and also caught `"Our admin can
act as a delegate for other users"` — an ordinary SSO question. Found by a
`TestNoFalsePositives` class populated with realistic benign tickets, not by reading
the regex. A detector that flags normal tickets forces needless review across the
queue, which is how a safety control gets disabled in practice. Fixed on the
linguistic distinction: an injection is an imperative and starts a clause, while
legitimate prose uses "can act as" mid-sentence. The offending string is a permanent
regression case.

**A prompt-cache bug no correctness test would catch.** The final arm cost $0.80
against the baseline's $0.29 for fewer calls. Usage showed 104,523 cache-creation
tokens and zero reads: the per-request canary sat inside the cached system block, and
a prompt cache is a prefix match, so every call wrote a fresh entry. Fixed by
splitting the system prompt into a cached policy/KB block and an uncached canary
block, keeping the canary in `system` so instruction separation survives. Verified
live: call 1 wrote 4,131 and read 0; call 2 wrote 0 and read 4,131. Four regression
tests pin the placement. Correctness tests passed either way — this was only visible
in the usage numbers of a real run.

**Two expected labels of mine were wrong, and my first correction was itself wrong.**
`A-001` was the final arm's only review miss. I initially changed the expectation to
require `MISSING_INFO` and review, reasoning that KB-AUTH-02 needs SSO-versus-password
information the ticket omits. On review that was wrong twice: `MISSING_INFO` concerns
information needed for a reliable *decision*, and AUTH/P2 is determinable without it —
and revising an expectation that had penalised my own system is not defensible even
when disclosed. Reverted; the final arm is counted as a review error, and the
baseline's contradiction on the same case is captured by a separate self-consistency
metric instead.

`A-006` was hedged as `any_of ["P0","P1"]` when the policy determines P0 — verified
exposure, still active, production key. The hedge masked a real final-arm error
(it returned P1; the baseline returned P0). Tightened to P0 exactly. Both corrections
move the numbers against the final version.

**Two defects in artifacts rather than behaviour**, both found by review rather than
by tests:

- The committed JSON schemas were stale, generated before `ModelTriageOutput` moved
  from `extra="forbid"` to `extra="ignore"`. A committed generated artifact needs a
  test asserting it is current; `tests/test_schemas.py` now compares each file to
  `model_json_schema()`, and I confirmed it fails against the stale versions before
  regenerating.
- `evidence`, `kb_ids`, and `flags` were optional when they should be required. With
  `default_factory=list`, an omitted field validated with an empty list substituted,
  so incomplete output was indistinguishable from complete output with nothing to
  report and skipped the repair it should have earned. Every existing test passed
  because they all supplied complete payloads.

**A declared dependency that was never called.** `python-dotenv` was in
`requirements.txt` and `pyproject.toml`, and nothing called `load_dotenv()`. A key
placed in `.env` was silently ignored. Caught by checking the wiring, not by a test —
there was no test. Now loaded at import with `override=False`, plus an
`api_key_status()` helper distinguishing *missing* from *present but empty*.

## The area I reviewed most carefully

The flag-provenance rules in `review.py`. Four degraded paths produce the same
`UNKNOWN`/`UNKNOWN`/`review=true` shape, and two must carry no flags:

| Path | Flags | Why |
| --- | --- | --- |
| Empty ticket | `["MISSING_INFO"]` | The information is absent from the ticket |
| No valid KB IDs | `["NO_KB_SUPPORT"]` | The KB was consulted and nothing applied |
| Schema validation failed | `[]` | A model-output failure says nothing about the ticket |
| Timeout / refusal | `[]` | An API error is no evidence about the KB — the lookup never ran |

`MISSING_INFO` on a schema failure is the tempting one: the pipeline does lack
information, so the flag feels right. But the flag is a claim about the *ticket*,
which may have been complete. `NO_KB_SUPPORT` on a timeout is the same error.

The flag vocabulary is closed and has no member for "the model or network failed", so
the correct output is no flag, with the reason in the run record. All four directions
are tested, including an assertion that a provider failure never yields
`NO_KB_SUPPORT`. I reviewed this hardest because the failure mode is silent: it
produces no crash and no failing test unless the test is written deliberately, and
puts false statements into production output.

## Where I did not take Claude's word

- **SDK behaviour.** Inspected the installed package rather than accepting a recalled
  call shape: `messages.parse()` merges `output_config` with the generated schema
  (`{**output_config, "format": ...}`), which is what lets `effort` and structured
  output compose.
- **Test counts in the report.** The first draft quoted per-file numbers from memory;
  several were wrong, and were regenerated from `pytest --collect-only`.
- **Causal claims from the evaluation.** An earlier draft called the tier-invariance
  result a proven fairness bug. Each tier was run once and the same baseline showed
  run-to-run instability, so the observation cannot separate a tier effect from
  variance. Reworded, with the experiment that would settle it named as the first
  next step.
- **Containment claims.** The adversarial run shows `Unknown KB IDs 5 → 0`, but most
  final-arm cases also fell back for an unrelated reason, so the zero is
  over-determined. The report says so and attributes the claim to a unit test.
- **"100% grounded" as a quality claim.** Action grounding is 100% by construction, not
  by measurement. Reading the outputs by hand found the assembled text
  context-insensitive in 6 of 20 cases, and found that the ungrounded-commitment
  metric reported 0 for the baseline while missing invented remediation steps it
  demonstrably produced. The scorer checks structure and label agreement; it does not
  read the output, and the manual review found more real problems than the entire
  automated suite.
