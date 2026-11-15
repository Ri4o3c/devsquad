"""Append-only trace recorder.

Agent and tool events use this storage without changing the database schema.
``record`` writes one JSON object and assigns a per-task sequence number
starting at 1. It does not call a model.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

from devsquad.db import connect


@dataclass(frozen=True)
class TraceEvent:
    """One stored trace row. ``payload`` is already decoded JSON."""

    id: str
    task_id: str
    seq: int
    event_type: str
    payload: dict
    created_at: str


_DISPLAY_EVENTS = frozenset({
    "run.started", "run.retrieval", "run.plan", "run.plan.error",
    "model.started", "model.completed", "model.error",
    "tool.started", "tool.completed", "tool.rejected", "tool.error", "tool.approval", "loop.result",
})
_DISPLAY_NUMBERS = frozenset({
    "step", "steps", "cost_usd", "duration_ms", "hit_count", "returncode",
    "input_tokens", "output_tokens", "retrieval_failed", "planner_enabled", "timed_out", "approved",
})
_DISPLAY_STATUS = frozenset({
    "completed", "step_limit", "cost_limit", "denied", "ask_human", "error",
})
_DISPLAY_REASON = frozenset({
    "unknown_tool", "permission_error", "permission_denied", "ask_human",
    "invalid_decision", "sandbox_denied", "execution_failed",
})


def trace_summary(event: TraceEvent) -> str:
    """Format operational metadata without displaying legacy prompt or plan text."""
    if event.event_type not in _DISPLAY_EVENTS:
        return f"{event.seq}\tlegacy_event"
    details = {}
    for key in _DISPLAY_NUMBERS:
        value = event.payload.get(key)
        if type(value) in (bool, int) or (type(value) is float and isfinite(value)):
            details[key] = value
    for key, choices in (("status", _DISPLAY_STATUS), ("reason", _DISPLAY_REASON)):
        value = event.payload.get(key)
        if isinstance(value, str) and value in choices:
            details[key] = value
    return f"{event.seq}\t{event.event_type}\t{json.dumps(details, sort_keys=True)}"


class TraceRecorder:
    """Write and list trace events for a task that already exists."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def record(
        self,
        task_id: str,
        event_type: str,
        payload: dict | None = None,
    ) -> TraceEvent:
        """Append one event. Returns the stored row.

        ``event_type`` is a short name such as ``model_turn``, ``tool_call``,
        ``tool_result``, ``limit``, or ``note``. The set is not enforced, so
        a later loop can add types without a migration.

        Raises:
            ValueError: ``task_id`` or ``event_type`` is blank, or the task
                does not exist.
            TypeError: ``payload`` is not a dict, or it is not JSON-serializable.
        """
        if not task_id or not str(task_id).strip():
            raise ValueError("task_id must not be empty")
        if not event_type or not str(event_type).strip():
            raise ValueError("event_type must not be empty")
        body = {} if payload is None else payload
        if not isinstance(body, dict):
            raise TypeError("payload must be a dict or None")
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)

        event_id = uuid.uuid4().hex
        created_at = datetime.now(UTC).isoformat()
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            found = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if found is None:
                raise ValueError(f"unknown task_id: {task_id}")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM traces WHERE task_id = ?",
                (task_id,),
            ).fetchone()["seq"]
            conn.execute(
                """
                INSERT INTO traces (id, task_id, seq, event_type, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, task_id, seq, event_type, encoded, created_at),
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return TraceEvent(
            id=event_id,
            task_id=task_id,
            seq=int(seq),
            event_type=event_type,
            payload=json.loads(encoded),
            created_at=created_at,
        )

    def list_events(self, task_id: str) -> list[TraceEvent]:
        """Return events for ``task_id`` in sequence order. Missing tasks yield ``[]``."""
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT id, task_id, seq, event_type, payload, created_at
                FROM traces
                WHERE task_id = ?
                ORDER BY seq ASC
                """,
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            TraceEvent(
                id=row["id"],
                task_id=row["task_id"],
                seq=int(row["seq"]),
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
