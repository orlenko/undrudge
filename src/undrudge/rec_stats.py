"""Durable, read-only aggregate recommendation statistics."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import recommend

SCOPE_ORDER = ("daily", "weekly")


@dataclass(frozen=True)
class RecommendationStats:
    total: int
    by_status: dict[str, int]
    by_scope: dict[str, int]


def recommendation_stats(conn: sqlite3.Connection) -> RecommendationStats:
    """Count recommendations by durable status and analysis scope."""
    rows = conn.execute(
        "SELECT status, scope, COUNT(*) AS count FROM recommendations "
        "GROUP BY status, scope"
    ).fetchall()
    by_status = {status: 0 for status in recommend.STATUS_ORDER}
    by_scope = {scope: 0 for scope in SCOPE_ORDER}
    total = 0
    for row in rows:
        status = str(row["status"])
        scope = str(row["scope"])
        count = int(row["count"])
        total += count
        by_status[status] = by_status.get(status, 0) + count
        by_scope[scope] = by_scope.get(scope, 0) + count

    extra_statuses = sorted(set(by_status) - set(recommend.STATUS_ORDER))
    extra_scopes = sorted(set(by_scope) - set(SCOPE_ORDER))
    return RecommendationStats(
        total=total,
        by_status={
            status: by_status[status]
            for status in (*recommend.STATUS_ORDER, *extra_statuses)
        },
        by_scope={
            scope: by_scope[scope]
            for scope in (*SCOPE_ORDER, *extra_scopes)
        },
    )
