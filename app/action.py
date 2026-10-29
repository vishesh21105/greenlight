"""Run GreenLight inside GitHub Actions: no server, no GitHub App, no ngrok.

A small workflow in the watched repository starts this when its CI workflow fails
(the `workflow_run` event). GreenLight then uses the run's own GITHUB_TOKEN to read the
logs, reproduces and verifies inside Docker on the Actions runner, and opens the
draft pull request. See action.yml and docs/github-actions-mode.md.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _annotate(level: str, title: str, message: str) -> None:
    """Print a GitHub Actions annotation (shows on the run page and in the checks API)."""
    message = message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")[:4000]
    print(f"::{level} title={title}::{message}", flush=True)


def _write_summary(incident, events, patches) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"## GreenLight: {incident.state.replace('_', ' ')}", ""]
    if incident.pr_url:
        lines += [f"**Fix:** {incident.pr_url}", ""]
    if incident.summary:
        lines += [incident.summary, ""]
    lines += ["| Time (UTC) | Step |", "| --- | --- |"]
    for e in events:
        msg = e.message.replace("|", "\\|").replace("\n", " ")[:300]
        lines.append(f"| {e.created_at.strftime('%H:%M:%S')} | {msg} |")
    for p in patches:
        lines += ["", f"### Patch ({'verified' if p.verified else 'not verified'})", "```diff", p.diff_text, "```"]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _save_report(incident_id: str, out_dir: Path) -> None:
    """Save the dashboard's incident page as a stand-alone HTML file (uploaded as an artifact)."""
    from fastapi.testclient import TestClient
    from app.main import app
    os.environ["START_WORKER"] = "0"
    with TestClient(app) as client:
        html = client.get(f"/incidents/{incident_id}").text
    css = (Path(__file__).parent / "static" / "style.css").read_text()
    html = html.replace('<link rel="stylesheet" href="/static/style.css">', f"<style>{css}</style>")
    (out_dir / "report.html").write_text(html)


def main() -> int:
    work = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "greenlight"
    work.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("GREENLIGHT_DATABASE_URL", f"sqlite:///{work / 'greenlight.db'}")
    os.environ.setdefault("GREENLIGHT_WORKSPACE", str(work / "workspaces"))
    os.environ.setdefault("SANDBOX_BACKEND", "docker")
    os.environ["START_WORKER"] = "0"

    from app import config
    config.reload_settings()
    from sqlmodel import select

    from app.db import init_db, session_scope
    from app.github_client import GitHubCodeHost
    from app.llm import get_llm
    from app.models import Incident, IncidentEvent, Patch
    from app.pipeline import Services, process_incident
    from app.sandbox import get_sandbox
    from app.webhooks import handle_workflow_run

    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_run":
        _annotate("warning", "GreenLight", "GreenLight runs on the workflow_run event; nothing to do.")
        return 0
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("INPUT_GITHUB_TOKEN")
    if not token:
        _annotate("error", "GreenLight", "No GITHUB_TOKEN available.")
        return 1

    init_db()
    decision = handle_workflow_run(event)
    if decision.action != "created":
        _annotate("notice", "GreenLight", f"Nothing to do: {decision.reason}.")
        return 0

    llm = get_llm()
    if llm is None:
        _annotate("warning", "GreenLight", "No AI key set (secret LLM_API_KEY): GreenLight will diagnose but not fix.")
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY')}" \
              f"/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"
    services = Services(code_host=GitHubCodeHost(token=token), sandbox=get_sandbox(), llm=llm, incident_link=run_url)
    process_incident(decision.incident_id, services)

    with session_scope() as s:
        inc = s.get(Incident, decision.incident_id)
        events = s.exec(select(IncidentEvent).where(IncidentEvent.incident_id == inc.id)
                        .order_by(IncidentEvent.seq_no)).all()
        patches = s.exec(select(Patch).where(Patch.incident_id == inc.id)).all()
    _write_summary(inc, events, patches)
    try:
        _save_report(inc.id, work)
    except Exception as exc:  # the report is a nice-to-have
        print(f"could not save the HTML report: {exc}", file=sys.stderr)

    level = "notice" if inc.state not in ("error",) else "error"
    detail = f"{inc.state} | category={inc.category} | cost=${inc.total_cost_usd or 0:.4f}"
    if inc.pr_url:
        detail += f" | fix={inc.pr_url}"
    _annotate(level, "GreenLight result", detail)
    # Short timeline in annotations too (readable through the checks API).
    tail = [e.message[:180] for e in events if e.type in ("state", "warning", "error")][-12:]
    _annotate("notice", "GreenLight timeline", "\n".join(tail))
    for e in events:
        if e.type == "error" and e.payload.get("traceback"):
            _annotate("error", "GreenLight traceback", e.payload["traceback"][-1800:])
    return 0 if inc.state != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
