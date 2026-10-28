# Benchmark (next phase)

This folder will hold the evaluation described in the project plan.

- `cases/` — one YAML file per broken build:

```yaml
id: tinyshop-001
repo: <your-github-user>/greenlight-bench-tinyshop
green_sha: <commit that passes>
broken_sha: <commit that fails>
category: code_bug           # code_bug / broken_test / dependency / config_env / flaky / infra / injection
expected_files: ["tinyshop/checkout.py"]
hidden_tests: ["tests/hidden/test_checkout_extra.py"]   # never shown to the agent
```

- Set A: about 60 seeded failures across 4 to 5 small Python repositories.
- Set B: 15 to 25 real failures from public repositories (a failed run followed by a small fixing commit).
- Metrics: fix rate, PR precision, wrong-fix rate, classification accuracy, reproduction rate,
  time to fix PR (median and 90th percentile), cost per successful fix, log reduction, injection resistance.
- Baselines: log-only (no tools, no sandbox), no reproduction gate, no log trimming, and timed humans.

`app/demo.py` already shows how to create broken commits and simulate a CI log; the benchmark runner can
reuse `simulate_ci_log` and `process_incident` with `LocalCodeHost` and the Docker sandbox.
