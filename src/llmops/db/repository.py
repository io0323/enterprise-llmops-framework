"""全DB操作の集約(リポジトリパターン。DDE / CGMP と同じ)。

SQL をここ以外に書かない。Phase 1 Step 1-1 の範囲は trace / span / cost_daily のみ。
Prompt / Model / Guard / レポート用のメソッドは後続 Step で追加する。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from llmops.db.connection import connect, init_schema
from llmops.models import SpanEnd, SpanStart, TraceStart


def utcnow() -> str:
    """DB に入れる時刻文字列(UTC・秒精度)。SQLite の比較が文字列順で成立する形にする。"""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _dumps(value: Any) -> str | None:
    if value is None or value == {}:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass
class Repository:
    """ELF の DB ハンドル。接続は呼び出し側が閉じる(`close()`)。"""

    conn: sqlite3.Connection

    @classmethod
    def open(cls, db_path: Path | str, *, create: bool = True) -> Repository:
        conn = connect(db_path)
        repo = cls(conn)
        if create:
            repo.init_schema()
        return repo

    def init_schema(self) -> None:
        init_schema(self.conn)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # traces
    # ------------------------------------------------------------------
    def insert_trace(self, trace: TraceStart) -> None:
        self.conn.execute(
            """
            INSERT INTO traces (id, system, operation, external_id, status, started_at, meta_json)
            VALUES (?, ?, ?, ?, 'running', ?, ?)
            """,
            (
                trace.id,
                trace.system,
                trace.operation,
                trace.external_id,
                utcnow(),
                _dumps(trace.meta),
            ),
        )
        self.conn.commit()

    def update_trace(
        self, trace_id: str, *, status: str, meta: dict[str, Any] | None = None
    ) -> None:
        """終了時の status / finished_at を確定する。meta は渡された場合のみ上書きする。"""
        if meta is None:
            self.conn.execute(
                "UPDATE traces SET status = ?, finished_at = ? WHERE id = ?",
                (status, utcnow(), trace_id),
            )
        else:
            self.conn.execute(
                "UPDATE traces SET status = ?, finished_at = ?, meta_json = ? WHERE id = ?",
                (status, utcnow(), _dumps(meta), trace_id),
            )
        self.conn.commit()

    def get_trace(self, trace_id: str) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM traces WHERE id = ?", (trace_id,)).fetchone()
        return cast("sqlite3.Row | None", row)

    # ------------------------------------------------------------------
    # spans
    # ------------------------------------------------------------------
    def next_span_seq(self, trace_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM spans WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        return int(row["seq"])

    def count_spans(self, trace_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM spans WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        return int(row["n"])

    def insert_span(self, span: SpanStart) -> None:
        """呼び出し**前**に実行される。success は 0(未確定)で入る。"""
        self.conn.execute(
            """
            INSERT INTO spans (
                id, trace_id, parent_span_id, seq, task,
                prompt_id, prompt_version, render_hash,
                logical_model, model_version, adapter, resolved_target,
                request_text, success, attempt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                span.id,
                span.trace_id,
                span.parent_span_id,
                span.seq,
                span.task,
                span.prompt_id,
                span.prompt_version,
                span.render_hash,
                span.logical_model,
                span.model_version,
                span.adapter,
                span.resolved_target,
                span.request_text,
                span.attempt,
                utcnow(),
            ),
        )
        self.conn.commit()

    def update_span(self, span_id: str, end: SpanEnd) -> None:
        self.conn.execute(
            """
            UPDATE spans SET
                response_text = ?, raw_response_json = ?,
                success = ?, degraded = ?, error_type = ?, error_message = ?,
                duration_ms = ?, api_duration_ms = ?,
                input_tokens = ?, output_tokens = ?,
                cache_read_tokens = ?, cache_write_tokens = ?,
                cost_usd = ?, num_turns = ?, provider_session = ?, meta_json = ?
            WHERE id = ?
            """,
            (
                end.response_text,
                end.raw_response_json,
                1 if end.success else 0,
                1 if end.degraded else 0,
                end.error_type,
                end.error_message,
                end.duration_ms,
                end.api_duration_ms,
                end.input_tokens,
                end.output_tokens,
                end.cache_read_tokens,
                end.cache_write_tokens,
                end.cost_usd,
                end.num_turns,
                end.provider_session,
                _dumps(end.meta),
                span_id,
            ),
        )
        self.conn.commit()

    def get_span(self, span_id: str) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM spans WHERE id = ?", (span_id,)).fetchone()
        return cast("sqlite3.Row | None", row)

    def list_spans(self, trace_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY seq", (trace_id,)
            ).fetchall()
        )

    # ------------------------------------------------------------------
    # cost_daily
    # ------------------------------------------------------------------
    def upsert_cost_daily(
        self,
        *,
        day: str,
        system: str,
        logical_model: str,
        calls: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """日次集計を加算する。`spans` からの導出だが、予算判定のたびに
        フルスキャンさせないための集計表(設計 §2.4)。"""
        self.conn.execute(
            """
            INSERT INTO cost_daily (day, system, logical_model, calls,
                                    input_tokens, output_tokens, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day, system, logical_model) DO UPDATE SET
                calls = calls + excluded.calls,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                cost_usd = cost_usd + excluded.cost_usd
            """,
            (day, system, logical_model, calls, input_tokens, output_tokens, cost_usd),
        )
        self.conn.commit()

    def month_cost(self, month: str, system: str | None = None) -> float:
        """当月の合計コスト。`month` は 'YYYY-MM'。"""
        if system is None:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM cost_daily WHERE day LIKE ?",
                (f"{month}-%",),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM cost_daily"
                " WHERE day LIKE ? AND system = ?",
                (f"{month}-%", system),
            ).fetchone()
        return float(row["total"])
