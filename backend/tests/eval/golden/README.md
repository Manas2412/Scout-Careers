# The scoring golden set

Forty hand-labelled job descriptions. `MATCH_SCORING.md` §12.1 for the gates,
`TEST_PLAN.md` §6.1 for the slices, `AI_ARCHITECTURE.md` §10 for why.

This set is the only thing in the system that can say whether a change to the
prompt, the vocabulary, the formula or the résumés made the scorer **better**
rather than merely different. Without it, a change that moves 850 scores is a
shrug.

## The one rule

**Label from the job description, not from the scorer's output.**

Do not run `score show` on a posting you are about to label. Do not open the
extracted requirements first. A set labelled from what the system already
produces can never fail the system — it is a mirror with a test runner
attached, and it will report 1.00 forever while the thing it guards rots.

`eval pick` deliberately writes an empty label block even though the extractor
has already produced requirements for every one of these postings. Filling them
in for you would save a day and cost the entire value of the set.

The one that matters most: `expected_recommended_variant` is **the variant you
would actually send**, argued from the JD — not the one the formula currently
picks. The straddle slice exists precisely because `combined` *should* often win
and frequently does not, and the only way to see that is to have written down
what right looks like before measuring.

## Working through it

```bash
scout-careers eval status                              # what is labelled
scout-careers eval pick --slice consulting --count 6   # candidates, nothing written
scout-careers eval pick --slice consulting --write     # stubs to fill in
scout-careers eval run                                 # measure
```

Each case is two files: `NNN-slice.yaml` with the labels and `NNN-slice.txt`
with the verbatim JD. The text is a separate file because job descriptions run
to thousands of characters with their own quoting, and the adversarial slice
contains text designed to confuse a parser.

## The slices, and what each is for

| Slice | Count | Why it is in the set |
|---|---|---|
| `ai_product` | 6 | The base case must not regress |
| `backend` | 6 | Base case |
| `consulting` | 6 | The least-exercised variant |
| `mismatch` | 5 | Must score low and recommend nothing |
| `straddle` | 5 | Where `combined` should win, and often does not |
| `verbose` | 4 | Extraction precision under boilerplate |
| `terse` | 4 | Extraction recall with little to work with |
| `adversarial` | 4 | Injection resistance |

Skewed toward hard cases on purpose. A set sampled evenly from the corpus would
be mostly base cases and would pass whatever you shipped.

## Revising a label

Allowed, with `revision_reason` filled in. Revising silently is not — that is
the mechanism by which a golden set drifts into agreeing with whatever the
scorer currently does.

## What is measured

| Metric | Gate |
|---|---|
| Hard-requirement recall | ≥ 0.90 |
| Hard-requirement precision | ≥ 0.85 |
| `kind` accuracy | ≥ 0.88 |
| Normalisation accuracy | ≥ 0.95 |
| Coverage-level agreement (Cohen's κ) | ≥ 0.70 |
| Variant-ranking correlation (Spearman ρ) | ≥ 0.75 |
| Grounding violations | **0** |

Grounding gates at zero rather than a floor: a fabricated evidence span is the
same class of failure as an uncited claim, and an invariant with a 95% pass
rate is not an invariant.

A change also fails if any metric drops more than 2 points against its
baseline, even while clearing every floor. Trading four points of recall for one
of precision passes every absolute gate and is still a bad change.

## Cost

The set is free to label and costs roughly ₹35 per full run, because every case
goes through the real extraction chain. It runs on prompt, vocabulary, formula
and résumé changes — not on every commit.
