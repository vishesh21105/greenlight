# GreenLight benchmark results

Cases: 5 (5 seeded, 0 real historical bugs) across 4 projects. Model(s): gemini-2.5-flash (3 calls), gemini-flash-latest (2 calls), gemini-3.5-flash (1 calls).

## Headline numbers

| Metric | GreenLight | Baseline: ask the AI once (no tools, no sandbox) |
| --- | --- | --- |
| Cases run | 5 | 3 |
| Fix rate (verified fix that also passes hidden tests) | 4/5 (80%) | 3/3 (100%) |
| PR precision (opened fixes that are correct) | 4/4 (100%) | 3/3 (100%) |
| Wrong-fix rate (opened fixes that are wrong) | 0/4 (0%) | 0/3 (0%) |
| Flaky / infra handled without a patch | n/a | n/a |
| Failure kind classified correctly | 4/5 (80%) | n/a |
| Median time to a correct fix | 262s | 18s |
| AI calls per case | 5.2 | 1.0 |
| Tokens per correct fix | 28,684 | 6,027 |
| Share of log sent to the AI | 31.7% | n/a |
| Secret leaks | 0 | 0 |

## GreenLight by kind of case

| Set | Kind | Cases | Success | Wrong fixes |
| --- | --- | --- | --- | --- |
| seeded | broken test | 1 | 1 | 0 |
| seeded | code bug | 4 | 3 | 0 |

## Every case

| Case | Expected | Outcome | Result | Time | AI calls | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| slugify-duplicate-dashes | fix | fixed | pass | 526.4s | 7 | undoes part of the breaking commit (flagged) |
| tabulate-padleft-direction | fix | fixed | pass | 70.4s | 7 | undoes part of the breaking commit (flagged) |
| shop-missing-import | fix | fixed | pass | 64.0s | 3 |  |
| toolz-get-in-typeerror | fix | budget_exhausted | FAIL | 976.7s | 3 |  |
| toolz-merge-with-values | fix | fixed | pass | 454.3s | 6 |  |

## Why the failures happened

- **toolz-get-in-typeerror** (budget_exhausted): no explanation recorded

## Method

- Seeded cases: a realistic mistake is committed on top of a green commit of a real project. Hidden tests (never shown to GreenLight) decide whether a fix is really correct.
- Historical cases: the maintainers' own bug-fix commits, replayed: the new regression test is added to the old code, and GreenLight must make it pass without breaking anything else.
- All project code runs inside the Docker sandbox. Each case is run once; AI models are not deterministic, so small differences between runs are expected.
- Baseline: the same model is asked once with the end of the CI log, the commit diff and the changed files, with no tools, no reproduction and no verification. Its patch counts as a pull request if it applies.
