# GreenLight benchmark

The numbers GreenLight reports come from here, not from a few lucky demos. Results so far:
**[results/REPORT.md](results/REPORT.md)** (updated automatically every day).

## The cases (44)

| Set | What it is | Cases |
| --- | --- | --- |
| A, seeded | A realistic mistake committed on top of a green commit of a real project | 35 |
| B, historical | Real bugs from the projects' own history, replayed | 9 |

Projects: [toolz](https://github.com/pytoolz/toolz), [cachetools](https://github.com/tkem/cachetools),
[python-slugify](https://github.com/un33k/python-slugify), [tabulate](https://github.com/astanin/python-tabulate)
(each pinned to an exact commit) and the tinyshop demo.

Set A covers code bugs (renames that missed a caller, off-by-one slices, swapped arguments, wrong operators,
narrowed exception handling, missing imports...), outdated tests after an intended change, flaky tests, runner
and network failures, and **prompt-injection** cases where the code tells the AI to break the rules or leak a
secret. See [cases.py](cases.py).

**Hidden tests.** Every seeded case has extra tests GreenLight never sees. A fix only counts if they pass too,
so a patch that merely silences the visible failure is counted as a wrong fix.

**Historical cases** ([mine_history.py](mine_history.py)): for an upstream commit that fixed a bug and added a
test, GreenLight gets the old code plus the new test, and must make the suite pass. The maintainers' real fix
is kept as the answer key, and each case is only kept if that real fix passes here.

## What is measured

Fix rate, PR precision, wrong-fix rate, flaky/infra handled without a patch, classification accuracy, time to
a correct fix, AI calls and tokens, share of the log sent to the AI, secret leaks, and unsafe patches the AI
attempted (all blocked by the checker). Compared against:

- **Baseline:** the same model asked once with the end of the log, the commit diff and the changed files.
  No tools, no reproduction, no verification.
- **Ablation, no reproduction:** GreenLight without its "reproduce before fixing" step.
- **Ablation, raw log:** GreenLight sending the raw end of the log instead of its trimmed log.

## Running it

All project code runs inside the Docker sandbox.

```bash
python -m benchmark.build                     # build + validate the 35 seeded cases
python -m benchmark.mine_history              # build + validate the historical cases
python -m benchmark.run --mode greenlight     # or log_only / no_repro / raw_log; --only <case> to pick cases
python -m benchmark.report                    # writes results/REPORT.md
python -m benchmark.daily --budget 45         # one day's batch within an AI call budget
```

The [Benchmark workflow](../.github/workflows/benchmark.yml) runs `benchmark.daily` on GitHub Actions every
day just after the free Gemini quota resets, and commits the results. A case cut off by the quota is not
recorded and runs again the next day.

## Honest limits

- Each case is run once; AI models are not deterministic, so a rerun can differ by a case or two.
- On the free tier, runs can mix models (Gemini 3.5 Flash, then fallbacks when its daily quota is used up).
  The report lists which models were used.
- Dependency-upgrade failures are not covered: the benchmark runs without network inside the sandbox.
- The human-timing baseline (classmates fixing the same cases by hand) is not automated: time 4 to 5 people
  on 5 cases each and add the numbers to the report.
