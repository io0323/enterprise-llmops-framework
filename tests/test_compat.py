"""SDK と互換shim の検証(Step 1-5)。

**移行の安全網**。既存 CGMP / DDE の `LLMClient` を使うコード片が、import を
差し替えただけで同じ挙動になることをここで押さえる(FR-013 / NFR-04)。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from llmops.config import Config
from llmops.db.repository import Repository
from llmops.errors import LLMBudgetExceeded, LLMError, PolicyViolation
from llmops.gateway import Runtime
from llmops.sdk import LLMOps
from llmops.sdk.compat import LLMClient, _llmops_section


@pytest.fixture()
def raw_config(config: Config) -> Config:
    """adhoc 呼び出しを許可した設定(移行期間の想定)。"""
    return config.model_copy(
        update={"gateway": config.gateway.model_copy(update={"allow_raw_completion": True})}
    )


@pytest.fixture()
def raw_runtime(raw_config: Config, repo: Repository) -> Runtime:
    runtime = Runtime.build(raw_config, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    return runtime


@pytest.fixture()
def client(raw_runtime: Runtime) -> LLMClient:
    return LLMClient.from_runtime(raw_runtime, system="cgmp", model="mock-echo")


# ---------------------------------------------------------------------------
# SDK(設計 §5.1)
# ---------------------------------------------------------------------------


def test_trace_context_closes_as_success(runtime: Runtime, repo: Repository) -> None:
    ops = LLMOps.from_runtime(runtime)
    with ops.trace("article.generate", external_id="req-1") as tr:
        ops.complete(prompt_id="elf.smoke", variables={"message": "x"})
    row = repo.get_trace(tr.trace_id)
    assert row is not None
    assert row["status"] == "success"
    assert row["finished_at"] is not None


def test_trace_context_records_failure(runtime: Runtime, repo: Repository) -> None:
    ops = LLMOps.from_runtime(runtime)
    with pytest.raises(RuntimeError), ops.trace("article.generate") as tr:
        raise RuntimeError("boom")
    row = repo.get_trace(tr.trace_id)
    assert row is not None
    assert row["status"] == "failed"


def test_trace_context_can_be_marked_partial(runtime: Runtime, repo: Repository) -> None:
    ops = LLMOps.from_runtime(runtime)
    with ops.trace("article.generate") as tr:
        tr.partial()
    row = repo.get_trace(tr.trace_id)
    assert row is not None
    assert row["status"] == "partial"


def test_completes_are_attached_to_the_open_trace(runtime: Runtime, repo: Repository) -> None:
    """1記事 = 1 trace・N span として辿れること(FR-030)。"""
    ops = LLMOps.from_runtime(runtime)
    with ops.trace("article.generate") as tr:
        for _ in range(3):
            ops.complete(prompt_id="elf.smoke", variables={"message": "x"}, task="section")
    spans = repo.list_spans(tr.trace_id)
    assert len(spans) == 3
    assert {row["task"] for row in spans} == {"section"}


def test_complete_without_trace_creates_one(runtime: Runtime, repo: Repository) -> None:
    ops = LLMOps.from_runtime(runtime)
    result = ops.complete(prompt_id="elf.smoke", variables={"message": "x"})
    span = repo.get_span(result.span_id)
    assert span is not None
    assert repo.get_trace(str(span["trace_id"])) is not None


def test_task_defaults_to_prompt_suffix(runtime: Runtime, repo: Repository) -> None:
    ops = LLMOps.from_runtime(runtime)
    result = ops.complete(prompt_id="elf.smoke", variables={"message": "x"})
    span = repo.get_span(result.span_id)
    assert span is not None
    assert span["task"] == "smoke"


def test_retry_false_means_single_attempt(runtime: Runtime, repo: Repository) -> None:
    from tests.test_gateway import CountingAdapter

    runtime.gateway._adapters["mock"] = CountingAdapter(fail_times=5)
    ops = LLMOps.from_runtime(runtime)
    with ops.trace("t") as tr, pytest.raises(LLMError):
        ops.complete(prompt_id="elf.smoke", variables={"message": "x"}, retry=False)
    assert len(repo.list_spans(tr.trace_id)) == 1


def test_complete_raw_is_denied_by_default(runtime: Runtime, repo: Repository) -> None:
    """既定では Registry 管理外の呼び出しを拒否する(設計 §5.1)。"""
    ops = LLMOps.from_runtime(runtime)
    with pytest.raises(PolicyViolation, match="allow_raw_completion"):
        ops.complete_raw(text="素の文字列", model="mock-echo", task="adhoc")
    assert repo.list_audit_logs(event="policy.violation")


def test_complete_raw_is_audited_when_allowed(raw_runtime: Runtime, repo: Repository) -> None:
    ops = LLMOps.from_runtime(raw_runtime)
    result = ops.complete_raw(text="素の文字列", model="mock-echo", task="adhoc")
    assert result.text == "素の文字列"
    assert repo.list_audit_logs(event="raw_completion")


# ---------------------------------------------------------------------------
# 互換shim — CGMP 版シグネチャ
# ---------------------------------------------------------------------------


def test_call_text_returns_stripped_body(client: LLMClient) -> None:
    assert client.call_text("本文をください", task="section") == "本文をください"


def test_call_text_strips_fence(raw_runtime: Runtime) -> None:
    """既存 `strip_fence` と同じ挙動(mock echo がフェンス付き文字列を返す形で検証)。"""
    client = LLMClient.from_runtime(raw_runtime, system="cgmp", model="mock-echo")
    assert client.call_text("```md\n本文\n```", task="section") == "本文"


def test_call_json_parses(raw_runtime: Runtime) -> None:
    client = LLMClient.from_runtime(raw_runtime, system="cgmp", model="mock-echo")
    assert client.call_json('{"sections": ["a"]}', task="outline") == {"sections": ["a"]}


def test_call_json_raises_llm_error_on_garbage(client: LLMClient) -> None:
    """既存と同じく `LLMError` 系で落ちる(呼び出し側の except がそのまま効く)。"""
    with pytest.raises(LLMError):
        client.call_json("これはJSONではありません", task="outline")


def test_spans_are_adhoc_during_migration(client: LLMClient, repo: Repository) -> None:
    """移行 Step 1 の時点では prompt_id=NULL(版はまだ刻まれない)。"""
    client.call_text("本文", task="section", request_id="req-1")
    span = repo.conn.execute("SELECT * FROM spans ORDER BY created_at DESC LIMIT 1").fetchone()
    assert span["prompt_id"] is None
    assert span["task"] == "section"


def test_section_seq_is_kept_in_meta(client: LLMClient, repo: Repository) -> None:
    """既存 llm_logs の section_seq に相当する情報を落とさない。"""
    client.call_text("本文", task="section", request_id="req-1", section_seq=3)
    span = repo.conn.execute("SELECT * FROM spans ORDER BY created_at DESC LIMIT 1").fetchone()
    assert json.loads(span["meta_json"])["section_seq"] == 3


def test_request_id_maps_to_one_trace(client: LLMClient, repo: Repository) -> None:
    """1記事(request_id)= 1 trace。呼び出し回数はこの単位で数える。"""
    client.call_text("a", task="section", request_id="req-1")
    client.call_text("b", task="section", request_id="req-1")
    client.call_text("c", task="section", request_id="req-2")

    traces = repo.conn.execute(
        "SELECT external_id, COUNT(*) AS n FROM traces GROUP BY external_id"
    ).fetchall()
    counts = {row["external_id"]: row["n"] for row in traces}
    assert counts["req-1"] == 1
    assert counts["req-2"] == 1
    assert client.calls_used("req-1") == 2
    assert client.calls_used("req-2") == 1


# ---------------------------------------------------------------------------
# 互換shim — 呼び出し回数上限(CGMP 絶対ルール3)
# ---------------------------------------------------------------------------


def test_calls_used_and_remaining(client: LLMClient) -> None:
    assert client.calls_used("req-1") == 0
    assert client.remaining_calls("req-1") == client.call_limit
    client.call_text("a", task="section", request_id="req-1")
    assert client.calls_used("req-1") == 1
    assert client.remaining_calls("req-1") == client.call_limit - 1


def test_ensure_budget_raises_when_short(raw_config: Config, repo: Repository) -> None:
    tight = raw_config.model_copy(
        update={"guard": raw_config.guard.model_copy(update={"calls_per_trace_limit": 2})}
    )
    runtime = Runtime.build(tight, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    client = LLMClient.from_runtime(runtime, system="cgmp", model="mock-echo")

    client.ensure_budget("req-1", needed=2)  # まだ通る
    client.call_text("a", task="section", request_id="req-1", retry=False)
    client.call_text("b", task="section", request_id="req-1", retry=False)
    with pytest.raises(LLMBudgetExceeded):
        client.ensure_budget("req-1", needed=1)


def test_budget_exception_is_also_an_llm_error() -> None:
    """既存 CGMP は `LLMBudgetExceeded(LLMError)`。捕捉可能性を維持する(N-017)。"""
    assert issubclass(LLMBudgetExceeded, LLMError)


# ---------------------------------------------------------------------------
# 互換shim — DDE 版シグネチャ
# ---------------------------------------------------------------------------


def test_dde_style_call(raw_runtime: Runtime) -> None:
    client = LLMClient.from_runtime(raw_runtime, system="dde", model="mock-echo")
    assert client.call('["a", "b"]', task="expand") == ["a", "b"]


def test_dde_batch_size_is_kept_in_meta(raw_runtime: Runtime, repo: Repository) -> None:
    client = LLMClient.from_runtime(raw_runtime, system="dde", model="mock-echo")
    client.call("[1]", task="classify", batch_size=50)
    span = repo.conn.execute("SELECT * FROM spans ORDER BY created_at DESC LIMIT 1").fetchone()
    assert json.loads(span["meta_json"])["batch_size"] == 50


def test_dde_call_records_system(raw_runtime: Runtime, repo: Repository) -> None:
    client = LLMClient.from_runtime(raw_runtime, system="dde", model="mock-echo")
    client.call("[1]", task="expand")
    assert repo.conn.execute("SELECT system FROM traces LIMIT 1").fetchone()["system"] == "dde"
    assert repo.conn.execute("SELECT system FROM cost_daily LIMIT 1").fetchone()["system"] == "dde"


# ---------------------------------------------------------------------------
# 設定の読み取り
# ---------------------------------------------------------------------------


def test_llmops_section_from_raw_dict() -> None:
    class AppConfig:
        raw = {"llmops": {"system": "cgmp", "model": "dde-batch"}}

    assert _llmops_section(AppConfig())["model"] == "dde-batch"


def test_llmops_section_from_attribute() -> None:
    class AppConfig:
        llmops = {"model": "chat-fast"}

    assert _llmops_section(AppConfig())["model"] == "chat-fast"


def test_llmops_section_from_plain_dict() -> None:
    assert _llmops_section({"llmops": {"model": "m"}})["model"] == "m"


def test_llmops_section_missing_is_empty() -> None:
    class AppConfig:
        pass

    assert _llmops_section(AppConfig()) == {}
    assert _llmops_section(None) == {}


def test_repo_argument_is_accepted_and_ignored(raw_runtime: Runtime) -> None:
    """既存呼び出し `LLMClient(config=config, repo=repo)` の形をそのまま受ける。"""
    sentinel = object()
    client = LLMClient(
        None,
        sentinel,
        system="cgmp",
        ops=LLMOps.from_runtime(raw_runtime, system="cgmp"),
        model="mock-echo",
    )
    assert client.call_text("x", task="t") == "x"


def test_model_comes_from_app_config(raw_runtime: Runtime, repo: Repository) -> None:
    """アプリ側 config の `llmops.model` を使う(DDE は dde-batch を使う。Step 1-7)。"""

    class AppConfig:
        raw: dict[str, Any] = {"llmops": {"model": "mock-json"}}

    client = LLMClient(
        AppConfig(),
        None,
        system="dde",
        ops=LLMOps.from_runtime(raw_runtime, system="dde"),
    )
    assert client.model == "mock-json"
