"""SQLite connection and small helpers used everywhere."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import event as sa_event
from sqlmodel import Session, SQLModel, create_engine, select

from app import config
from app.models import FINAL_STATES, Incident, IncidentEvent, IncidentState, utcnow

_engine = None
_engine_lock = threading.Lock()
_event_lock = threading.Lock()


def get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            url = config.settings.database_url
            connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
            _engine = create_engine(url, connect_args=connect_args)
            if url.startswith("sqlite"):
                @sa_event.listens_for(_engine, "connect")
                def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
                    # WAL mode lets the web pages read while the worker writes.
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA foreign_keys=ON")
                    cur.close()
        return _engine


def reset_engine() -> None:
    """Forget the current engine (used by tests that switch databases)."""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """One short transaction. Keep these short so SQLite never stays locked."""
    with Session(get_engine(), expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def add_event(incident_id: str, type_: str, message: str, payload: Optional[dict] = None) -> None:
    """Append one line to an incident's timeline."""
    with _event_lock, session_scope() as s:
        last = s.exec(
            select(IncidentEvent.seq_no)
            .where(IncidentEvent.incident_id == incident_id)
            .order_by(IncidentEvent.seq_no.desc())
        ).first()
        seq = (last or 0) + 1
        s.add(IncidentEvent(incident_id=incident_id, seq_no=seq, type=type_,
                            message=message, payload=payload or {}))


def set_state(incident_id: str, state: IncidentState, message: str = "", **fields) -> None:
    """Change an incident's state and record it on the timeline, in one place."""
    with session_scope() as s:
        inc = s.get(Incident, incident_id)
        if inc is None:
            raise ValueError(f"unknown incident {incident_id}")
        inc.state = state.value
        for key, value in fields.items():
            setattr(inc, key, value)
        if state in FINAL_STATES:
            inc.resolved_at = utcnow()
        s.add(inc)
    add_event(incident_id, "state", message or state.value, {"state": state.value})


def update_incident(incident_id: str, **fields) -> None:
    with session_scope() as s:
        inc = s.get(Incident, incident_id)
        for key, value in fields.items():
            setattr(inc, key, value)
        s.add(inc)


def get_incident(incident_id: str) -> Optional[Incident]:
    with session_scope() as s:
        return s.get(Incident, incident_id)
