# Connecting GreenLight to GitHub

GreenLight acts on GitHub as a **GitHub App**: a robot account with its own narrow permissions,
installed only on the repositories you choose. About 15 minutes.

## 1. Give GreenLight a public address

GitHub must be able to reach your laptop. During development use ngrok (free):

```bash
uvicorn app.main:app --port 8000      # in one terminal
ngrok http 8000                        # in another; copy the https://....ngrok-free.app address
```

## 2. Create the GitHub App

GitHub → your profile picture → **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**.

| Field | Value |
| --- | --- |
| Name | `greenlight-yourname` (must be unique on GitHub) |
| Homepage URL | your repository's URL |
| Webhook URL | `https://<your-ngrok-address>/api/v1/webhooks/github` |
| Webhook secret | a long random string, e.g. from `python -c "import secrets; print(secrets.token_hex(32))"` |

**Repository permissions** (everything else: No access):

| Permission | Access | Why |
| --- | --- | --- |
| Actions | Read-only | Download the failing run's logs |
| Contents | Read and write | Clone the code, push `greenlight/fix-*` branches, post commit comments |
| Pull requests | Read and write | Open the draft pull request |
| Metadata | Read-only | Required by GitHub |

**Subscribe to events:** tick **Workflow run**.

Create the app, then on its page:
- note the **App ID**,
- click **Generate a private key** and save the `.pem` file next to the project (it is in `.gitignore`).

> Honest note: "Contents: write" lets the app push to any branch. GreenLight's code only ever pushes to
> `greenlight/fix-*` branches and never merges, and you should also turn on **branch protection** for `main`
> (Settings → Branches) so nothing, including GreenLight, can push to it directly.

## 3. Install the app on the demo repository

1. Create a new GitHub repository (e.g. `tinyshop`) and push the contents of `demo/tinyshop` to it.
2. On the app's page → **Install App** → choose your account → **Only select repositories** → `tinyshop`.

## 4. Configure and run

```bash
cp .env.example .env
# fill in GITHUB_APP_ID, GITHUB_PRIVATE_KEY_PATH, GITHUB_WEBHOOK_SECRET,
# LLM_API_KEY, LLM_MODEL (and LLM_BASE_URL if not OpenAI), PUBLIC_URL=<ngrok address>
set -a; source .env; set +a
docker info                             # Docker must be running
uvicorn app.main:app --port 8000
```

In the app's settings, **Advanced → Recent Deliveries** shows every webhook GitHub sent and lets you
**Redeliver** one, which is handy while developing.

## 5. Break the build on purpose

In `tinyshop`, rename `compute_total` to `calc_total` in `tinyshop/pricing.py` and in
`tests/test_pricing.py`, but not in `tinyshop/checkout.py`. Commit and push. Within a minute:

- the red run appears on the dashboard,
- the timeline fills in live,
- a draft pull request `GreenLight: fix code bug in tinyshop/checkout.py` appears on GitHub.
