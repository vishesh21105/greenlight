import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import session_scope
from app.jobs import claim_next_job, recover_interrupted_jobs, run_one
from app.main import app
from app.models import Incident, Job


def payload(conclusion="failure", fork=False, branch="main", run_id=101):
    return {
        "action": "completed",
        "workflow_run": {"id": run_id, "name": "CI", "conclusion": conclusion, "head_branch": branch,
                         "head_sha": "a" * 40, "html_url": "https://github.com/acme/shop/actions/runs/101",
                         "head_repository": {"full_name": "someone/shop" if fork else "acme/shop"}},
        "repository": {"full_name": "acme/shop", "clone_url": "https://github.com/acme/shop.git",
                       "default_branch": "main"},
        "installation": {"id": 42},
    }


def post(client, body: dict, event="workflow_run", secret="test-secret"):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/v1/webhooks/github", content=raw,
                       headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig, "Content-Type": "application/json"})


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_rejects_bad_signature(client):
    assert post(client, payload(), secret="wrong").status_code == 401
    r = client.post("/api/v1/webhooks/github", content=b"{}", headers={"X-GitHub-Event": "workflow_run"})
    assert r.status_code == 401


def test_ping(client):
    assert post(client, {"zen": "hi"}, event="ping").json()["message"] == "pong"


@pytest.mark.parametrize("kwargs,reason", [
    ({"conclusion": "success"}, "conclusion"),
    ({"fork": True}, "fork"),
    ({"branch": "greenlight/fix-123"}, "own fix branch"),
])
def test_ignores_what_it_should(client, kwargs, reason):
    r = post(client, payload(**kwargs)).json()
    assert r["action"] == "ignored" and reason in r["reason"]


def test_failed_run_creates_one_incident_and_job(client):
    first = post(client, payload()).json()
    assert first["action"] == "created"
    again = post(client, payload()).json()
    assert again["action"] == "duplicate" and again["incident_id"] == first["incident_id"]
    with session_scope() as s:
        assert len(s.exec(select(Incident)).all()) == 1
        job = s.exec(select(Job)).one()
    assert job.status == "waiting"

    inc = client.get(f"/api/v1/incidents/{first['incident_id']}").json()
    assert inc["repository"] == "acme/shop" and inc["state"] == "received"
    events = client.get(f"/api/v1/incidents/{first['incident_id']}/events?after=0").json()
    assert events["events"][0]["message"].startswith("Received a failed run")


def test_pages_render(client):
    assert "No broken builds yet" in client.get("/").text
    iid = post(client, payload()).json()["incident_id"]
    assert "acme/shop" in client.get("/").text
    page = client.get(f"/incidents/{iid}")
    assert page.status_code == 200 and "Timeline" in page.text
    assert client.get("/metrics").status_code == 200
    assert client.get("/incidents/nope").status_code == 404


def test_dashboard_password(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("any", "s3cret")).status_code == 200
    assert client.get("/api/v1/health").status_code == 200          # stays open
    assert post(client, {"zen": "x"}, event="ping").status_code == 200


def test_retry_needs_admin_token(client, monkeypatch):
    iid = post(client, payload()).json()["incident_id"]
    assert client.post(f"/api/v1/incidents/{iid}/retry").status_code == 403
    monkeypatch.setenv("ADMIN_TOKEN", "t0ken")
    assert client.post(f"/api/v1/incidents/{iid}/retry", headers={"X-Admin-Token": "t0ken"}).status_code == 200


def test_worker_marks_incident_error_if_handler_leaves_it_open(client):
    iid = post(client, payload()).json()["incident_id"]
    assert run_one(lambda incident_id: None)          # handler that does nothing
    with session_scope() as s:
        assert s.get(Incident, iid).state == "error"


def test_interrupted_jobs_are_resumed(client):
    post(client, payload())
    job = claim_next_job()
    assert job.status == "running"
    assert recover_interrupted_jobs() == 1
    with session_scope() as s:
        assert s.get(Job, job.id).status == "waiting"
