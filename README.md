# GreenLight

**When a build breaks, GreenLight tells you why, and hands you a tested fix to approve.**

GreenLight watches a project's CI pipeline (GitHub Actions). When a build turns red it:

1. reads the CI log and cuts it down to the few lines that matter,
2. works out what kind of failure it is (code bug, outdated test, dependency, flaky test, runner problem...),
3. **reproduces** the failure in an isolated sandbox before touching anything,
4. lets an AI agent investigate the code through a small set of safe tools,
5. **verifies** the proposed fix in a fresh sandbox (the failing tests, then the whole suite),
6. opens a draft pull request with the evidence, or posts an honest diagnosis if it is not confident.

A human approves every change. GreenLight never merges anything.

**See it working:** [GreenLight's pull request on tinyshop](https://github.com/vishesh21105/tinyshop/pull/1), opened
by itself after a broken commit: the cause in plain English, the two-line fix, and the sandbox's proof that the
failing tests and the full suite pass. **Benchmark:** [benchmark/results/REPORT.md](benchmark/results/REPORT.md),
44 cases from four real open-source projects plus the demo, updated daily.

---

## Try it in two minutes (no GitHub, no AI key)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                                     # all tests should pass

python -m app.demo                                      # creates 4 real broken builds and handles them
START_WORKER=0 uvicorn app.main:app --port 8000         # then open http://localhost:8000
```

The demo builds real git repositories with real broken commits and runs the full pipeline:

| Scenario | What happens | Expected result |
| --- | --- | --- |
| `rename` | A function was renamed but one caller was missed | Verified fix pushed to a `greenlight/fix-...` branch |
| `discount` | A business rule changed and a test disagrees | Honest diagnosis, no patch (a human must decide) |
| `flaky` | A test fails at random, like a race condition | Labelled flaky after repeated sandbox runs, no patch |
| `infra` | The CI runner ran out of disk | Recognised from the log alone, re-run suggested |

Without an AI key the demo uses a **scripted replay** of the agent (clearly marked on the timeline).
Set `LLM_API_KEY` and `LLM_MODEL` (see `.env.example`) and the same demo runs with a real model.
For a free Google Gemini key:

```bash
export LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
export LLM_API_KEY=<your key>
export LLM_MODEL=gemini-3.5-flash
export LLM_FALLBACK_MODELS=gemini-flash-latest,gemini-2.5-flash   # used when a model runs out of free quota
export LLM_MIN_INTERVAL_SECONDS=7                                  # free tier: few calls per minute
export LLM_REASONING_EFFORT=low
python -m app.demo
```

> By default the demo uses the `local` sandbox, which runs tests on your machine with **no isolation**; it is
> only for these demo repositories. With Docker running, `python -m app.demo --sandbox docker` uses the real
> isolated sandbox. Real repositories always use the Docker sandbox.

## Use it on a real GitHub repository

**Easiest: GitHub Actions mode** ([docs/github-actions-mode.md](docs/github-actions-mode.md)). Add one workflow
file and one secret to the repository; GreenLight runs on GitHub's own machines when CI fails, with Docker for
the sandbox. No server needed. This is how [tinyshop](https://github.com/vishesh21105/tinyshop) is set up.

```yaml
# .github/workflows/greenlight.yml (shortened; full file in the doc)
on:
  workflow_run: {workflows: ["CI"], types: [completed]}
permissions: {contents: write, pull-requests: write, actions: read}
jobs:
  greenlight:
    if: github.event.workflow_run.conclusion == 'failure'
    runs-on: ubuntu-latest
    steps:
      - uses: vishesh21105/greenlight@main
        with: {llm-api-key: "${{ secrets.LLM_API_KEY }}"}
```

**Server mode, with the live dashboard** ([docs/setup-github-app.md](docs/setup-github-app.md)): create a
GitHub App, install it on repositories, put its ID, key and webhook secret in `.env`, make sure Docker is
running, then `uvicorn app.main:app --port 8000`.

## How it is built

One Python app does everything: FastAPI serves the webhook, a JSON API and the dashboard pages; a background
thread works through a jobs table in SQLite; Docker runs the untrusted code. See
**[docs/architecture.md](docs/architecture.md)** for the design and the safety rules.

```text
app/
  main.py            web app: webhook, API, dashboard pages, starts the worker
  webhooks.py        GitHub signature check; decides which failed runs to handle
  jobs.py            jobs table + worker thread (replaces a queue server)
  pipeline.py        one incident from red build to a final state
  logs.py            log trimming (no AI)
  classify.py        failure categories: rules first, AI only when unsure
  sandbox.py         Docker sandbox (real) and local sandbox (dev only)
  agent/loop.py      the bounded agent loop and its prompt
  agent/tools.py     the agent's 8 narrow tools (no shell tool)
  patches.py         edits -> diff, and the patch safety checker
  policy.py          the .greenlight.yml house rules
  scrub.py           hides secrets before anything is sent or stored
  llm.py             one adapter for any OpenAI-compatible model
  github_client.py   GitHub access (App or Actions token), plus a local stand-in for the demo
  action.py          GitHub Actions mode entry point (used by action.yml)
  metrics.py         numbers for the metrics page
  demo.py            the offline demo
action.yml           the reusable GitHub Action
benchmark/           44 cases, runner, baselines, ablations, daily job and report
demo/tinyshop/       the small shop library used as the demo repository
tests/               80+ tests: every safety rule, the full loop, and real Docker sandbox tests
```

## Safety in one paragraph

The AI only *requests* actions; GreenLight checks every request in code. There is no shell tool. Untrusted
code runs only in a throwaway Docker container with no network during tests, no secrets, and CPU, memory,
process and time limits. Patches are checked against the repository's `.greenlight.yml` (read from the last
*green* commit, so a bad commit cannot loosen its own rules): no workflow files, no secret files, no deleting
or skipping tests, size limits. A fix is only offered after the sandbox has seen the tests pass.

## Status

Working end to end on GitHub: a broken commit on [tinyshop](https://github.com/vishesh21105/tinyshop) led to a
verified draft pull request with no human in the loop. The Docker sandbox is tested for real on GitHub's
runners (network off, no secrets, read-only code, unprivileged user, time and memory limits). The benchmark
runs daily within the free AI quota; see [benchmark/README.md](benchmark/README.md).

Known limits: Python projects on GitHub Actions only; dependency-upgrade failures are diagnosed but not part of
the benchmark; the free AI tier allows only a few fixes a day.
