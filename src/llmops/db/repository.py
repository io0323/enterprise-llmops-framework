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
from llmops.models import ModelVersionRow, PromptVersionRow, SpanEnd, SpanStart, TraceStart


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

    # ------------------------------------------------------------------
    # prompt_versions / prompt_deployments / prompt_transitions
    # ------------------------------------------------------------------
    def insert_prompt_version(self, version: PromptVersionRow) -> None:
        self.conn.execute(
            """
            INSERT INTO prompt_versions (
                prompt_id, version, status, body, front_matter, var_schema,
                content_hash, source_path, owner, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.prompt_id,
                version.version,
                version.status,
                version.body,
                version.front_matter,
                version.var_schema,
                version.content_hash,
                version.source_path,
                version.owner,
                version.note,
                utcnow(),
                utcnow(),
            ),
        )
        self.conn.commit()

    def get_prompt_version(self, prompt_id: str, version: int) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM prompt_versions WHERE prompt_id = ? AND version = ?",
            (prompt_id, version),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def latest_prompt_version(self, prompt_id: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM prompt_versions WHERE prompt_id = ? ORDER BY version DESC LIMIT 1",
            (prompt_id,),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def list_prompt_versions(self, prompt_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM prompt_versions WHERE prompt_id = ? ORDER BY version",
                (prompt_id,),
            ).fetchall()
        )

    def list_prompts(self, *, status: str | None = None) -> list[sqlite3.Row]:
        """prompt_id ごとの最新版を返す(カタログ表示用)。"""
        sql = """
            SELECT pv.*, pd.active_version, pd.canary_version, pd.canary_percent
              FROM prompt_versions pv
              JOIN (SELECT prompt_id, MAX(version) AS version
                      FROM prompt_versions GROUP BY prompt_id) latest
                ON pv.prompt_id = latest.prompt_id AND pv.version = latest.version
              LEFT JOIN prompt_deployments pd ON pd.prompt_id = pv.prompt_id
        """
        params: tuple[str, ...] = ()
        if status is not None:
            sql += " WHERE pv.status = ?"
            params = (status,)
        sql += " ORDER BY pv.prompt_id"
        return list(self.conn.execute(sql, params).fetchall())

    def set_prompt_status(self, prompt_id: str, version: int, status: str) -> None:
        self.conn.execute(
            "UPDATE prompt_versions SET status = ?, updated_at = ?"
            " WHERE prompt_id = ? AND version = ?",
            (status, utcnow(), prompt_id, version),
        )
        self.conn.commit()

    def get_deployment(self, prompt_id: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM prompt_deployments WHERE prompt_id = ?", (prompt_id,)
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def set_deployment(
        self,
        prompt_id: str,
        *,
        active_version: int,
        canary_version: int | None = None,
        canary_percent: int = 0,
        actor: str | None = None,
    ) -> None:
        """publish / rollback / canary はこの1行の更新で完結する(FR-050)。"""
        self.conn.execute(
            """
            INSERT INTO prompt_deployments
                (prompt_id, active_version, canary_version, canary_percent, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(prompt_id) DO UPDATE SET
                active_version = excluded.active_version,
                canary_version = excluded.canary_version,
                canary_percent = excluded.canary_percent,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (prompt_id, active_version, canary_version, canary_percent, utcnow(), actor),
        )
        self.conn.commit()

    def insert_prompt_transition(
        self,
        *,
        prompt_id: str,
        version: int,
        from_status: str | None,
        to_status: str,
        actor: str | None = None,
        reason: str | None = None,
        eval_run_id: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO prompt_transitions
                (prompt_id, version, from_status, to_status, actor, reason, eval_run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (prompt_id, version, from_status, to_status, actor, reason, eval_run_id, utcnow()),
        )
        self.conn.commit()

    def list_prompt_transitions(self, prompt_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM prompt_transitions WHERE prompt_id = ? ORDER BY id",
                (prompt_id,),
            ).fetchall()
        )

    def prompt_last_used_at(self, prompt_id: str) -> str | None:
        """最終利用日を `spans` から導出する(FR-052 の下ごしらえ)。"""
        row = self.conn.execute(
            "SELECT MAX(created_at) AS last_used FROM spans WHERE prompt_id = ?", (prompt_id,)
        ).fetchone()
        return None if row is None or row["last_used"] is None else str(row["last_used"])

    # ------------------------------------------------------------------
    # audit_logs
    # ------------------------------------------------------------------
    def insert_audit_log(
        self,
        *,
        event: str,
        actor: str | None = None,
        subject: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit_logs (event, actor, subject, detail_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (event, actor, subject, _dumps(detail), utcnow()),
        )
        self.conn.commit()

    def list_audit_logs(self, *, event: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if event is None:
            rows = self.conn.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_logs WHERE event = ? ORDER BY id DESC LIMIT ?", (event, limit)
            ).fetchall()
        return list(rows)

    # ------------------------------------------------------------------
    # model_versions
    # ------------------------------------------------------------------
    def insert_model_version(self, model: ModelVersionRow) -> None:
        self.conn.execute(
            """
            INSERT INTO model_versions (
                logical_name, version, adapter, params_json, price_json,
                fallback_to, status, config_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model.logical_name,
                model.version,
                model.adapter,
                model.params_json,
                model.price_json,
                model.fallback_to,
                model.status,
                model.config_hash,
                utcnow(),
            ),
        )
        self.conn.commit()

    def latest_model_version(self, logical_name: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM model_versions WHERE logical_name = ? ORDER BY version DESC LIMIT 1",
            (logical_name,),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def get_model_version(self, logical_name: str, version: int) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM model_versions WHERE logical_name = ? AND version = ?",
            (logical_name, version),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def list_model_versions(self, logical_name: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM model_versions WHERE logical_name = ? ORDER BY version",
                (logical_name,),
            ).fetchall()
        )

    def list_models(self) -> list[sqlite3.Row]:
        """論理モデル名ごとの最新版。"""
        return list(
            self.conn.execute(
                """
                SELECT mv.* FROM model_versions mv
                  JOIN (SELECT logical_name, MAX(version) AS version
                          FROM model_versions GROUP BY logical_name) latest
                    ON mv.logical_name = latest.logical_name AND mv.version = latest.version
                 ORDER BY mv.logical_name
                """
            ).fetchall()
        )

    # ------------------------------------------------------------------
    # budgets
    # ------------------------------------------------------------------
    def upsert_budget(
        self,
        *,
        budget_id: str,
        scope: str,
        period: str,
        limit_usd: float,
        hard_limit: bool = True,
        warn_percents: str = "[50,80,100]",
        enabled: bool = True,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO budgets (id, scope, period, limit_usd, hard_limit, warn_percents, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                scope = excluded.scope, period = excluded.period,
                limit_usd = excluded.limit_usd, hard_limit = excluded.hard_limit,
                warn_percents = excluded.warn_percents, enabled = excluded.enabled
            """,
            (
                budget_id,
                scope,
                period,
                limit_usd,
                1 if hard_limit else 0,
                warn_percents,
                1 if enabled else 0,
            ),
        )
        self.conn.commit()

    def get_budget(self, budget_id: str) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()
        return cast("sqlite3.Row | None", row)

    def list_budgets(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM budgets ORDER BY id").fetchall())

    # ------------------------------------------------------------------
    # 参照系(レポート / trace 表示)
    # ------------------------------------------------------------------
    def spans_since(self, since: str, *, system: str | None = None) -> list[sqlite3.Row]:
        """`since`(YYYY-MM-DD HH:MM:SS)以降の span を trace の system 付きで返す。"""
        sql = """
            SELECT s.*, t.system AS system, t.operation AS operation
              FROM spans s JOIN traces t ON t.id = s.trace_id
             WHERE s.created_at >= ?
        """
        params: tuple[str, ...] = (since,)
        if system is not None:
            sql += " AND t.system = ?"
            params = (since, system)
        sql += " ORDER BY s.created_at"
        return list(self.conn.execute(sql, params).fetchall())

    def list_traces(
        self,
        *,
        since: str | None = None,
        system: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM traces WHERE 1 = 1"
        params: list[Any] = []
        if since is not None:
            sql += " AND started_at >= ?"
            params.append(since)
        if system is not None:
            sql += " AND system = ?"
            params.append(system)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params).fetchall())

    def find_trace(self, key: str) -> sqlite3.Row | None:
        """trace_id か external_id で1件引く(CLI が両方を受けるため)。"""
        row = self.conn.execute(
            "SELECT * FROM traces WHERE id = ? OR external_id = ? ORDER BY started_at DESC LIMIT 1",
            (key, key),
        ).fetchone()
        return cast("sqlite3.Row | None", row)
