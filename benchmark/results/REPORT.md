# GreenLight benchmark results

Cases: 7 (6 seeded, 1 real historical bugs) across 4 projects. Model(s) used: gemini-3.5-flash, gemini-2.5-flash, gemini-flash-latest.

## Headline numbers

| Metric | GreenLight | Baseline: ask the AI once (no tools, no sandbox) |
| --- | --- | --- |
| Cases run | 7 | 5 |
| Fix rate (verified fix that also passes hidden tests) | 6/7 (86%) | 4/5 (80%) |
| PR precision (opened fixes that are correct) | 6/6 (100%) | 4/4 (100%) |
| Wrong-fix rate (opened fixes that are wrong) | 0/6 (0%) | 0/4 (0%) |
| Flaky / infra handled without a patch | n/a | n/a |
| Failure kind classified correctly | 6/7 (86%) | n/a |
| Median time to a correct fix (including waits for the free AI tier) | 90s | 13s |
| Median GreenLight time to a correct fix (excluding AI wait) | 6s | 2s |
| AI calls per case | 6.6 | 1.0 |
| Tokens per correct fix | 33,720 | 6,175 |
| Share of log sent to the AI | 28.5% | n/a |
| Secret leaks | 0 | 0 |

## GreenLight by kind of case

| Set | Kind | Cases | Success | Wrong fixes |
| --- | --- | --- | --- | --- |
| seeded | broken test | 2 | 2 | 0 |
| seeded | code bug | 4 | 4 | 0 |
| historical | code bug | 1 | 0 | 0 |

## Every case

| Case | Expected | Outcome | Result | Time | AI calls | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| slugify-duplicate-dashes | fix | fixed | pass | 526.4s | 7 | reverts the broken line (reviewer warned) |
| tabulate-padleft-direction | fix | fixed | pass | 70.4s | 7 | reverts the broken line (reviewer warned) |
| shop-gst-rate-outdated-tests | fix | fixed | pass | 109.2s | 5 | edits a test (flagged) |
| shop-missing-import | fix | fixed | pass | 64.0s | 3 |  |
| toolz-get-in-typeerror | fix | fixed | pass | 40.7s | 5 | reverts the broken line (reviewer warned) |
| toolz-merge-with-values | fix | fixed | pass | 454.3s | 6 |  |
| hist-tabulate-5373ebfa | fix | gave_up | FAIL | 936.1s | 13 |  |

## Why the failures happened

- **hist-tabulate-5373ebfa** (gave_up): no explanation recorded

## Method

- Seeded cases: a realistic mistake is committed on top of a green commit of a real project. Hidden tests (never shown to GreenLight) decide whether a fix is really correct.
- Historical cases: the maintainers' own bug-fix commits, replayed: the new regression test is added to the old code, and GreenLight must make it pass without breaking anything else.
- All project code runs inside the Docker sandbox. Each case is run once; AI models are not deterministic, so small differences between runs are expected.
- Runs use the free Gemini tier, which queues requests and limits calls per day, so total times include waiting for the AI. The row excluding AI wait shows GreenLight's own work (sandbox runs, tools, checks).
- A case where the AI provider refused or timed out because of quota is not recorded; it runs again later.
- Baseline: the same model is asked once with the end of the CI log, the commit diff and the changed files, with no tools, no reproduction and no verification. Its patch counts as a pull request if it applies.
