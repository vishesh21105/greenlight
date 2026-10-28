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
| `flaky` | A test fails on alternate runs | Labelled flaky after 3 sandbox runs, no patch |
| `infra` | The CI runner ran out of disk | Recognised from the log alone, re-run suggested |

Without an AI key the demo uses a **scripted replay** of the agent (clearly marked on the timeline).
Set `LLM_API_KEY` and `LLM_MODEL` (see `.env.example`) and the same demo runs with a real model.

> The demo uses the `local-noinstall` sandbox, which runs tests on your machine with **no isolation**.
> It is only for these demo repositories. Real repositories always use the Docker sandbox.

## Use it on a real GitHub repository

See **[docs/setup-github-app.md](docs/setup-github-app.md)**. In short: create a GitHub App, install it on a
repository, put its ID, key and webhook secret in `.env`, make sure Docker is running, then:

```bash
set -a; source .env; set +a
uvicorn app.main:app --port 8000
```

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
  github_client.py   GitHub App access, plus a local stand-in for the demo
  metrics.py         numbers for the metrics page
  demo.py            the offline demo
demo/tinyshop/       the small shop library used as the demo repository
tests/               65 tests covering every safety rule and the full loop
```

## Safety in one paragraph

The AI only *requests* actions; GreenLight checks every request in code. There is no shell tool. Untrusted
code runs only in a throwaway Docker container with no network during tests, no secrets, and CPU, memory,
process and time limits. Patches are checked against the repository's `.greenlight.yml` (read from the last
*green* commit, so a bad commit cannot loosen its own rules): no workflow files, no secret files, no deleting
or skipping tests, size limits. A fix is only offered after the sandbox has seen the tests pass.

## Status and next steps

Built so far (plan phases 0 to 6, core): webhook, log trimming, classification, sandbox reproduction with flaky
detection, agent with tools and budgets, patch safety checks, verification with repair attempts, draft PRs and
comments, dashboard and metrics, 65 tests.

Next:
1. **Connect a real GitHub App and a real model** and run the `tinyshop` demo repository end to end.
2. **Benchmark** (plan phase 7): build the seeded and real failure sets in `benchmark/`, then measure fix rate,
   wrong-fix rate, time and cost against the baselines.
3. **Polish for the showcase**: deploy on one small VM, rehearse the live demo, record a backup video.
