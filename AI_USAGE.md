# AI Usage

This project was built with Claude Code. Below: what I delegated, two prompts that
materially shaped the result, a suggestion I rejected, a bug that testing found
rather than review, and the area I scrutinised hardest before accepting it.

## What I delegated

- **Requirements extraction.** Turning the assignment PDF into an explicit
  checklist grouped by section, with inferred items separated out as marked
  assumptions rather than folded in silently.
- **Design review, iteratively.** This was the most valuable part and took several
  rounds. I pushed back on the first architecture repeatedly and the design changed
  substantially each time (see below).
- **Implementation** of the modules once the design was settled, with me specifying
  the invariants rather than the code.
- **Test authoring**, including the false-positive and negative cases I asked for
  specifically, which is what caught the real defects.
- **SDK verification.** Rather than trusting recalled API shapes, checking the
  installed `anthropic` package directly for how `messages.parse()` handles
  `output_config` alongside `output_format`.

## Two prompts that shaped the result

**1. On architecture — refusing the default reach for an agent.**

> "Do not add a vector database, UI, or unrelated infrastructure unless you can
> justify why it is necessary. What do you think about using an agent? I think it
> can be smart to use Classification, Priority rules, Supplied Knowledge Base as a
> tool… We can also use deterministic for things like ticket_id, category from
> allowlist only, priority from allowlist only, flags from allowlist only, KB IDs
> from provided KB list only, each evidence quote is an exact substring of the
> ticket…"

This set the whole shape of the system. The answer that came back argued *against*
the agent — 7 articles fit in a cached prompt, and a tool-calling loop adds
nondeterminism that directly degrades the stability metric the assignment
grades — and for pushing every listed invariant into deterministic code. The
"model proposes, application decides" framing came out of this exchange and became
the organising idea.

**2. On closed vocabularies — making enforcement structural.**

> "There are answers that need to be structured and not free: Category is a closed
> application-owned enum… The category is validated using a typed schema. Unknown
> or invented category values trigger one repair attempt. If repair fails, the
> service returns category=UNKNOWN, priority=UNKNOWN, and needs_human_review=true.
> The system is a deterministic LLM pipeline, not an autonomous agent. The same
> principle should also apply to priority and flags. That is, the model proposes
> values, but the application maintains the legal lists and prevents any invented
> values."

This produced the ownership table in the README and forced a distinction I had not
made: `kb_ids` degrades gracefully (drop the invalid, keep the valid) because it is
list-valued and has a designated flag for the empty case, whereas `category` and
`priority` are scalars with no partial credit and must hard-fail into abstention.
A reviewer will ask why those two behave differently; the answer exists because
this prompt forced it.

## A suggestion I rejected

**Claude proposed a negation-aware prohibition scanner over
`recommended_action.text`** — regex for refund promises, delivery dates, and
resolution times, skipping matches preceded by "do not" / "never" so that
compliant text restating a prohibition would not be flagged.

I rejected it and the design changed instead. Two problems:

1. **It cannot be honest.** The proposed test assertion was that every KB action
   string "passes the negation-aware scan, so a future bad edit cannot introduce an
   unnegated promise." That is an unfalsifiable guarantee about an incomplete
   detector. *"The refund is already approved"*, *"you will receive the money
   shortly"*, and *"finance has guaranteed reimbursement"* are all promises
   containing no negatable verb the detector knows.
2. **The fragility is inherent, not fixable.** A keyword check rejects the
   *correct* sentence too, because compliant output routinely contains the
   forbidden words.

The replacement: the model no longer writes action text at all. It proposes only
`kb_ids`, and the application assembles the text from each article's `steps[]` and
`prohibitions[]`. An ungrounded promise becomes structurally impossible rather than
detected. Storing the KB as `steps`/`prohibitions` also happens to mirror the
literal bullet structure of the assignment, so the data is verifiably faithful to
the source.

The scanner did survive — demoted to an *evaluation* instrument with its recall
limitation stated, used to put a floor under the baseline's ungrounded-commitment
count. It is not on the runtime path and nothing depends on it.

