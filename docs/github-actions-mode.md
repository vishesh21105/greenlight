# Using GreenLight on any GitHub repository (GitHub Actions mode)

The easiest way to run GreenLight: no server, no GitHub App, no ngrok. GreenLight runs inside GitHub
Actions, right after your CI fails, using the token GitHub gives every workflow run.

Live example: [vishesh21105/tinyshop](https://github.com/vishesh21105/tinyshop) and its
[first GreenLight pull request](https://github.com/vishesh21105/tinyshop/pull/1).

## Setup (5 minutes)

1. **Add the workflow.** Create `.github/workflows/greenlight.yml` in the repository you want to protect:

   ```yaml
   name: GreenLight
   on:
     workflow_run:
       workflows: ["CI"]          # the name of your CI workflow
       types: [completed]

   permissions:
     contents: write              # push the greenlight/fix-* branch, comment on the commit
     pull-requests: write         # open the draft pull request
     actions: read                # download the failed run's logs

   jobs:
     greenlight:
       if: >-
         github.event.workflow_run.conclusion == 'failure' &&
         github.event.workflow_run.head_repository.full_name == github.repository &&
         !startsWith(github.event.workflow_run.head_branch, 'greenlight/')
       runs-on: ubuntu-latest
       timeout-minutes: 30
       steps:
         - uses: vishesh21105/greenlight@main
           with:
             llm-api-key: ${{ secrets.LLM_API_KEY }}
   ```

2. **Add the AI key as a secret:** repository **Settings → Secrets and variables → Actions → New repository
   secret**, name `LLM_API_KEY`. Without it GreenLight still reproduces and diagnoses, but does not fix.

3. **Allow pull requests from Actions:** **Settings → Actions → General → Workflow permissions** → tick
   *Allow GitHub Actions to create and approve pull requests*. Without it, GreenLight pushes the verified fix
   branch and posts a one-click "open pull request" link instead.

4. Optional: add a `.greenlight.yml` with your house rules (see the main README).

## What happens when CI fails

1. The `workflow_run` event starts GreenLight on a fresh Actions runner.
2. GreenLight downloads the failed job's log with the run's token, trims it, and classifies the failure.
3. It clones the repository and reproduces the failure inside Docker on the runner (no network while tests
   run, no secrets inside, unprivileged user, resource limits).
4. The AI agent investigates and proposes a fix; GreenLight verifies it in a fresh copy (the failing tests,
   then the whole suite).
5. A draft pull request appears, plus a comment on the broken commit. The run page shows a summary, and an
   HTML incident report is attached as an artifact.

## Things to know

- **Fork pull requests are ignored** (their code is untrusted). So are GreenLight's own `greenlight/*` branches.
- **CI does not run on GreenLight's pull request automatically**, because GitHub does not start workflows for
  changes made with the Actions token. GreenLight already ran the full suite in its sandbox; push any commit
  to the branch, or close and reopen the PR, to run your CI too.
- **The `.greenlight.yml` rules are read from the last green commit**, never from the failing one.
- **Free AI tiers are small.** The free Gemini tier allows about 20 requests a day on Gemini 3.5 Flash, and
  one incident uses 5 to 10. GreenLight automatically moves to the fallback models when a daily quota is used
  up. For a live demo, keep calls in reserve (the daily benchmark is limited to 45) or enable billing.
- Inputs you can change: `llm-model`, `llm-fallback-models`, `llm-base-url` (any OpenAI-compatible endpoint),
  `llm-min-interval-seconds`, `llm-reasoning-effort`, `python-version`.
