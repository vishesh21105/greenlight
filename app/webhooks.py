"""GitHub webhooks: check the signature, decide whether to act, record an incident."""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

from sqlmodel import select

from app.db import add_event, session_scope
from app.jobs import enqueue
from app.models import Incident, Repository

GREENLIGHT_BRANCH_PREFIX = "greenlight/"


def verify_signature(secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    """GitHub signs every delivery with the shared secret (HMAC-SHA256)."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@dataclass
class WebhookDecision:
    action: str            # created / ignored / duplicate
    reason: str
    incident_id: Optional[str] = None


def handle_workflow_run(payload: dict) -> WebhookDecision:
    run = payload.get("workflow_run") or {}
    repo_info = payload.get("repository") or {}
    if payload.get("action") != "completed":
        return WebhookDecision("ignored", "run not completed yet")
    if run.get("conclusion") != "failure":
        return WebhookDecision("ignored", f"conclusion is {run.get('conclusion')}")

    full_name = repo_info.get("full_name", "")
    head_repo = (run.get("head_repository") or {}).get("full_name", full_name)
    if head_repo != full_name:
        # Code from a fork is code from a stranger: never run it (v1 rule).
        return WebhookDecision("ignored", "run comes from a fork")
    branch = run.get("head_branch") or ""
    if branch.startswith(GREENLIGHT_BRANCH_PREFIX):
        return WebhookDecision("ignored", "failure on GreenLight's own fix branch")

    run_id = run.get("id")
    installation_id = (payload.get("installation") or {}).get("id")
    with session_scope() as s:
        repo = s.exec(select(Repository).where(Repository.full_name == full_name)).first()
        if repo is None:
            repo = Repository(full_name=full_name, github_installation_id=installation_id,
                              clone_url=repo_info.get("clone_url", ""),
                              default_branch=repo_info.get("default_branch", "main"))
            s.add(repo)
            s.flush()
        elif installation_id and repo.github_installation_id != installation_id:
            repo.github_installation_id = installation_id
            s.add(repo)
        if not repo.active:
            return WebhookDecision("ignored", "repository is paused")
        existing = s.exec(select(Incident).where(Incident.run_id == run_id)).first()
        if existing:
            return WebhookDecision("duplicate", "incident already exists", existing.id)
        inc = Incident(repository_id=repo.id, run_id=run_id, branch=branch, failing_sha=run.get("head_sha", ""))
        s.add(inc)

    add_event(inc.id, "state", f"Received a failed run of '{run.get('name', 'CI')}' on {branch} "
                               f"({inc.failing_sha[:8]})", {"state": "received", "run_url": run.get("html_url")})
    enqueue(inc.id)
    return WebhookDecision("created", "incident created", inc.id)