A related smaller rejection: Claude proposed NFC-normalising evidence quotes before
the substring check, to be lenient about the Hebrew ticket. Section 3 says "exact
substring", and normalising first quietly weakens that. Kept strict; the
normalisation result is recorded as a diagnostic counter so a real run can say
whether strictness ever costs anything.

## A bug found by testing, not by trusting the code

**The injection detector's `act_as` pattern had a false positive on ordinary
support prose.**

Generated:

```python
re.compile(r"\b(?:act|behave|respond)\s+as\s+(?:a\s+|an\s+|if\s+)", re.I)
```

It correctly caught `"Act as a billing administrator."` — and also caught
`"Our admin can act as a delegate for other users — is that supported?"`, which is
an ordinary SSO delegation question.

It was found because I had asked for a `TestNoFalsePositives` class populated with
realistic benign tickets, not because anyone read the regex. First full run:

```
AssertionError: false positive: ('act_as',)
```

Why it matters beyond correctness: a detector that flags normal tickets forces
needless human review across the entire queue, and a review queue full of false
positives is how a safety control gets switched off in production. A false positive
here is not cosmetic.

The fix works on the linguistic distinction rather than by deleting the test: an
injection is an *imperative* and starts a clause, while legitimate prose uses "can
act as" / "acts as" / "acting as" mid-sentence. The pattern is now anchored to a
clause boundary, with politeness words allowed after it so "Please act as…" still
matches. The offending string is a permanent regression case.

**A cost bug the real evaluation exposed, which no test would have caught.**
The final arm cost $0.80 against the baseline's $0.29 for *fewer* calls. Usage showed
104,523 cache-creation tokens and **zero** cache reads: the per-request canary sat
inside the cached system block, and a prompt cache is a prefix match, so every call
wrote a fresh entry and none ever hit. Fixed by splitting the system prompt into a
cached policy/KB block and an uncached canary block — keeping the canary in `system`
rather than the user turn, so instruction separation survives. Verified against the
real API: call 1 wrote 4,131 tokens and read 0; call 2 wrote 0 and read 4,131. Four
regression tests now pin the cache placement. Worth recording because correctness
tests pass either way — this was only visible in the usage numbers of a real run.

**An expected label of mine was wrong, and the metric was rewarding the wrong
behaviour.** The final version's only review-accuracy miss was `A-001`, where my
expectation said `needs_human_review: false`. Investigating showed both arms flagged
`MISSING_INFO`, the baseline paired that flag with `needs_human_review: false` — a
self-contradictory decision — and my expectation agreed with the contradiction. So
the final version was marked wrong for being self-consistent. `KB-AUTH-02`'s first
step requires knowing whether the customer uses SSO or a local password, which the
ticket never says, so `MISSING_INFO` is correct and review follows. I corrected the
label, recorded the change and the previous values in `data/eval/cases.json` under
`revised_after_live_run`, and reported **both** sets of numbers in the evaluation
report — because editing an expectation after seeing a result is exactly the move
that needs disclosing rather than quietly making.

**Two defects a reviewer found that my tests did not cover.** Both are worth
recording because they share a cause — I had tested behaviour thoroughly and left
two *artifacts* of that behaviour unguarded:

- **The committed JSON schemas were stale.** `schemas/` is generated from
  `models.py`, and it had been generated before `ModelTriageOutput` changed from
  `extra="forbid"` to `extra="ignore"`. The committed files still advertised
  `additionalProperties: false` and carried outdated descriptions. Found by
  re-running the generator and diffing. A generated artifact that is committed
  needs a test asserting it is current, or it rots silently;
  `tests/test_schemas.py` now compares each file to `model_json_schema()` and I
  confirmed it fails against the stale versions before regenerating.
