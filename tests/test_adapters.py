"""Adapter SPI と3実装の検証(Step 1-2)。

**実APIを叩かない**(絶対ルール14)。`claude_cli` は `subprocess.run` をモックし、
応答は `tests/fixtures/claude_cli_response_*.json`(Phase 0 の実測応答とその派生)を使う。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from llmops.adapters import ADAPTERS, AdapterRequest, get_adapter
from llmops.adapters.anthropic_sdk import AnthropicSdkAdapter
from llmops.adapters.base import ProviderAdapter
from llmops.adapters.claude_cli import ClaudeCliAdapter
from llmops.adapters.mock import MockAdapter
from llmops.errors import AdapterError, AdapterUnavailable, LLMError

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _Completed:
    """`subprocess.CompletedProcess` の最小代替(returncode / stdout / stderr のみ使う)。"""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def patch_run(
    monkeypatch: pytest.MonkeyPatch, completed: Any, record: dict[str, Any] | None = None
) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> Any:
        if record is not None:
            record["args"] = args
            record["kwargs"] = kwargs
        if isinstance(completed, Exception):
            raise completed
        return completed

    monkeypatch.setattr(subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# レジストリ
# ---------------------------------------------------------------------------


def test_registry_has_three_adapters() -> None:
    assert set(ADAPTERS) == {"claude_cli", "anthropic_sdk", "mock"}


def test_get_adapter_returns_instance() -> None:
    adapter = get_adapter("mock")
    assert isinstance(adapter, MockAdapter)
    assert isinstance(adapter, ProviderAdapter)


def test_unknown_adapter_is_unavailable() -> None:
    with pytest.raises(AdapterUnavailable, match="未知の adapter"):
        get_adapter("nope")


# ---------------------------------------------------------------------------
# mock
# ---------------------------------------------------------------------------


def test_mock_echo_is_deterministic() -> None:
    adapter = MockAdapter()
    first = adapter.invoke(AdapterRequest("こんにちは", {"mode": "echo"}))
    second = adapter.invoke(AdapterRequest("こんにちは", {"mode": "echo"}))
    assert first.text == "こんにちは"
    assert first == second
    assert first.cost_usd == 0.0
    assert first.input_tokens is not None and first.input_tokens > 0


def test_mock_defaults_to_echo() -> None:
    assert MockAdapter().invoke(AdapterRequest("x")).text == "x"


def test_mock_json_mode() -> None:
    resp = MockAdapter().invoke(
        AdapterRequest("ignored", {"mode": "json", "payload": {"sections": ["a"]}})
    )
    assert json.loads(resp.text) == {"sections": ["a"]}


def test_mock_fail_mode_raises_adapter_error() -> None:
    with pytest.raises(AdapterError):
        MockAdapter().invoke(AdapterRequest("x", {"mode": "fail"}))


def test_mock_unknown_mode_raises() -> None:
    with pytest.raises(AdapterError, match="未知の mode"):
        MockAdapter().invoke(AdapterRequest("x", {"mode": "teleport"}))


# ---------------------------------------------------------------------------
# claude_cli — 実測応答(Phase 0)
# ---------------------------------------------------------------------------


def test_claude_cli_collects_every_field_design_expects(monkeypatch: pytest.MonkeyPatch) -> None:
    """設計 §5.4 が拾うフィールドが、実測応答から全て取れること(FR-031)。"""
    payload = json.loads(fixture_text("claude_cli_response_success.json"))
    patch_run(monkeypatch, _Completed(stdout=json.dumps(payload)))

    resp = ClaudeCliAdapter().invoke(AdapterRequest("1+1は?"))

    assert resp.text == payload["result"]
    assert resp.cost_usd == payload["total_cost_usd"]
    assert resp.api_duration_ms == payload["duration_api_ms"]
    assert resp.num_turns == payload["num_turns"]
    assert resp.session_id == payload["session_id"]
    assert resp.input_tokens == payload["usage"]["input_tokens"]
    assert resp.output_tokens == payload["usage"]["output_tokens"]
    assert resp.cache_read_tokens == payload["usage"]["cache_read_input_tokens"]
    assert resp.cache_write_tokens == payload["usage"]["cache_creation_input_tokens"]
    assert resp.resolved_target == "claude -p --output-format json"


def test_claude_cli_keeps_raw_response_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """生応答を丸ごと残す(設計 §2.1。カラム化していない項目を後から再集計するため)。"""
    payload = json.loads(fixture_text("claude_cli_response_success.json"))
    patch_run(monkeypatch, _Completed(stdout=json.dumps(payload)))
    resp = ClaudeCliAdapter().invoke(AdapterRequest("x"))
    assert resp.raw == payload
    assert "modelUsage" in resp.raw


def test_claude_cli_does_not_read_model_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """実モデル名を持つ `modelUsage` を読まない(絶対ルール13 / NOTES.md N-011)。

    `modelUsage` を削っても、構造化フィールドの値が1つも変わらないことで確認する。
    """
    payload = json.loads(fixture_text("claude_cli_response_success.json"))
    without = {k: v for k, v in payload.items() if k != "modelUsage"}

    patch_run(monkeypatch, _Completed(stdout=json.dumps(payload)))
    with_model_usage = ClaudeCliAdapter().invoke(AdapterRequest("x"))
    patch_run(monkeypatch, _Completed(stdout=json.dumps(without)))
    without_model_usage = ClaudeCliAdapter().invoke(AdapterRequest("x"))

    assert with_model_usage.raw != without_model_usage.raw  # raw だけが違う
    for field in (
        "text",
        "cost_usd",
        "api_duration_ms",
        "num_turns",
        "session_id",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "resolved_target",
    ):
        assert getattr(with_model_usage, field) == getattr(without_model_usage, field)


def test_claude_cli_uses_command_and_timeout_from_params(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    patch_run(monkeypatch, _Completed(stdout='{"result": "ok"}'), record)

    ClaudeCliAdapter().invoke(
        AdapterRequest("prompt", {"command": ["claude", "-p"], "timeout_sec": 300})
    )

    assert record["args"][0] == ["claude", "-p"]
    assert record["kwargs"]["timeout"] == 300
    assert record["kwargs"]["input"] == "prompt"


# ---------------------------------------------------------------------------
# claude_cli — 欠損許容(絶対ルール9)
# ---------------------------------------------------------------------------


def test_minimal_response_only_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """必須は `result` のみ。他が全部無くても例外にならない。"""
    payload = json.loads(fixture_text("claude_cli_response_minimal.json"))
    patch_run(monkeypatch, _Completed(stdout=json.dumps(payload)))

    resp = ClaudeCliAdapter().invoke(AdapterRequest("x"))

    assert resp.text == payload["result"]
    assert resp.cost_usd is None
    assert resp.input_tokens is None
    assert resp.output_tokens is None
    assert resp.session_id is None
    assert resp.num_turns is None
    assert resp.api_duration_ms is None


def test_partial_response_keeps_present_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """usage はあるが cache 系と total_cost_usd が無い版(CLIバージョン差の想定)。"""
    payload = json.loads(fixture_text("claude_cli_response_partial.json"))
    patch_run(monkeypatch, _Completed(stdout=json.dumps(payload)))

    resp = ClaudeCliAdapter().invoke(AdapterRequest("x"))

    assert resp.input_tokens == 3
    assert resp.output_tokens == 5
    assert resp.cache_read_tokens is None
    assert resp.cost_usd is None
    # duration_api_ms が無いときは duration_ms にフォールバックする(設計 §5.4)
    assert resp.api_duration_ms == payload["duration_ms"]


def test_missing_fields_are_warned_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """欠損は WARN ログのみ。処理は継続する(絶対ルール9)。"""
    patch_run(monkeypatch, _Completed(stdout='{"result": "ok"}'))
    with caplog.at_level("WARNING"):
        resp = ClaudeCliAdapter().invoke(AdapterRequest("x"))
    assert resp.text == "ok"
    assert "想定フィールドがありません" in caplog.text
    for name in ("total_cost_usd", "usage", "session_id", "num_turns"):
        assert name in caplog.text


def test_no_warning_when_response_is_complete(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    patch_run(monkeypatch, _Completed(stdout=fixture_text("claude_cli_response_success.json")))
    with caplog.at_level("WARNING"):
        ClaudeCliAdapter().invoke(AdapterRequest("x"))
    assert "想定フィールドがありません" not in caplog.text


# ---------------------------------------------------------------------------
# claude_cli — 失敗の扱い
# ---------------------------------------------------------------------------


def test_missing_result_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run(monkeypatch, _Completed(stdout='{"usage": {"input_tokens": 1}}'))
    with pytest.raises(AdapterError, match="result"):
        ClaudeCliAdapter().invoke(AdapterRequest("x"))


def test_is_error_response_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`is_error: true` は失敗(既存実装の判定を踏襲)。"""
    patch_run(monkeypatch, _Completed(stdout=fixture_text("claude_cli_response_is_error.json")))
    with pytest.raises(AdapterError, match="is_error"):
        ClaudeCliAdapter().invoke(AdapterRequest("x"))


