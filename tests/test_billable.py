"""実課金とサブスク換算の区別(NOTES.md N-045)。

`claude -p` のコストはサブスク利用の換算値で追加課金が無い。これを予算に混ぜると
**課金していない処理(CGMP/DDE)が、課金している処理(Harness)を止める**。
予算が見るのは `billable: true` のぶんだけ、という一点をここで固定する。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from llmops.config import Config
from llmops.db.connection import apply_migrations, connect, init_schema
from llmops.db.repository import Repository
from llmops.errors import LLMBudgetExceeded, QuotaExceeded
from llmops.gateway import Runtime
from llmops.models import CompletionRequest, SpanEnd, SpanStart, TraceStart
from llmops.observability.cost import this_month
from llmops.observability.report import collect, cost_report
from llmops.registry.model_registry import default_billable

NOW = datetime(2026, 9, 23, tzinfo=UTC)


def _request(trace_id: str, model: str) -> CompletionRequest:
    return CompletionRequest(
        trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
        variables={"message": "x"}, model=model,
    )


def _run(runtime: Runtime, model: str) -> Any:
    trace_id = runtime.tracer.start_trace("t")
    return runtime.gateway.complete(_request(trace_id, model))


# ---------------------------------------------------------------------------
# 宣言
# ---------------------------------------------------------------------------


def test_claude_cli_and_mock_default_to_non_billable() -> None:
    """既定は adapter から決まる。サブスク実行と mock は課金されない。"""
    assert default_billable("claude_cli") is False
    assert default_billable("mock") is False


def test_unknown_adapter_defaults_to_billable() -> None:
    """**宣言を忘れたら「課金される」側に倒す。** 黙って予算を素通りさせない。"""
    assert default_billable("anthropic_sdk") is True
    assert default_billable("some_future_provider") is True


def test_declaration_overrides_the_adapter_default(runtime: Runtime) -> None:
    """models.yaml の billable が最優先(conftest の priced は mock だが課金扱い)。"""
    assert runtime.models.resolve("priced").billable is True
    assert runtime.models.resolve("mock-echo").billable is False


# ---------------------------------------------------------------------------
# 予算判定
# ---------------------------------------------------------------------------


def test_subscription_cost_does_not_consume_the_budget(
    config: Config, repo: Repository
) -> None:
    """**これが N-045 の本体。** 課金していない処理は予算を食わない。"""
    runtime = Runtime.build(config, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    # 予算を使い切った状態を、サブスク換算のコストだけで作る
    repo.upsert_cost_daily(
        day=f"{this_month()}-01", system="cgmp", logical_model="chat-standard",
        cost_usd=999.0, billable=False,
    )
    repo.upsert_budget(budget_id="global", scope="global", period="monthly", limit_usd=1.0)

    # 実課金するモデルは、まだ予算が残っているので通る
    assert _run(runtime, "priced").text
    assert runtime.guard.budget_states("elf")[0].used_usd == pytest.approx(0.0, abs=1e-9)


def test_billable_cost_still_stops_the_call(config: Config, repo: Repository) -> None:
    runtime = Runtime.build(config, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    repo.upsert_cost_daily(
        day=f"{this_month()}-01", system="harness", logical_model="priced",
        cost_usd=999.0, billable=True,
    )
    repo.upsert_budget(budget_id="global", scope="global", period="monthly", limit_usd=1.0)

    with pytest.raises(QuotaExceeded):
        _run(runtime, "priced")


def test_non_billable_call_is_not_blocked_by_an_exhausted_budget(
    config: Config, repo: Repository
) -> None:
    """予算を使い切っていても、課金しない呼び出しは止めない。"""
    runtime = Runtime.build(config, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    repo.upsert_cost_daily(
        day=f"{this_month()}-01", system="harness", logical_model="priced",
        cost_usd=999.0, billable=True,
    )
    repo.upsert_budget(budget_id="global", scope="global", period="monthly", limit_usd=1.0)

    assert _run(runtime, "mock-echo").text


def test_call_count_limit_still_applies_to_non_billable(
    config: Config, repo: Repository
) -> None:
    """回数上限は課金と無関係(CGMP の1記事10回は課金の話ではない)。"""
    limited = config.model_copy(
        update={"guard": config.guard.model_copy(update={"calls_per_trace_limit": 1})}
    )
    runtime = Runtime.build(limited, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    trace_id = runtime.tracer.start_trace("t")
    one_shot = replace(_request(trace_id, "mock-echo"), attempts=1)
    runtime.gateway.complete(one_shot)
    with pytest.raises(LLMBudgetExceeded, match="呼び出し上限"):
        runtime.gateway.complete(one_shot)


# ---------------------------------------------------------------------------
# 記録は落とさない
# ---------------------------------------------------------------------------


def test_non_billable_cost_is_still_recorded(runtime: Runtime, repo: Repository) -> None:
    """可視性は落とさない。cost_daily にも span にも残る。"""
    result = _run(runtime, "mock-echo")

    span = repo.get_span(result.span_id)
    assert span is not None
    assert span["billable"] == 0
    row = repo.conn.execute(
        "SELECT billable, cost_usd FROM cost_daily WHERE logical_model = 'mock-echo'"
    ).fetchone()
    assert row is not None and row["billable"] == 0
    # 全体を見るときは billable_only=False
    assert repo.month_cost(this_month(), billable_only=False) >= 0.0


def test_month_cost_separates_the_two(repo: Repository) -> None:
    month = this_month()
    repo.upsert_cost_daily(
        day=f"{month}-01", system="cgmp", logical_model="chat-standard",
        cost_usd=1.5, billable=False,
    )
    repo.upsert_cost_daily(
        day=f"{month}-01", system="harness", logical_model="harness-generator",
        cost_usd=0.25, billable=True,
    )

    assert repo.month_cost(month) == pytest.approx(0.25)
    assert repo.month_cost(month, billable_only=False) == pytest.approx(1.75)


# ---------------------------------------------------------------------------
# レポート(予算対象かどうかが読み取れること)
# ---------------------------------------------------------------------------


def _record_span(repo: Repository, *, system: str, cost: float, billable: bool) -> None:
    """コスト付きの span を1件作る(mock Adapter はコスト0を返すため直接組む)。"""
    trace_id = f"trace-{system}-{billable}-{cost}"
    repo.insert_trace(TraceStart(id=trace_id, system=system, operation="t"))
    span_id = f"span-{trace_id}"
    repo.insert_span(
        SpanStart(
            id=span_id, trace_id=trace_id, seq=1, task="t", logical_model="m",
            adapter="mock", request_text="x", billable=billable,
        )
    )
    repo.update_span(span_id, SpanEnd(success=True, cost_usd=cost, duration_ms=1))


def test_report_separates_billable_from_subscription(repo: Repository) -> None:
    _record_span(repo, system="elf", cost=0.25, billable=True)
    _record_span(repo, system="elf", cost=1.50, billable=False)

    buckets = {b.key: b for b in collect(repo, since=datetime(2000, 1, 1, tzinfo=UTC))}
    elf = buckets["elf"]
    assert elf.cost_usd == pytest.approx(1.75)
    assert elf.billable_cost_usd == pytest.approx(0.25)
    assert elf.subscription_cost_usd == pytest.approx(1.50)
    assert elf.budget_scope == "混在"

    markdown = cost_report(repo, since=datetime(2000, 1, 1, tzinfo=UTC), now=NOW)
    assert "うち実課金" in markdown
    assert "実課金(予算の対象)" in markdown
    assert "サブスク換算(予算の対象外)" in markdown


def test_report_marks_a_subscription_only_group(repo: Repository) -> None:
    _record_span(repo, system="cgmp", cost=0.67, billable=False)
    (bucket,) = collect(repo, since=datetime(2000, 1, 1, tzinfo=UTC))
    assert bucket.budget_scope == "対象外"
    assert bucket.billable_cost_usd == 0.0


# ---------------------------------------------------------------------------
# 既存DBの移行(列追加と同時に過去行を直す)
# ---------------------------------------------------------------------------


def _legacy_db(path: Path) -> None:
    """billable 列が無かった頃の DB を作る。"""
    conn = connect(path)
    init_schema(conn)
    for table in ("spans", "cost_daily", "model_versions"):
        conn.execute(f"ALTER TABLE {table} DROP COLUMN billable")
    conn.commit()
    conn.close()


def test_migration_backfills_past_rows(tmp_path: Path) -> None:
    """**過去のサブスク換算コストが予算を食ったままにならないこと。**"""
    db = tmp_path / "legacy.sqlite3"
    _legacy_db(db)
    conn = connect(db)
    conn.execute(
        "INSERT INTO model_versions (logical_name, version, adapter, params_json, config_hash)"
        " VALUES ('chat-standard', 1, 'claude_cli', '{}', 'h1')"
    )
    conn.execute(
        "INSERT INTO model_versions (logical_name, version, adapter, params_json, config_hash)"
        " VALUES ('harness-generator', 1, 'anthropic_sdk', '{}', 'h2')"
    )
    conn.execute(
        "INSERT INTO cost_daily (day, system, logical_model, calls, cost_usd)"
        " VALUES ('2026-09-01', 'cgmp', 'chat-standard', 1, 0.67)"
    )
    conn.execute(
        "INSERT INTO cost_daily (day, system, logical_model, calls, cost_usd)"
        " VALUES ('2026-09-01', 'harness', 'harness-generator', 1, 0.02)"
    )
    conn.commit()

    applied = apply_migrations(conn)

    assert {"spans.billable", "cost_daily.billable", "model_versions.billable"} <= set(applied)
    rows = {
        str(r["logical_model"]): int(r["billable"])
        for r in conn.execute("SELECT logical_model, billable FROM cost_daily")
    }
    assert rows == {"chat-standard": 0, "harness-generator": 1}
    assert apply_migrations(conn) == [], "2回目は何もしない(冪等)"
    conn.close()


def test_migration_backfills_spans_by_adapter(tmp_path: Path) -> None:
    db = tmp_path / "legacy-spans.sqlite3"
    _legacy_db(db)
    conn = connect(db)
    conn.execute(
        "INSERT INTO traces (id, system, operation, started_at)"
        " VALUES ('t1', 'cgmp', 'x', '2026-09-01 00:00:00')"
    )
    for span_id, adapter in (("s1", "claude_cli"), ("s2", "anthropic_sdk"), ("s3", "mock")):
        conn.execute(
            "INSERT INTO spans (id, trace_id, seq, task, logical_model, adapter,"
            " request_text, success) VALUES (?, 't1', 1, 'x', 'm', ?, '', 1)",
            (span_id, adapter),
        )
    conn.commit()

    apply_migrations(conn)

    rows = conn.execute("SELECT id, billable FROM spans").fetchall()
    assert {str(r["id"]): int(r["billable"]) for r in rows} == {"s1": 0, "s2": 1, "s3": 0}
    conn.close()


def test_models_yaml_declares_billable_for_every_model() -> None:
    """**宣言漏れを許さない。** 既定に頼ると、後から足したモデルが黙って対象外になる。"""
    from llmops.config import project_root

    loaded = yaml.safe_load((project_root() / "models.yaml").read_text(encoding="utf-8"))
    missing = [name for name, entry in loaded["models"].items() if "billable" not in entry]
    assert missing == [], f"models.yaml に billable の宣言がありません: {missing}"