- **`evidence`, `kb_ids`, and `flags` were optional when they should be required.**
  With `default_factory=list`, a response that *omitted* them validated and
  Pydantic substituted empty lists — so an incomplete response looked identical to
  a complete one that genuinely had nothing to report, and it skipped the repair it
  should have earned. This is the subtler of the two: every test I had written
  passed, because they all supplied complete payloads. The fix makes the fields
  required (empty is fine, absent is not), which the schema-constrained request now
  also enforces at generation time.

**A dependency that was declared and never used.** `python-dotenv` was in
`requirements.txt` and `pyproject.toml`, and nothing anywhere called
`load_dotenv()`. A key placed in `.env` would have been silently ignored and shown
up as an opaque authentication error. This one was *not* caught by a test — there
was no test, which was exactly the problem; it was caught by checking the wiring
before handing the file over. The fix loads `.env` at import time with
`override=False` so a real environment variable still wins, and adds an
`api_key_status()` helper that distinguishes *missing* from *present but empty*,
because an empty key still occupies its precedence slot and gets sent as a
credential. Six regression tests now cover it, two running a subprocess against a
relocated project root to prove a `.env` value genuinely reaches the environment.

**Two more defects that testing caught**, both in my own measurement code rather
than the system, which is the more embarrassing category:

- The mock's canned payload carried `kb_ids` only nested inside
  `recommended_action` (the baseline's shape), while the final schema has it at top
  level. Every structured call parsed as zero KB IDs and reported `NO_KB_SUPPORT` —
  which reads as a pipeline bug rather than a fixture one.
- The adversarial evaluation assigned defect scripts by case index, which handed
  the tier-invariance pair two *different* defects. Its two halves diverged for a
  reason unrelated to tier, and the harness reported a tier-invariance failure that
  was not real. Had I not checked why the number was 0/1, that fabricated finding
  would have gone into the evaluation report as a genuine baseline weakness.

## The area I reviewed most carefully

**The flag-provenance rules in `review.py`.**

Four degraded paths produce the same `UNKNOWN`/`UNKNOWN`/`needs_human_review=true`
shape, and the temptation is to give them the same flags. Two of them must carry
none:

| Path | Flags | Why |
| --- | --- | --- |
| Empty ticket | `["MISSING_INFO"]` | True: the information is absent from the ticket |
| No valid KB IDs | `["NO_KB_SUPPORT"]` | True: the KB was consulted, nothing applied |
| Schema validation failed | `[]` | A model-output failure says nothing about the ticket |
| Timeout / refusal | `[]` | An API error is no evidence about the KB — the lookup never ran |

`MISSING_INFO` on a schema failure is the seductive one: the pipeline *does* lack
information, so the flag feels right. But the flag is a claim about the **ticket**,
and the ticket may have been perfectly complete. Attaching `NO_KB_SUPPORT` to a
timeout is the same error — `kb_ids` did end up empty, but not because no article
applied.

Since the flag vocabulary is closed and has no member meaning "the model or the
network failed", the correct output is no flag at all, with the reason recorded in
the run record. Emitting nothing is the only truthful option available. All four
directions are tested, including an explicit assertion that a provider failure
never produces `NO_KB_SUPPORT`.

I reviewed this hardest because it is the kind of error that never surfaces as a
crash or a failing test unless someone writes the test on purpose — it just puts
quietly false statements into production output, and downstream consumers would
have no way to know.

## Where I did not take Claude's word for it

- **SDK behaviour.** Instead of accepting a recalled call shape, I had the
  installed package inspected: `messages.parse()` merges `output_config` with the
  generated schema (`{**output_config, "format": ...}`), which is what lets
  `effort` and structured output compose. Verified before writing the provider.
- **Test counts in the evaluation report.** The first draft quoted per-file numbers
  from memory; several were wrong. They were regenerated from
  `pytest --collect-only` before the report was committed.
- **The claim that containment metrics prove containment.** The adversarial run
  shows `Unknown KB IDs 5 → 0`, but in the final arm most cases also fell back for
  an unrelated reason, so the zero is over-determined. The report says so and
  attributes the claim to a targeted unit test instead.
