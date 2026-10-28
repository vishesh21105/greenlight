# Architecture and threat model

## The one important split

The part that holds secrets (GitHub token, AI key) never runs the project's code, and the sandbox that
runs the code never sees a secret. Like a bank: the teller with the cash never opens strangers' parcels.

```text
GitHub ──webhook──▶ API (FastAPI) ──▶ jobs table (SQLite) ──▶ worker thread
                                                               │
                         ┌─────────────────────────────────────┤
                         ▼                                     ▼
                  AI model (adapter)                   Docker sandbox
                  reasoning only                       runs tests, no secrets,
                                                       no network during tests
                         │
                         ▼
         GitHub ◀── draft PR + comment ◀── verified patch
```

## Incident states

```text
received → log_trimmed → classified → reproducing → investigating → verifying → fixed
                             │              │              │              │
                           infra    flaky / not_reproduced  gave_up /    (fails → back to
                                                            budget_exhausted  investigating,
                                                                              max 2 repairs)
```

Every incident ends in exactly one final state: `fixed`, `diagnosed_no_fix`, `flaky`, `not_reproduced`,
`infra`, `gave_up`, `budget_exhausted` or `error`. The worker has a safety net that marks an incident
`error` if anything leaves it open.

## Design decisions (and why)

| Decision | Why |
| --- | --- |
| The agent proposes *text replacements*, GreenLight builds the diff | AI models often get diff line numbers wrong; exact-match replacements either apply correctly or fail loudly |
| Reproduce before fixing | A failure GreenLight cannot reproduce cannot be verified, so it is not patched |
| Policy read from the last **green** commit | A bad commit cannot loosen the rules for its own fix |
| Rules first, AI second for classification | Fast, free and predictable for the common cases |
| SQLite + jobs table instead of PostgreSQL + Redis | One file, nothing to install; the jobs table and the AI adapter are the seams to swap later |
| One bounded agent, not several | Cheaper, easier to reason about, easier to measure |

## Threat model

| Threat | Defence | Tested in |
| --- | --- | --- |
| Fake webhook triggers work | HMAC-SHA256 signature check with the shared secret | `test_api.py` |
| Code from a fork runs in our sandbox | Fork runs are ignored | `test_api.py` |
| GreenLight loops on its own fix branches | `greenlight/*` branches are ignored | `test_api.py` |
| Malicious code escapes the sandbox or phones home | Fresh container, `--network none` for tests, `--cap-drop ALL`, `no-new-privileges`, non-root user, read-only filesystem, CPU/memory/process/time limits, no Docker socket | `test_sandbox.py` |
| Secrets leak to the code under test | Sandbox gets a minimal environment; secrets are never passed in | `test_sandbox.py` |
| Secrets leak to the AI or the database | Scrubbing of keys, tokens, passwords and private keys on all text | `test_safety.py` |
| Prompt injection in logs or code ("ignore your rules...") | Structural: no shell tool, no tool can read secret files, every patch goes through the same checker whatever the AI believes | `test_agent.py` |
| AI edits CI config, secrets or its own policy | Always-blocked paths: `.github/**`, `.greenlight.yml`, `.env`, keys | `test_safety.py` |
| AI "fixes" a build by skipping or deleting tests | Checker rejects skip/xfail markers, removed assertions, removed test functions, deleted files | `test_safety.py` |
| Path tricks (`../`, symlinks) | Paths must stay inside the repository; symlinks rejected | `test_safety.py` |
| Shell injection through test names in logs | Test ids are filtered to safe characters; all commands are argument lists, never a shell | `test_logs_and_classify.py` |
| Runaway cost or endless loops | Step, cost, time and test-run budgets | `test_agent.py` |
| Wrong fix gets merged | Verification in a fresh sandbox, draft PR only, human approval, branch protection on `main` | `test_pipeline.py` |
| Public dashboard | Optional `DASHBOARD_PASSWORD`; retry needs `ADMIN_TOKEN` | `test_api.py` |

Known limits: the Docker sandbox reduces risk but is not a proof of perfect isolation; for a public,
multi-team service a stronger sandbox (e.g. gVisor or Firecracker microVMs) would be the next step.
