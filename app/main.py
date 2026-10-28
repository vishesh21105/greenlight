"""The GreenLight web app: GitHub webhook, JSON API and dashboard pages, plus the worker thread.

Start it with:  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app import config
from app.db import init_db, session_scope
from app.jobs import Worker, enqueue
from app.metrics import compute_metrics
from app.models import FINAL_STATES, Incident, IncidentEvent, LLMCall, Patch, Repository, SandboxRun
from app.webhooks import handle_workflow_run, verify_signature

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
FINAL = {s.value for s in FINAL_STATES}


def build_services():
    from app.github_client import get_code_host
    from app.llm import get_llm
    from app.pipeline import Services
    from app.sandbox import get_sandbox
    return Services(code_host=get_code_host(os.environ.get("CODE_HOST", "github")), sandbox=get_sandbox(),
                    llm=get_llm(), dashboard_base_url=os.environ.get("PUBLIC_URL", "http://localhost:8000"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker = None
    if config.settings.start_worker:
        from app.pipeline import process_incident
        services = build_services()
        try:
            services.sandbox.cleanup_leftovers()
        except Exception:
            pass
        worker = Worker(lambda incident_id: process_incident(incident_id, services))
        worker.start()
    yield
    if worker:
        worker.stop()


app = FastAPI(title="GreenLight", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# --------------------------------------------------------------------------- optional password

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """If DASHBOARD_PASSWORD is set, the dashboard and API need it (webhook and health stay open)."""
    password = os.environ.get("DASHBOARD_PASSWORD")
    path = request.url.path
    if password and not (path.startswith("/api/v1/webhooks") or path == "/api/v1/health"):
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(given, password)
            except Exception:
                ok = False
        if not ok:
            return HTMLResponse("Authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="GreenLight"'})
    return await call_next(request)


# --------------------------------------------------------------------------- serialising

def _incident_dict(inc: Incident, repo_name: str = "") -> dict:
    duration = None
    if inc.resolved_at and inc.started_at:
        duration = (inc.resolved_at - inc.started_at).total_seconds()
    return {
        "id": inc.id, "repository": repo_name, "run_id": inc.run_id, "branch": inc.branch,
        "failing_sha": inc.failing_sha, "last_green_sha": inc.last_green_sha, "state": inc.state,
        "final": inc.state in FINAL, "category": inc.category, "confidence": inc.confidence,
        "summary": inc.summary, "failing_tests": inc.failing_tests, "pr_url": inc.pr_url,
        "raw_log_lines": inc.raw_log_lines, "trimmed_log_lines": inc.trimmed_log_lines,
        "total_cost_usd": round(inc.total_cost_usd or 0, 4),
        "started_at": inc.started_at.isoformat() if inc.started_at else None,
        "resolved_at": inc.resolved_at.isoformat() if inc.resolved_at else None,
        "duration_seconds": duration,
    }


def _event_dict(e: IncidentEvent) -> dict:
    return {"seq_no": e.seq_no, "type": e.type, "message": e.message, "payload": e.payload,
            "created_at": e.created_at.isoformat() if e.created_at else None}


def _load_incident(incident_id: str):
    with session_scope() as s:
        inc = s.get(Incident, incident_id)
        if inc is None:
            raise HTTPException(404, "incident not found")
        repo = s.get(Repository, inc.repository_id)
        return inc, repo


# --------------------------------------------------------------------------- webhook + API

@app.post("/api/v1/webhooks/github")
async def github_webhook(request: Request):
    secret = config.settings.github_webhook_secret
    if not secret:
        raise HTTPException(503, "GITHUB_WEBHOOK_SECRET is not configured")
    body = await request.body()
    if not verify_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "bad signature")
    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"ok": True, "message": "pong"}
    if event != "workflow_run":
        return {"ok": True, "action": "ignored", "reason": f"event {event} not handled"}
    decision = handle_workflow_run(json.loads(body))
    return {"ok": True, "action": decision.action, "reason": decision.reason, "incident_id": decision.incident_id}


@app.get("/api/v1/health")
def health():
    return {"ok": True}


@app.get("/api/v1/repositories")
def list_repositories():
    with session_scope() as s:
        return [{"id": r.id, "full_name": r.full_name, "active": r.active, "default_branch": r.default_branch}
                for r in s.exec(select(Repository)).all()]


@app.get("/api/v1/incidents")
def list_incidents(state: Optional[str] = None, category: Optional[str] = None, limit: int = 100):
    with session_scope() as s:
        q = select(Incident, Repository).join(Repository).order_by(Incident.started_at.desc()).limit(min(limit, 500))
        if state:
            q = q.where(Incident.state == state)
        if category:
            q = q.where(Incident.category == category)
        return [_incident_dict(i, r.full_name) for i, r in s.exec(q).all()]


@app.get("/api/v1/incidents/{incident_id}")
def get_incident_api(incident_id: str):
    inc, repo = _load_incident(incident_id)
    return _incident_dict(inc, repo.full_name)


@app.get("/api/v1/incidents/{incident_id}/events")
def get_events(incident_id: str, after: int = 0):
    inc, _ = _load_incident(incident_id)
    with session_scope() as s:
        events = s.exec(select(IncidentEvent).where(IncidentEvent.incident_id == incident_id,
                                                    IncidentEvent.seq_no > after)
                        .order_by(IncidentEvent.seq_no)).all()
    return {"state": inc.state, "final": inc.state in FINAL, "events": [_event_dict(e) for e in events]}


@app.get("/api/v1/incidents/{incident_id}/patches")
def get_patches(incident_id: str):
    _load_incident(incident_id)
    with session_scope() as s:
        rows = s.exec(select(Patch).where(Patch.incident_id == incident_id).order_by(Patch.created_at)).all()
        return [{"id": p.id, "hash": p.patch_hash, "diff": p.diff_text, "rationale": p.rationale,
                 "files_changed": p.files_changed, "lines_changed": p.lines_changed,
                 "touches_tests": p.touches_tests, "verified": p.verified, "pr_url": p.pr_url} for p in rows]


@app.post("/api/v1/incidents/{incident_id}/retry")
def retry_incident(incident_id: str, request: Request):
    token = os.environ.get("ADMIN_TOKEN")
    if not token or not secrets.compare_digest(request.headers.get("X-Admin-Token", ""), token):
        raise HTTPException(403, "retry needs the X-Admin-Token header (set ADMIN_TOKEN on the server)")
    _load_incident(incident_id)
    return {"ok": True, "job_id": enqueue(incident_id)}


@app.get("/api/v1/metrics")
def metrics_api():
    return compute_metrics()


# --------------------------------------------------------------------------- pages

def diff_lines(diff: str) -> list[tuple[str, str]]:
    out = []
    for line in (diff or "").splitlines():
        if line.startswith(("+++", "---", "diff --git", "new file mode")):
            cls = "meta"
        elif line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        else:
            cls = "ctx"
        out.append((cls, line))
    return out


templates.env.filters["diff_lines"] = diff_lines


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f} min"


templates.env.filters["duration"] = _fmt_duration
templates.env.filters["minutes"] = lambda m: _fmt_duration(m * 60 if m is not None else None)

STATE_LABELS = {
    "received": "Received", "log_trimmed": "Reading log", "classified": "Classified",
    "reproducing": "Reproducing", "investigating": "Investigating", "verifying": "Verifying fix",
    "fixed": "Fixed", "diagnosed_no_fix": "Diagnosed", "flaky": "Flaky test",
    "not_reproduced": "Not reproduced", "infra": "Infrastructure", "gave_up": "No safe fix",
    "budget_exhausted": "Budget hit", "error": "GreenLight error",
}
templates.env.globals["STATE_LABELS"] = STATE_LABELS


@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    incidents = list_incidents(limit=200)
    return templates.TemplateResponse(request, "index.html", {"incidents": incidents, "metrics": compute_metrics()})


@app.get("/incidents/{incident_id}", response_class=HTMLResponse)
def incident_page(request: Request, incident_id: str):
    inc, repo = _load_incident(incident_id)
    with session_scope() as s:
        events = s.exec(select(IncidentEvent).where(IncidentEvent.incident_id == incident_id)
                        .order_by(IncidentEvent.seq_no)).all()
        runs = s.exec(select(SandboxRun).where(SandboxRun.incident_id == incident_id)
                      .order_by(SandboxRun.created_at)).all()
        calls = s.exec(select(LLMCall).where(LLMCall.incident_id == incident_id)).all()
    usage = {"calls": len(calls), "input_tokens": sum(c.input_tokens for c in calls),
             "output_tokens": sum(c.output_tokens for c in calls)}
    return templates.TemplateResponse(request, "incident.html", {
        "inc": _incident_dict(inc, repo.full_name), "events": [_event_dict(e) for e in events],
        "patches": get_patches(incident_id), "runs": runs, "usage": usage,
        "trimmed_log": inc.trimmed_log or "",
    })


@app.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request):
    return templates.TemplateResponse(request, "metrics.html", {"m": compute_metrics()})
