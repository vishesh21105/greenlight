"""Numbers for the metrics page and the benchmark report."""
from __future__ import annotations

import statistics
from collections import Counter
from datetime import timedelta
from typing import Optional

from sqlmodel import select

from app.db import session_scope
from app.models import FINAL_STATES, Incident, IncidentState, utcnow

# Incidents where a code fix was a reasonable expectation.
NOT_FIXABLE = {IncidentState.infra.value, IncidentState.flaky.value, IncidentState.not_reproduced.value,
               IncidentState.error.value}


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def compute_metrics(repository_id: Optional[str] = None, days: int = 14) -> dict:
    with session_scope() as s:
        q = select(Incident)
        if repository_id:
            q = q.where(Incident.repository_id == repository_id)
        incidents = s.exec(q).all()

    final_values = {st.value for st in FINAL_STATES}
    finished = [i for i in incidents if i.state in final_values]
    fixed = [i for i in finished if i.state == IncidentState.fixed.value]
    fixable = [i for i in finished if i.state not in NOT_FIXABLE]
    fix_minutes = [(i.resolved_at - i.started_at).total_seconds() / 60 for i in fixed
                   if i.resolved_at and i.started_at]
    total_cost = sum(i.total_cost_usd or 0 for i in incidents)
    reductions = [i.trimmed_log_lines / i.raw_log_lines for i in incidents if i.raw_log_lines and i.trimmed_log_lines]

    today = utcnow().date()
    day_labels = [(today - timedelta(days=d)) for d in range(days - 1, -1, -1)]
    per_day = Counter(i.started_at.date() for i in incidents if i.started_at)
    fixed_day = Counter(i.started_at.date() for i in fixed if i.started_at)

    return {
        "incidents": len(incidents),
        "finished": len(finished),
        "fixed": len(fixed),
        "fixable": len(fixable),
        "fix_rate": (len(fixed) / len(fixable)) if fixable else None,
        "median_minutes_to_fix": statistics.median(fix_minutes) if fix_minutes else None,
        "p90_minutes_to_fix": _percentile(fix_minutes, 0.9),
        "total_cost_usd": total_cost,
        "cost_per_fix_usd": (total_cost / len(fixed)) if fixed else None,
        "avg_log_kept": statistics.mean(reductions) if reductions else None,
        "by_state": dict(Counter(i.state for i in incidents)),
        "by_category": dict(Counter(i.category or "unclassified" for i in incidents)),
        "series": {
            "labels": [d.strftime("%d %b") for d in day_labels],
            "incidents": [per_day.get(d, 0) for d in day_labels],
            "fixed": [fixed_day.get(d, 0) for d in day_labels],
        },
    }
