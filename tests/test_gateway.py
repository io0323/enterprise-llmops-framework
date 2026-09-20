"""Gateway / Guard / Tracer / Cost の統合検証(Step 1-4)。

`mock` Adapter とインメモリDBのみ。実APIは叩かない(絶対ルール14)。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from llmops.adapters.base import AdapterRequest, AdapterResponse, ProviderAdapter
from llmops.config import Config
from llmops.db.repository import Repository
from llmops.errors import (
    AdapterError,
    AllProvidersFailed,
    LLMBudgetExceeded,
    LLMError,
    ModelNotFound,
    PromptNotPublished,
    QuotaExceeded,
)
from llmops.gateway import Runtime
from llmops.models import CompletionRequest

VARS = {"message": "こんにちは"}


def _request(trace_id: str, **kwargs: Any) -> CompletionRequest:
    base: dict[str, Any] = {
        "trace_id": trace_id,
        "task": "smoke",
        "prompt_id": "elf.smoke",
        "variables": VARS,
    }
    base.update(kwargs)
    return CompletionRequest(**base)


def _trace(runtime: Runtime) -> str:
    return runtime.tracer.start_trace("test.op")


class CountingAdapter(ProviderAdapter):
    """N 回失敗してから成功する Adapter(retry の検証用)。"""

    name = "counting"

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    def invoke(self, req: AdapterRequest) -> AdapterResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise AdapterError(f"失敗 {self.calls} 回目")
        return AdapterResponse(text=req.text, raw={"result": req.text}, cost_usd=0.0)


# ---------------------------------------------------------------------------
# 正常系 — 資産の刻印(FR-032)
# ---------------------------------------------------------------------------


def test_complete_records_every_imprint(runtime: Runtime, repo: Repository) -> None:
    """完了条件: span に prompt_id / version / render_hash / model / adapter が全部入る。"""
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(_request(trace_id))

    span = repo.get_span(result.span_id)
    assert span is not None
    assert span["prompt_id"] == "elf.smoke"
    assert span["prompt_version"] == 1
    assert span["render_hash"] == result.render_hash
    assert span["logical_model"] == "mock-echo"
    assert span["model_version"] == 1
    assert span["adapter"] == "mock"
    assert span["success"] == 1
    assert span["degraded"] == 0
    assert span["trace_id"] == trace_id
    assert span["seq"] == 1


def test_render_hash_is_recorded_and_deterministic(runtime: Runtime) -> None:
    trace_id = _trace(runtime)
    first = runtime.gateway.complete(_request(trace_id))
    second = runtime.gateway.complete(_request(trace_id))
    assert first.render_hash == second.render_hash


def test_request_and_response_text_are_recorded(runtime: Runtime, repo: Repository) -> None:
    """入出力全文記録(CGMP 絶対ルール8 を継承)。"""
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(_request(trace_id))
    span = repo.get_span(result.span_id)
    assert span is not None
    assert "こんにちは" in span["request_text"]
    assert "こんにちは" in span["response_text"]
    assert json.loads(span["raw_response_json"])["adapter"] == "mock"


def test_default_model_comes_from_front_matter(runtime: Runtime) -> None:
    trace_id = _trace(runtime)
    assert runtime.gateway.complete(_request(trace_id)).logical_model == "mock-echo"


def test_explicit_model_overrides_front_matter(runtime: Runtime) -> None:
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(_request(trace_id, model="priced"))
    assert result.logical_model == "priced"


def test_as_json_parses_response(runtime: Runtime) -> None:
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(_request(trace_id, model="mock-json", as_json=True))
    assert result.json == {"sections": ["a", "b"]}


def test_spans_are_numbered_within_trace(runtime: Runtime, repo: Repository) -> None:
    trace_id = _trace(runtime)
    for _ in range(3):
        runtime.gateway.complete(_request(trace_id))
    assert [row["seq"] for row in repo.list_spans(trace_id)] == [1, 2, 3]


def test_adhoc_call_has_no_prompt_id(runtime: Runtime, repo: Repository) -> None:
    """互換shim 経由の呼び出しは prompt_id=NULL の adhoc span になる(移行 Step 1)。"""
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(
        CompletionRequest(trace_id=trace_id, task="adhoc", text="素の文字列", model="mock-echo")
    )
    span = repo.get_span(result.span_id)
    assert span is not None
    assert span["prompt_id"] is None
    assert span["render_hash"] is None
    assert result.text == "素の文字列"


def test_adhoc_without_text_is_error(runtime: Runtime) -> None:
    trace_id = _trace(runtime)
    with pytest.raises(LLMError, match="prompt_id も text も"):
        runtime.gateway.complete(CompletionRequest(trace_id=trace_id, task="x", model="mock-echo"))


def test_model_is_required_somewhere(runtime: Runtime, workspace: Any) -> None:
    trace_id = _trace(runtime)
    with pytest.raises(LLMError, match="論理モデル名が決まりません"):
        runtime.gateway.complete(CompletionRequest(trace_id=trace_id, task="x", text="a"))


# ---------------------------------------------------------------------------
# Prompt の状態(FR-021)
# ---------------------------------------------------------------------------


def test_unpublished_prompt_is_rejected(runtime: Runtime, workspace: Any) -> None:
    (workspace / "prompts" / "elf" / "smoke.md").write_text(
        (workspace / "prompts" / "elf" / "smoke.md").read_text().replace("書き写して", "写して"),
        encoding="utf-8",
    )
    runtime.prompts.sync()  # v2 は draft
    trace_id = _trace(runtime)
    with pytest.raises(PromptNotPublished):
        runtime.gateway.complete(_request(trace_id, version=2))


def test_allow_unpublished_opens_the_gate(config: Config, repo: Repository, workspace: Any) -> None:
    """開発時のみ true にする逃げ道が効くこと。"""
    relaxed = config.model_copy(
        update={"gateway": config.gateway.model_copy(update={"allow_unpublished": True})}
    )
    runtime = Runtime.build(relaxed, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    (workspace / "prompts" / "elf" / "smoke.md").write_text(
        (workspace / "prompts" / "elf" / "smoke.md").read_text().replace("書き写して", "写して"),
        encoding="utf-8",
    )
    runtime.prompts.sync()
    trace_id = _trace(runtime)
    assert runtime.gateway.complete(_request(trace_id, version=2)).prompt_version == 2


def test_blocked_model_is_not_resolvable(runtime: Runtime) -> None:
    trace_id = _trace(runtime)
    with pytest.raises(ModelNotFound, match="blocked"):
        runtime.gateway.complete(_request(trace_id, model="blocked-model"))


def test_unknown_model_is_not_found(runtime: Runtime) -> None:
    trace_id = _trace(runtime)
    with pytest.raises(ModelNotFound):
        runtime.gateway.complete(_request(trace_id, model="does-not-exist"))


# ---------------------------------------------------------------------------
# retry / fallback(絶対ルール7)
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_second_attempt(runtime: Runtime, repo: Repository) -> None:
    adapter = CountingAdapter(fail_times=1)
    runtime.gateway._adapters["mock"] = adapter
    trace_id = _trace(runtime)

    result = runtime.gateway.complete(_request(trace_id))

    assert adapter.calls == 2
    spans = repo.list_spans(trace_id)
    assert [row["success"] for row in spans] == [0, 1]
    assert [row["attempt"] for row in spans] == [1, 2]
    assert result.degraded is False


def test_failed_attempt_is_recorded_with_error(runtime: Runtime, repo: Repository) -> None:
    runtime.gateway._adapters["mock"] = CountingAdapter(fail_times=1)
    trace_id = _trace(runtime)
    runtime.gateway.complete(_request(trace_id))

    failed = repo.list_spans(trace_id)[0]
    assert failed["error_type"] == "AdapterError"
    assert "失敗 1 回目" in failed["error_message"]


def test_fallback_marks_degraded(runtime: Runtime, repo: Repository) -> None:
    """完了条件: 失敗する mock → chat-fallback に落ち、degraded=1 が記録される。"""
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(_request(trace_id, model="always-fail"))

    assert result.degraded is True
    assert result.logical_model == "mock-echo"
    span = repo.get_span(result.span_id)
    assert span is not None
    assert span["degraded"] == 1
    assert span["logical_model"] == "mock-echo"


def test_fallback_is_single_hop(runtime: Runtime, repo: Repository, config: Config) -> None:
    """2段目の Fallback は構造的に起きない(絶対ルール7)。"""
    trace_id = _trace(runtime)
    # always-fail → mock-echo。mock-echo も失敗させると、そこで打ち切られる
    runtime.gateway._adapters["mock"] = CountingAdapter(fail_times=99)
    with pytest.raises(AllProvidersFailed):
        runtime.gateway.complete(_request(trace_id, model="always-fail"))

    # 初段 retry_max=2 + fallback 先 retry_max=2 = 4 span。5 以上なら連鎖している
    assert len(repo.list_spans(trace_id)) == 4


def test_no_fallback_defined_raises(runtime: Runtime) -> None:
    trace_id = _trace(runtime)
    with pytest.raises(AllProvidersFailed):
        runtime.gateway.complete(_request(trace_id, model="fail-no-fallback"))


def test_fallback_disabled_by_config(config: Config, repo: Repository) -> None:
    disabled = config.model_copy(
        update={"gateway": config.gateway.model_copy(update={"fallback_enabled": False})}
    )
    runtime = Runtime.build(disabled, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    trace_id = _trace(runtime)
    with pytest.raises(AllProvidersFailed):
        runtime.gateway.complete(_request(trace_id, model="always-fail"))


# ---------------------------------------------------------------------------
# Guard(絶対ルール6: 呼び出しの「前」)
# ---------------------------------------------------------------------------


def test_calls_per_trace_limit_stops_before_adapter(config: Config, repo: Repository) -> None:
    """完了条件: 上限到達で呼び出し前に例外。**Adapter が呼ばれていない**こと。"""
    tight = config.model_copy(
        update={"guard": config.guard.model_copy(update={"calls_per_trace_limit": 2})}
    )
    runtime = Runtime.build(tight, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()

    adapter = CountingAdapter()
    runtime.gateway._adapters["mock"] = adapter
    trace_id = _trace(runtime)

    runtime.gateway.complete(_request(trace_id, attempts=1))
    runtime.gateway.complete(_request(trace_id, attempts=1))
    calls_before = adapter.calls

    with pytest.raises(LLMBudgetExceeded, match="呼び出し上限"):
        runtime.gateway.complete(_request(trace_id, attempts=1))

    assert adapter.calls == calls_before  # Adapter は呼ばれていない
    assert repo.count_spans(trace_id) == 2  # span も増えていない


def test_quota_exceeded_is_audited(config: Config, repo: Repository) -> None:
    tight = config.model_copy(
        update={"guard": config.guard.model_copy(update={"calls_per_trace_limit": 1})}
    )
    runtime = Runtime.build(tight, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    trace_id = _trace(runtime)
    runtime.gateway.complete(_request(trace_id, attempts=1))
    with pytest.raises(LLMBudgetExceeded):
        runtime.gateway.complete(_request(trace_id, attempts=1))

    assert repo.list_audit_logs(event="quota.exceeded")


def test_soft_quota_only_warns(config: Config, repo: Repository) -> None:
    soft = config.model_copy(
        update={
            "guard": config.guard.model_copy(
                update={"calls_per_trace_limit": 1, "hard_quota": False}
            )
        }
    )
    runtime = Runtime.build(soft, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    trace_id = _trace(runtime)
    runtime.gateway.complete(_request(trace_id, attempts=1))
    # 上限を超えても通る(記録は残る)
    assert runtime.gateway.complete(_request(trace_id, attempts=1)).text


def test_budget_stops_before_adapter(config: Config, repo: Repository) -> None:
    """予算超過は呼び出し前に止める(FR-034)。"""
    budgeted = config.model_copy(
        update={"budget": config.budget.model_copy(update={"global_monthly_usd": 0.001})}
    )
    runtime = Runtime.build(budgeted, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    adapter = CountingAdapter()
    runtime.gateway._adapters["mock"] = adapter
    trace_id = _trace(runtime)

    # priced モデルは単価が入っているので見積りが上限を超える
    with pytest.raises(QuotaExceeded, match="予算上限"):
        runtime.gateway.complete(_request(trace_id, model="priced"))

    assert adapter.calls == 0
    assert repo.count_spans(trace_id) == 0


def test_system_budget_is_applied(config: Config, repo: Repository) -> None:
    runtime = Runtime.build(config, repo=repo, system="cgmp")
    runtime.prompts.sync()
    runtime.models.sync()
    repo.upsert_budget(budget_id="cgmp", scope="system", period="monthly", limit_usd=0.0001)
    trace_id = _trace(runtime)
    with pytest.raises(QuotaExceeded, match="cgmp"):
        runtime.gateway.complete(_request(trace_id, model="priced"))


def test_disabled_budget_is_ignored(config: Config, repo: Repository) -> None:
    runtime = Runtime.build(config, repo=repo, system="cgmp")
    runtime.prompts.sync()
    runtime.models.sync()
    repo.upsert_budget(
        budget_id="cgmp", scope="system", period="monthly", limit_usd=0.0, enabled=False
    )
    trace_id = _trace(runtime)
    assert runtime.gateway.complete(_request(trace_id, model="priced")).text


def test_soft_budget_only_warns(config: Config, repo: Repository) -> None:
    runtime = Runtime.build(config, repo=repo, system="cgmp")
    runtime.prompts.sync()
    runtime.models.sync()
    repo.upsert_budget(
        budget_id="cgmp", scope="system", period="monthly", limit_usd=0.0, hard_limit=False
    )
    trace_id = _trace(runtime)
    assert runtime.gateway.complete(_request(trace_id, model="priced")).text


# ---------------------------------------------------------------------------
# Cost(FR-031)
# ---------------------------------------------------------------------------


def test_cost_daily_is_updated(runtime: Runtime, repo: Repository) -> None:
    trace_id = _trace(runtime)
    runtime.gateway.complete(_request(trace_id))
    row = repo.conn.execute("SELECT * FROM cost_daily").fetchone()
    assert row["calls"] == 1
    assert row["system"] == "elf"
    assert row["logical_model"] == "mock-echo"


def test_provider_cost_wins_over_estimate(runtime: Runtime, repo: Repository) -> None:
    """Provider が実コストを返したらそれを正とする。"""
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(_request(trace_id, model="priced"))
    span = repo.get_span(result.span_id)
    assert span is not None
    # mock は cost_usd=0.0 を返すので、単価があっても 0.0 が入る
    assert span["cost_usd"] == 0.0
    assert span["meta_json"] is None or "cost_estimated" not in span["meta_json"]


def test_estimated_cost_is_flagged(runtime: Runtime, repo: Repository) -> None:
    """Provider がコストを返さないときは概算し、概算であることを span に刻む。"""

    class NoCostAdapter(ProviderAdapter):
        name = "mock"

        def invoke(self, req: AdapterRequest) -> AdapterResponse:
            return AdapterResponse(
                text=req.text, raw={"result": req.text}, input_tokens=1000, output_tokens=1000
            )

    runtime.gateway._adapters["mock"] = NoCostAdapter()
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(_request(trace_id, model="priced"))

    span = repo.get_span(result.span_id)
    assert span is not None
    assert span["cost_usd"] == pytest.approx(3.0)  # 1.0/1k * 1000 + 2.0/1k * 1000
    assert json.loads(span["meta_json"])["cost_estimated"] is True


# ---------------------------------------------------------------------------
# Tracer(絶対ルール4 / 5)
# ---------------------------------------------------------------------------


def test_trace_lifecycle_is_recorded(runtime: Runtime, repo: Repository) -> None:
    trace_id = runtime.tracer.start_trace("article.generate", external_id="req-1")
    runtime.gateway.complete(_request(trace_id))
    runtime.tracer.end_trace(trace_id, status="success")

    row = repo.get_trace(trace_id)
    assert row is not None
    assert row["status"] == "success"
    assert row["external_id"] == "req-1"
    assert row["finished_at"] is not None


def _break_span_writes(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """span の書き込みだけを壊す(Prompt 解決や Guard は生かしたまま)。"""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(runtime.repo, "insert_span", boom)
    monkeypatch.setattr(runtime.repo, "update_span", boom)


def test_tracer_failure_does_not_break_the_call(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """記録に失敗しても呼び出し結果は返る(絶対ルール4 / NFR-02)。"""
    _break_span_writes(runtime, monkeypatch)
    trace_id = _trace(runtime)
    with caplog.at_level("WARNING"):
        result = runtime.gateway.complete(_request(trace_id))
    assert result.text  # 応答は得られている
    assert "trace の記録に失敗" in caplog.text


def test_cost_accrual_failure_does_not_break_the_call(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(runtime.repo, "upsert_cost_daily", boom)
    trace_id = _trace(runtime)
    assert runtime.gateway.complete(_request(trace_id)).text


def test_strict_trace_raises(
    config: Config, repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ELF_STRICT_TRACE 相当。テストで記録漏れを検出するための逃げ道。"""
    strict = config.model_copy(
        update={"trace": config.trace.model_copy(update={"strict": True})}
    )
    runtime = Runtime.build(strict, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    trace_id = _trace(runtime)
    _break_span_writes(runtime, monkeypatch)
    with pytest.raises(RuntimeError, match="disk full"):
        runtime.gateway.complete(_request(trace_id))


def test_long_text_is_truncated(config: Config, repo: Repository, workspace: Any) -> None:
    short = config.model_copy(
        update={"trace": config.trace.model_copy(update={"max_text_chars": 20})}
    )
    runtime = Runtime.build(short, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    trace_id = _trace(runtime)

    result = runtime.gateway.complete(_request(trace_id, variables={"message": "あ" * 500}))
    span = repo.get_span(result.span_id)
    assert span is not None
    assert len(span["request_text"]) <= 21
    assert len(span["response_text"]) <= 21
    assert json.loads(span["meta_json"])["truncated"] is True


def test_text_recording_can_be_disabled(config: Config, repo: Repository) -> None:
    quiet = config.model_copy(
        update={
            "trace": config.trace.model_copy(
                update={"record_request_text": False, "record_response_text": False}
            )
        }
    )
    runtime = Runtime.build(quiet, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    trace_id = _trace(runtime)
    result = runtime.gateway.complete(_request(trace_id))

    span = repo.get_span(result.span_id)
    assert span is not None
    assert span["request_text"] == ""
    assert span["response_text"] is None
