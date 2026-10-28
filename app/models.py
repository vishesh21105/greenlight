"""Database tables. Every table hangs off an Incident (one red build)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, JSON, Text
from sqlmodel import Field, SQLModel


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IncidentState(str, Enum):
    received = "received"
    log_trimmed = "log_trimmed"
    classified = "classified"
    reproducing = "reproducing"
    investigating = "investigating"
    verifying = "verifying"
    # Final states: every incident must end in exactly one of these.
    fixed = "fixed"                          # verified draft PR opened
    diagnosed_no_fix = "diagnosed_no_fix"    # comment posted, no safe patch
    flaky = "flaky"                          # passed on re-run
    not_reproduced = "not_reproduced"        # could not make it fail in the sandbox
    infra = "infra"                          # runner/network problem, re-run suggested
    gave_up = "gave_up"                      # agent stopped without a patch
    budget_exhausted = "budget_exhausted"
    error = "error"                          # GreenLight itself failed


FINAL_STATES = {
    IncidentState.fixed, IncidentState.diagnosed_no_fix, IncidentState.flaky,
    IncidentState.not_reproduced, IncidentState.infra, IncidentState.gave_up,
    IncidentState.budget_exhausted, IncidentState.error,
}


class Category(str, Enum):
    code_bug = "code_bug"
    broken_test = "broken_test"
    dependency = "dependency"
    config_env = "config_env"
    flaky = "flaky"
    infra = "infra"
    unknown = "unknown"


class Repository(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    full_name: str = Field(index=True, unique=True)       # e.g. "greenlight-demo/tinyshop"
    github_installation_id: Optional[int] = None
    clone_url: str = ""                                    # https URL, or a local path in dev mode
    default_branch: str = "main"
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Incident(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    repository_id: str = Field(foreign_key="repository.id", index=True)
    run_id: Optional[int] = Field(default=None, index=True)   # GitHub workflow run id
    branch: str = ""
    failing_sha: str = ""
    last_green_sha: Optional[str] = None
    state: str = Field(default=IncidentState.received.value, index=True)
    category: Optional[str] = None
    confidence: Optional[float] = None
    summary: Optional[str] = Field(default=None, sa_column=Column(Text))
    raw_log_lines: Optional[int] = None
    trimmed_log: Optional[str] = Field(default=None, sa_column=Column(Text))
    trimmed_log_lines: Optional[int] = None
    failing_tests: list = Field(default_factory=list, sa_column=Column(JSON))
    pr_url: Optional[str] = None
    total_cost_usd: float = 0.0
    started_at: datetime = Field(default_factory=utcnow, index=True)
    resolved_at: Optional[datetime] = None


class Job(SQLModel, table=True):
    """The to-do list the worker thread reads. Replaces a separate queue."""
    id: str = Field(default_factory=new_id, primary_key=True)
    incident_id: str = Field(foreign_key="incident.id", index=True)
    step: str = "process_incident"
    status: str = Field(default="waiting", index=True)   # waiting / running / done / failed
    attempts: int = 0
    last_error: Optional[str] = Field(default=None, sa_column=Column(Text))
    run_after: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)


class IncidentEvent(SQLModel, table=True):
    """The timeline: every step, tool call and state change."""
    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: str = Field(foreign_key="incident.id", index=True)
    seq_no: int = Field(index=True)
    type: str                        # state / step / tool_call / tool_result / llm / warning / error
    message: str = Field(default="", sa_column=Column(Text))
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)


class Patch(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    incident_id: str = Field(foreign_key="incident.id", index=True)
    diff_text: str = Field(default="", sa_column=Column(Text))
    patch_hash: str = ""
    rationale: str = Field(default="", sa_column=Column(Text))
    files_changed: int = 0
    lines_changed: int = 0
    touches_tests: bool = False
    valid: bool = False
    validation_errors: list = Field(default_factory=list, sa_column=Column(JSON))
    verified: bool = False
    pr_url: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class SandboxRun(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    incident_id: str = Field(foreign_key="incident.id", index=True)
    patch_id: Optional[str] = None
    purpose: str                     # reproduce / rerun / verify_failing / verify_full
    command: list = Field(default_factory=list, sa_column=Column(JSON))
    exit_code: Optional[int] = None
    duration_ms: int = 0
    timed_out: bool = False
    output_excerpt: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow)


class LLMCall(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    incident_id: Optional[str] = Field(default=None, index=True)
    purpose: str = ""                # classify / agent
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    created_at: datetime = Field(default_factory=utcnow)
