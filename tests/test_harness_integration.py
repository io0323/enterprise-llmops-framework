"""Harness 統合(Step 3-6)で ELF 側に足した仕組み。

- `expected_text`: 版管理を自前で持つシステムが、ELF の知らない版を黙って送らない
- `use_adapter`: 利用システムが構成済みの Adapter(SDK クライアント)を渡す
- `TraceContext.child`: パイプラインのステップを spans を汚さずに記録する

Harness 側のコードはここでは import しない(ELF は Harness 無しで動く / NFR-03)。
"""

from __future__ import annotations

import json

import pytest

from llmops.adapters.base import AdapterRequest, AdapterResponse, ProviderAdapter
from llmops.db.repository import Repository
from llmops.errors import PromptMismatch
from llmops.gateway import Runtime
from llmops.sdk import LLMOps

VARS = {"message": "こんにちは"}
EXPECTED = "書き写してください: こんにちは\n"


@pytest.fixture()
def ops(runtime: Runtime) -> LLMOps:
    return LLMOps.from_runtime(runtime, system="harness")


# ---------------------------------------------------------------------------
# expected_text
# ---------------------------------------------------------------------------


def test_matching_expected_text_runs_normally(ops: LLMOps) -> None:
    result = ops.complete(prompt_id="elf.smoke", variables=VARS, expected_text=EXPECTED)
    assert result.prompt_id == "elf.smoke"
    assert result.prompt_version == 1


def test_mismatch_is_refused_before_the_call(ops: LLMOps, repo: Repository) -> None:
    """**LLM を呼ぶ前に**止める。課金も span も発生しない。"""
    adapter = _Configured()  # elf.smoke が実際に使う mock を置き換えて呼び出しを数える
    ops.runtime.gateway.use_adapter(adapter)
    with ops.trace("t") as tr, pytest.raises(PromptMismatch, match="elf.smoke@1"):
        ops.complete(
            prompt_id="elf.smoke",
            variables=VARS,
            expected_text="Harness の DB にだけある別の版\n",
        )
    assert adapter.seen == []
    assert list(repo.list_spans(tr.trace_id)) == []


def test_mismatch_message_points_to_registration(ops: LLMOps) -> None:
    with pytest.raises(PromptMismatch, match="publish してから"):
        ops.complete(prompt_id="elf.smoke", variables=VARS, expected_text="x")


def test_expected_text_is_optional(ops: LLMOps) -> None:
    """指定しなければ従来どおり(DDE / CGMP は照合しない)。"""
    assert ops.complete(prompt_id="elf.smoke", variables=VARS).text


# ---------------------------------------------------------------------------
# use_adapter
# ---------------------------------------------------------------------------


class _Configured(ProviderAdapter):
    """利用システムが構成して渡す Adapter の代役。既定の mock を置き換える。"""

    name = "mock"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def invoke(self, req: AdapterRequest) -> AdapterResponse:
        self.seen.append(req.text)
        return AdapterResponse(text="configured", raw={}, cost_usd=0.0)


def test_use_adapter_replaces_the_default(ops: LLMOps) -> None:
    configured = _Configured()
    ops.runtime.gateway.use_adapter(configured)

    result = ops.complete(prompt_id="elf.smoke", variables=VARS)

    assert result.text == "configured"
    assert configured.seen == [EXPECTED]


# ---------------------------------------------------------------------------
# TraceContext.child
# ---------------------------------------------------------------------------


def test_child_step_is_stamped_on_spans(ops: LLMOps, repo: Repository) -> None:
    with ops.trace("pipeline.daily", external_id="daily:2026-09-21") as tr:
        with tr.child("research"):
            pass
        with tr.child("generate"):
            ops.complete(prompt_id="elf.smoke", variables=VARS)

    spans = list(repo.list_spans(tr.trace_id))
    assert len(spans) == 1, "ステップは spans に行を作らない(Guard / コスト集計を狂わせない)"
    assert json.loads(spans[0]["meta_json"])["step"] == "generate"


def test_child_steps_are_recorded_on_the_trace(ops: LLMOps, repo: Repository) -> None:
    with ops.trace("pipeline.daily", external_id="daily:x", pipeline="daily") as tr:
        with tr.child("research"):
            pass
        with pytest.raises(RuntimeError), tr.child("generate"):
            raise RuntimeError("boom")

    trace = repo.get_trace(tr.trace_id)
    assert trace is not None
    meta = json.loads(trace["meta_json"])
    assert meta["pipeline"] == "daily", "開始時の meta を上書きで消さない"
    assert [(s["name"], s["status"]) for s in meta["steps"]] == [
        ("research", "success"),
        ("generate", "failed"),
    ]
    assert trace["status"] == "success", "ステップの失敗を trace に伝えるかは呼び出し側が決める"


def test_trace_without_steps_keeps_meta(ops: LLMOps, repo: Repository) -> None:
    with ops.trace("t", pipeline="weekly") as tr:
        pass
    trace = repo.get_trace(tr.trace_id)
    assert trace is not None
    assert json.loads(trace["meta_json"]) == {"pipeline": "weekly"}


def test_step_outside_trace_is_not_stamped(ops: LLMOps, repo: Repository) -> None:
    result = ops.complete(prompt_id="elf.smoke", variables=VARS)
    span = repo.get_span(result.span_id)
    assert span is not None
    assert "step" not in json.loads(span["meta_json"] or "{}")


def test_current_trace_is_exposed_only_inside(ops: LLMOps) -> None:
    assert ops.current_trace is None
    with ops.trace("t") as tr:
        assert ops.current_trace is tr
    assert ops.current_trace is None
