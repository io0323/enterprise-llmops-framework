"""レポート生成の検証(Step 1-6 / FR-033)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from llmops.db.repository import Repository
from llmops.gateway import Runtime
from llmops.models import CompletionRequest, SpanEnd
from llmops.observability.report import Bucket, collect, cost_report, parse_since, percentile

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# --since の解釈
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7d", NOW - timedelta(days=7)),
        ("24h", NOW - timedelta(hours=24)),
        ("2w", NOW - timedelta(weeks=2)),
    ],
)
def test_parse_since_relative(value: str, expected: datetime) -> None:
    assert parse_since(value, now=NOW) == expected


def test_parse_since_iso_date() -> None:
    assert parse_since("2026-09-01", now=NOW) == datetime(2026, 9, 1, tzinfo=UTC)


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="書式が不正"):
        parse_since("last week", now=NOW)


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------


def test_percentile_of_empty_is_zero() -> None:
    assert percentile([], 0.5) == 0


def test_percentile_picks_order_statistics() -> None:
    values = list(range(1, 101))
    # 補間なしの最近傍順位法。index = round(ratio * (n - 1))
    assert percentile(values, 0.5) == 51
    assert percentile(values, 0.95) == 95
    assert percentile(values, 0.0) == 1
    assert percentile(values, 1.0) == 100


def test_bucket_success_rate_of_empty() -> None:
    assert Bucket(key="x").success_rate == 0.0


def _run(runtime: Runtime, *, task: str = "smoke", model: str = "mock-echo") -> str:
    trace_id = runtime.tracer.start_trace("test.op")
    result = runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id,
            task=task,
            prompt_id="elf.smoke",
            variables={"message": "x"},
            model=model,
        )
    )
    runtime.tracer.end_trace(trace_id, status="success")
    return result.span_id


def test_collect_groups_by_system(runtime: Runtime, repo: Repository) -> None:
    _run(runtime)
    _run(runtime)
    buckets = collect(repo, since=datetime.now(UTC) - timedelta(days=1), by="system")
    assert [b.key for b in buckets] == ["elf"]
    assert buckets[0].calls == 2
    assert buckets[0].success_rate == 100.0


def test_collect_groups_by_model(runtime: Runtime, repo: Repository) -> None:
    _run(runtime, model="mock-echo")
    _run(runtime, model="priced")
    buckets = collect(repo, since=datetime.now(UTC) - timedelta(days=1), by="model")
    assert {b.key for b in buckets} == {"mock-echo", "priced"}


def test_collect_groups_by_prompt(runtime: Runtime, repo: Repository) -> None:
    _run(runtime)
    buckets = collect(repo, since=datetime.now(UTC) - timedelta(days=1), by="prompt")
    assert [b.key for b in buckets] == ["elf.smoke@1"]


def test_adhoc_spans_are_labelled(runtime: Runtime, repo: Repository) -> None:
    trace_id = runtime.tracer.start_trace("t")
    runtime.gateway.complete(
        CompletionRequest(trace_id=trace_id, task="adhoc", text="x", model="mock-echo")
    )
    buckets = collect(repo, since=datetime.now(UTC) - timedelta(days=1), by="prompt")
    assert [b.key for b in buckets] == ["(adhoc)"]


def test_collect_counts_failures_and_degraded(runtime: Runtime, repo: Repository) -> None:
    trace_id = runtime.tracer.start_trace("t")
    runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id,
            task="smoke",
            prompt_id="elf.smoke",
            variables={"message": "x"},
            model="always-fail",
        )
    )
    buckets = {b.key: b for b in collect(repo, since=datetime.now(UTC) - timedelta(days=1),
                                        by="model")}
    assert buckets["always-fail"].succeeded == 0
    assert buckets["mock-echo"].degraded == 1


def test_collect_excludes_older_spans(runtime: Runtime, repo: Repository) -> None:
    span_id = _run(runtime)
    repo.conn.execute(
        "UPDATE spans SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (span_id,)
    )
    repo.conn.commit()
    assert collect(repo, since=datetime.now(UTC) - timedelta(days=1), by="system") == []


def test_collect_filters_by_system(runtime: Runtime, repo: Repository) -> None:
    _run(runtime)
    assert collect(repo, since=datetime.now(UTC) - timedelta(days=1), system="dde") == []
    assert collect(repo, since=datetime.now(UTC) - timedelta(days=1), system="elf")


def test_collect_rejects_unknown_axis(repo: Repository) -> None:
    with pytest.raises(ValueError, match="--by"):
        collect(repo, since=datetime.now(UTC), by="phase-of-the-moon")


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_report_is_markdown_table(runtime: Runtime, repo: Repository) -> None:
    _run(runtime)
    markdown = cost_report(repo, since=datetime.now(UTC) - timedelta(days=1), by="system")
    assert "# ELF コストレポート" in markdown
    assert "| system | 呼び出し |" in markdown
    assert "## 合計" in markdown


def test_report_when_empty(repo: Repository) -> None:
    markdown = cost_report(repo, since=datetime.now(UTC) - timedelta(days=1))
    assert "記録された呼び出しはありません" in markdown


def test_report_highlights_degraded(runtime: Runtime, repo: Repository) -> None:
    """運用ガイド §2 が「必ず見る3つ」の筆頭に挙げている指標。"""
    trace_id = runtime.tracer.start_trace("t")
    runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id,
            task="smoke",
            prompt_id="elf.smoke",
            variables={"message": "x"},
            model="always-fail",
        )
    )
    markdown = cost_report(repo, since=datetime.now(UTC) - timedelta(days=1))
    assert "degraded(Fallback で得た結果): 1 件" in markdown
    assert "model health" in markdown


def test_report_separates_failed_cost(runtime: Runtime, repo: Repository) -> None:
    """失敗に使ったコストが分けて出ること(運用ガイド §2)。"""
    span_id = _run(runtime)
    repo.update_span(span_id, SpanEnd(success=False, cost_usd=0.5, duration_ms=10))
    markdown = cost_report(repo, since=datetime.now(UTC) - timedelta(days=1))
    assert "失敗に使ったコスト: 0.500000 USD" in markdown
