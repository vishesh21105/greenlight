"""Background work without Redis: a jobs table plus one worker thread.

The webhook only writes a "to-do" row and returns at once. The worker thread picks
rows up one at a time. Because jobs live in the database, nothing is lost if the
app restarts: unfinished jobs are picked up again on start.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import timedelta
from typing import Callable, Optional

from sqlmodel import select

from app import config
from app.db import add_event, session_scope, set_state
from app.models import FINAL_STATES, Incident, IncidentState, Job, utcnow

MAX_ATTEMPTS = 2


def enqueue(incident_id: str, step: str = "process_incident") -> str:
    with session_scope() as s:
        job = Job(incident_id=incident_id, step=step)
        s.add(job)
    return job.id


def recover_interrupted_jobs() -> int:
    """Jobs left 'running' by a crash go back to 'waiting' (or fail after too many tries)."""
    count = 0
    with session_scope() as s:
        for job in s.exec(select(Job).where(Job.status == "running")).all():
            if job.attempts >= MAX_ATTEMPTS:
                job.status = "failed"
                job.last_error = "interrupted too many times"
            else:
                job.status = "waiting"
            s.add(job)
            count += 1
    return count


def claim_next_job() -> Optional[Job]:
    with session_scope() as s:
        job = s.exec(
            select(Job).where(Job.status == "waiting", Job.run_after <= utcnow()).order_by(Job.created_at)
        ).first()
        if job is None:
            return None
        job.status = "running"
        job.attempts += 1
        s.add(job)
        return job


def finish_job(job_id: str, error: Optional[str] = None) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if error and job.attempts < MAX_ATTEMPTS:
            job.status = "waiting"
            job.run_after = utcnow() + timedelta(seconds=30)
        else:
            job.status = "failed" if error else "done"
        job.last_error = error
        s.add(job)


def run_one(handler: Callable[[str], None]) -> bool:
    """Process one waiting job. Returns False when there was nothing to do."""
    job = claim_next_job()
    if job is None:
        return False
    try:
        handler(job.incident_id)
        finish_job(job.id)
    except Exception as exc:
        finish_job(job.id, f"{exc.__class__.__name__}: {exc}")
        add_event(job.incident_id, "error", f"Worker error: {exc}", {"traceback": traceback.format_exc()[-3000:]})
    # Safety net: the incident must end in a final state.
    with session_scope() as s:
        inc = s.get(Incident, job.incident_id)
        still_open = inc is not None and IncidentState(inc.state) not in FINAL_STATES
        job_row = s.get(Job, job.id)
        retrying = job_row.status == "waiting"
    if still_open and not retrying:
        set_state(job.incident_id, IncidentState.error, "Stopped without reaching a final state.")
    return True


class Worker(threading.Thread):
    def __init__(self, handler: Callable[[str], None]) -> None:
        super().__init__(name="greenlight-worker", daemon=True)
        self.handler = handler
        self._stop_event = threading.Event()

    def run(self) -> None:
        recover_interrupted_jobs()
        while not self._stop_event.is_set():
            try:
                worked = run_one(self.handler)
            except Exception:
                traceback.print_exc()
                worked = False
            if not worked:
                self._stop_event.wait(config.settings.worker_poll_seconds)

    def stop(self) -> None:
        self._stop_event.set()
