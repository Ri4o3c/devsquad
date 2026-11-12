"""Trace recorder tests. No model calls."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from devsquad.db import connect, init_db
from devsquad.tasks import create_task
from devsquad.traces import TraceRecorder


@pytest.fixture
def task_id(git_repo, tmp_path):
    task = create_task(
        repo=git_repo,
        title="trace me",
        workspaces_dir=tmp_path / "ws",
        db_path=tmp_path / "devsquad.db",
    )
    return task.id, tmp_path / "devsquad.db"


def test_schema_columns(tmp_path) -> None:
    db = tmp_path / "schema.db"
    init_db(db)
    conn = sqlite3.connect(db)
    task_cols = [row[1] for row in conn.execute("PRAGMA table_info(tasks)")]
    trace_cols = [row[1] for row in conn.execute("PRAGMA table_info(traces)")]
    conn.close()
    assert task_cols[:6] == [
        "id",
        "repo_path",
        "branch",
        "worktree_path",
        "status",
        "created_at",
    ]
    assert {"title", "base_commit"} <= set(task_cols)
    assert trace_cols == [
        "id",
        "task_id",
        "seq",
        "event_type",
        "payload",
        "created_at",
    ]


def test_record_assigns_sequence_and_roundtrips_json(task_id) -> None:
    tid, db = task_id
    recorder = TraceRecorder(db)
    first = recorder.record(tid, "note", {"text": "你好", "n": 1})
    second = recorder.record(tid, "tool_call", {"name": "read_file", "ok": True})
    assert first.seq == 1
    assert second.seq == 2
    assert first.payload == {"text": "你好", "n": 1}
    datetime.fromisoformat(first.created_at)

    events = recorder.list_events(tid)
    assert [event.seq for event in events] == [1, 2]
    assert events[1].event_type == "tool_call"
    assert events[1].payload["name"] == "read_file"

    conn = connect(db)
    stored = conn.execute("SELECT payload FROM traces WHERE id = ?", (first.id,)).fetchone()
    conn.close()
    assert json.loads(stored["payload"])["text"] == "你好"


def test_sequences_are_per_task(git_repo, tmp_path) -> None:
    db = tmp_path / "devsquad.db"
    workspaces = tmp_path / "ws"
    first = create_task(repo=git_repo, title="a", workspaces_dir=workspaces, db_path=db)
    second = create_task(repo=git_repo, title="b", workspaces_dir=workspaces, db_path=db)
    recorder = TraceRecorder(db)
    assert recorder.record(first.id, "note").seq == 1
    assert recorder.record(second.id, "note").seq == 1
    assert recorder.record(first.id, "note").seq == 2
    assert recorder.list_events(second.id)[0].task_id == second.id


def test_missing_payload_is_an_empty_object(task_id) -> None:
    tid, db = task_id
    event = TraceRecorder(db).record(tid, "ping")
    assert event.payload == {}


def test_unknown_task_is_rejected(tmp_path) -> None:
    recorder = TraceRecorder(tmp_path / "devsquad.db")
    with pytest.raises(ValueError, match="unknown task_id"):
        recorder.record("does-not-exist", "note", {"text": "x"})


def test_blank_event_type_and_non_dict_payload_are_rejected(task_id) -> None:
    tid, db = task_id
    recorder = TraceRecorder(db)
    with pytest.raises(ValueError):
        recorder.record(tid, "  ")
    with pytest.raises(TypeError):
        recorder.record(tid, "note", ["not", "a", "dict"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        recorder.record(tid, "note", {"bad": {1, 2}})  # type: ignore[dict-item]


def test_list_unknown_task_is_empty(tmp_path) -> None:
    assert TraceRecorder(tmp_path / "devsquad.db").list_events("missing") == []
