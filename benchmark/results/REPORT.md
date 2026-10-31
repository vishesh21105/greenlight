# GreenLight benchmark results

Cases: 12 (10 seeded, 2 real historical bugs) across 5 projects. Model(s) used: gemini-3.5-flash, gemini-2.5-flash, gemini-flash-latest.

## Headline numbers

| Metric | GreenLight | Baseline: ask the AI once (no tools, no sandbox) |
| --- | --- | --- |
| Cases run | 12 | 7 |
| Fix rate (verified fix that also passes hidden tests) | 9/11 (82%) | 5/7 (71%) |
| PR precision (opened fixes that are correct) | 9/9 (100%) | 5/5 (100%) |
| Wrong-fix rate (opened fixes that are wrong) | 0/9 (0%) | 0/5 (0%) |
| Flaky / infra handled without a patch | 1/1 (100%) | n/a |
| Failure kind classified correctly | 9/12 (75%) | n/a |
| Median time to a correct fix (including waits for the free AI tier) | 109s | 18s |
| Median GreenLight time to a correct fix (excluding AI wait) | 6s | 4s |
| AI calls per case | 7.4 | 1.0 |
| Tokens per correct fix | 45,913 | 6,437 |
| Share of log sent to the AI | 25.9% | n/a |
| Secret leaks | 0 | 0 |

## GreenLight by kind of case

| Set | Kind | Cases | Success | Wrong fixes |
| --- | --- | --- | --- | --- |
| seeded | broken test | 3 | 2 | 0 |
| seeded | code bug | 6 | 6 | 0 |
| seeded | flaky | 1 | 1 | 0 |
| historical | broken test | 1 | 1 | 0 |
| historical | code bug | 1 | 0 | 0 |

## Every case

| Case | Expected | Outcome | Result | Time | AI calls | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| slugify-duplicate-dashes | fix | fixed | pass | 526.4s | 7 | reverts the broken line (reviewer warned) |
| slugify-flaky-network | flaky | flaky | pass | 9.2s | 1 |  |
| slugify-stopword-case | fix | fixed | pass | 1445.6s | 9 | reverts the broken line (reviewer warned) |
| tabulate-padleft-direction | fix | fixed | pass | 70.4s | 7 | reverts the broken line (reviewer warned) |
| shop-gst-rate-outdated-tests | fix | fixed | pass | 109.2s | 5 | edits a test (flagged) |
| shop-missing-import | fix | fixed | pass | 64.0s | 3 |  |
| toolz-drop-off-by-one | fix | fixed | pass | 23.2s | 3 | reverts the broken line (reviewer warned) |
| toolz-get-in-typeerror | fix | fixed | pass | 40.7s | 5 | reverts the broken line (reviewer warned) |
| toolz-merge-with-values | fix | fixed | pass | 454.3s | 6 |  |
| toolz-partition-pad | fix | budget_exhausted | FAIL | 150.6s | 16 |  |
| hist-cachetools-41e4b161 | fix | fixed | pass | 238.8s | 14 |  |
| hist-tabulate-5373ebfa | fix | gave_up | FAIL | 936.1s | 13 |  |

## Why the failures happened

- **hist-tabulate-5373ebfa** (gave_up): no explanation recorded
- **toolz-partition-pad** (budget_exhausted): no explanation recorded

## Method

- Seeded cases: a realistic mistake is committed on top of a green commit of a real project. Hidden tests (never shown to GreenLight) decide whether a fix is really correct.
- Historical cases: the maintainers' own bug-fix commits, replayed: the new regression test is added to the old code, and GreenLight must make it pass without breaking anything else.
- All project code runs inside the Docker sandbox. Each case is run once; AI models are not deterministic, so small differences between runs are expected.
- Runs use the free Gemini tier, which queues requests and limits calls per day, so total times include waiting for the AI. The row excluding AI wait shows GreenLight's own work (sandbox runs, tools, checks).
- A case where the AI provider refused or timed out because of quota is not recorded; it runs again later.
- Baseline: the same model is asked once with the end of the CI log, the commit diff and the changed files, with no tools, no reproduction and no verification. Its patch counts as a pull request if it applies.