def test_nonzero_returncode_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run(monkeypatch, _Completed(stdout="", returncode=2, stderr="boom"))
    with pytest.raises(AdapterError, match="異常終了"):
        ClaudeCliAdapter().invoke(AdapterRequest("x"))


def test_invalid_json_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run(monkeypatch, _Completed(stdout=fixture_text("claude_cli_response_invalid.txt")))
    with pytest.raises(AdapterError, match="JSON"):
        ClaudeCliAdapter().invoke(AdapterRequest("x"))


def test_non_object_json_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run(monkeypatch, _Completed(stdout='["not", "an", "object"]'))
    with pytest.raises(AdapterError, match="オブジェクト"):
        ClaudeCliAdapter().invoke(AdapterRequest("x"))


def test_timeout_is_adapter_error(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run(monkeypatch, subprocess.TimeoutExpired(cmd="claude", timeout=1))
    with pytest.raises(AdapterError, match="タイムアウト"):
        ClaudeCliAdapter().invoke(AdapterRequest("x"))


def test_missing_binary_is_adapter_error(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run(monkeypatch, FileNotFoundError("claude"))
    with pytest.raises(AdapterError, match="起動できません"):
        ClaudeCliAdapter().invoke(AdapterRequest("x"))


def test_adapter_errors_are_catchable_as_llm_error() -> None:
    """既存コードの `except LLMError` がそのまま効くこと。"""
    assert issubclass(AdapterError, LLMError)
    assert issubclass(AdapterUnavailable, LLMError)


def test_health_uses_version_not_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    patch_run(monkeypatch, _Completed(stdout="2.1.108"), record)
    assert ClaudeCliAdapter().health() is True
    assert record["args"][0] == ["claude", "--version"]


def test_health_false_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_run(monkeypatch, FileNotFoundError("claude"))
    assert ClaudeCliAdapter().health() is False


# ---------------------------------------------------------------------------
# anthropic_sdk — 未インストール環境で壊れないこと
# ---------------------------------------------------------------------------


def test_sdk_adapter_module_imports_without_sdk() -> None:
    """モジュールのロード自体は SDK 無しでも成功する(ELF全体を壊さない)。"""
    assert get_adapter("anthropic_sdk").name == "anthropic_sdk"


def test_sdk_adapter_fails_at_resolve_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """未インストールなら解決時に `AdapterUnavailable`(絶対ルール2)。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(AdapterUnavailable, match="sdk-anthropic"):
        AnthropicSdkAdapter().invoke(AdapterRequest("x", {"model_env": "ANTHROPIC_MODEL"}))


def test_sdk_adapter_requires_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """実モデル名は環境変数から。params に直書きさせない(絶対ルール13)。"""
    monkeypatch.setattr(
        "llmops.adapters.anthropic_sdk._load_sdk", lambda: pytest.fail("SDK を呼んではいけない")
    )
    with pytest.raises(AdapterUnavailable, match="model_env"):
        AnthropicSdkAdapter()._model({})


def test_sdk_adapter_reports_unset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELF_TEST_MODEL", raising=False)
    with pytest.raises(AdapterUnavailable, match="ELF_TEST_MODEL"):
        AnthropicSdkAdapter()._model({"model_env": "ELF_TEST_MODEL"})


def test_sdk_adapter_target_label_hides_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """span に残るのは環境変数名であって実モデル名ではない。"""
    monkeypatch.setenv("ELF_TEST_MODEL", "some-real-model-name")
    label = AnthropicSdkAdapter()._target_label({"model_env": "ELF_TEST_MODEL"})
    assert label == "anthropic_sdk:$ELF_TEST_MODEL"
    assert "some-real-model-name" not in label
