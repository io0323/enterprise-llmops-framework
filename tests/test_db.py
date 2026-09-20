"""db/ の検証(Step 1-1)。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llmops.db.connection import MEMORY, connect, init_schema, session, table_names
from llmops.db.repository import Repository
from llmops.models import SpanEnd, SpanStart, TraceStart

EXPECTED_TABLES = {
    "traces",
    "spans",
    "prompt_versions",
    "prompt_deployments",
    "prompt_transitions",
    "model_versions",
    "budgets",
    "cost_daily",
    "eval_suites",
    "eval_cases",
    "eval_runs",
    "eval_results",
    "asset_catalog",
    "audit_logs",
}


@pytest.fixture()
def repo() -> Repository:
    return Repository.open(MEMORY)


def _trace(repo: Repository, trace_id: str = "t1") -> str:
    repo.insert_trace(TraceStart(id=trace_id, system="cgmp", operation="article.generate"))
    return trace_id


def _span_start(trace_id: str, span_id: str = "s1", seq: int = 1) -> SpanStart:
    return SpanStart(
        id=span_id,
        trace_id=trace_id,
        seq=seq,
        task="section",
        logical_model="chat-standard",
        adapter="mock",
        request_text="prompt text",
        prompt_id="cgmp.section",
        prompt_version=3,
        render_hash="0123456789abcdef",
    )


def test_schema_has_all_tables(repo: Repository) -> None:
    """Phase 2-3 のテーブルも最初から作る(後からの ALTER を避ける)。"""
    assert set(table_names(repo.conn)) >= EXPECTED_TABLES


def test_init_schema_is_idempotent(repo: Repository) -> None:
    init_schema(repo.conn)
    init_schema(repo.conn)
    assert set(table_names(repo.conn)) >= EXPECTED_TABLES


def test_foreign_keys_enabled(repo: Repository) -> None:
    assert repo.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_span(_span_start("missing-trace"))


def test_wal_mode_on_file_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "sub" / "x.sqlite3")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_session_commits_and_closes(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    with session(db) as conn:
        init_schema(conn)
        conn.execute(
            "INSERT INTO traces (id, system, operation, status, started_at)"
            " VALUES ('t', 'elf', 'op', 'running', '2026-01-01 00:00:00')"
        )
    with session(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 1


def test_session_rolls_back_on_error(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    with session(db) as conn:
        init_schema(conn)
    with pytest.raises(RuntimeError), session(db) as conn:
        conn.execute(
            "INSERT INTO traces (id, system, operation, status, started_at)"
            " VALUES ('t', 'elf', 'op', 'running', '2026-01-01 00:00:00')"
        )
        raise RuntimeError("boom")
    with session(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 0


def test_trace_lifecycle(repo: Repository) -> None:
    repo.insert_trace(
        TraceStart(id="t1", system="dde", operation="expand", external_id="run-9", meta={"n": 3})
    )
    row = repo.get_trace("t1")
    assert row is not None
    assert row["status"] == "running"
    assert row["finished_at"] is None
    assert row["external_id"] == "run-9"
    assert row["meta_json"] == '{"n": 3}'

    repo.update_trace("t1", status="success")
    row = repo.get_trace("t1")
    assert row is not None
    assert row["status"] == "success"
    assert row["finished_at"] is not None


def test_span_is_inserted_before_the_call(repo: Repository) -> None:
    """絶対ルール5: 呼び出し前に INSERT。落ちても「呼び出そうとした」記録が残る。"""
    trace_id = _trace(repo)
    repo.insert_span(_span_start(trace_id))

    row = repo.get_span("s1")
    assert row is not None
    assert row["success"] == 0
    assert row["request_text"] == "prompt text"
    assert row["response_text"] is None
    # 資産の刻印(FR-032)は開始時点で入っている
    assert row["prompt_id"] == "cgmp.section"
    assert row["prompt_version"] == 3
    assert row["render_hash"] == "0123456789abcdef"
    assert row["logical_model"] == "chat-standard"
    assert row["adapter"] == "mock"


def test_span_end_updates_measurements(repo: Repository) -> None:
    trace_id = _trace(repo)
    repo.insert_span(_span_start(trace_id))
    repo.update_span(
        "s1",
        SpanEnd(
            success=True,
            response_text="body",
            raw_response_json='{"result": "body"}',
            duration_ms=1200,
            api_duration_ms=1100,
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=5,
            cache_write_tokens=7,
            cost_usd=0.5,
            num_turns=1,
            provider_session="sess-1",
            meta={"truncated": True},
        ),
    )
    row = repo.get_span("s1")
    assert row is not None
    assert row["success"] == 1
    assert row["degraded"] == 0
    assert row["response_text"] == "body"
    assert row["cost_usd"] == 0.5
    assert row["input_tokens"] == 10
    assert row["cache_write_tokens"] == 7
    assert row["provider_session"] == "sess-1"
    assert row["meta_json"] == '{"truncated": true}'


def test_span_end_records_failure(repo: Repository) -> None:
    trace_id = _trace(repo)
    repo.insert_span(_span_start(trace_id))
    repo.update_span("s1", SpanEnd(success=False, error_type="AdapterError", error_message="boom"))
    row = repo.get_span("s1")
    assert row is not None
    assert row["success"] == 0
    assert row["error_type"] == "AdapterError"


def test_seq_and_count(repo: Repository) -> None:
    trace_id = _trace(repo)
    assert repo.next_span_seq(trace_id) == 1
    repo.insert_span(_span_start(trace_id, "s1", 1))
    assert repo.next_span_seq(trace_id) == 2
    repo.insert_span(_span_start(trace_id, "s2", 2))
    assert repo.count_spans(trace_id) == 2
    assert [r["id"] for r in repo.list_spans(trace_id)] == ["s1", "s2"]


def test_cost_daily_accumulates(repo: Repository) -> None:
    for _ in range(3):
        repo.upsert_cost_daily(
            day="2026-09-20",
            system="cgmp",
            logical_model="chat-standard",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.01,
        )
    row = repo.conn.execute("SELECT * FROM cost_daily").fetchone()
    assert row["calls"] == 3
    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 150
    assert row["cost_usd"] == pytest.approx(0.03)


def test_month_cost_filters_by_system(repo: Repository) -> None:
    repo.upsert_cost_daily(day="2026-09-01", system="cgmp", logical_model="m", cost_usd=1.0)
    repo.upsert_cost_daily(day="2026-09-30", system="dde", logical_model="m", cost_usd=2.0)
    repo.upsert_cost_daily(day="2026-08-31", system="cgmp", logical_model="m", cost_usd=4.0)
    assert repo.month_cost("2026-09") == pytest.approx(3.0)
    assert repo.month_cost("2026-09", system="cgmp") == pytest.approx(1.0)
