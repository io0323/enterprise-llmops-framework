"""ELF 内部で受け渡す DTO(dataclass)。

設定ロードのみ pydantic、それ以外は dataclass(CLAUDE.md コーディング規約)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Observability(db/repository.py が読み書きする形)
# ---------------------------------------------------------------------------


@dataclass
class TraceStart:
    """`traces` へ INSERT する内容。"""

    id: str
    system: str
    operation: str
    external_id: str | None = None
    meta: dict[str, Any] | None = None


@dataclass
class SpanStart:
    """`spans` へ呼び出し**前**に INSERT する内容(絶対ルール5)。

    プロセスが落ちても「呼び出そうとした」記録が残るよう、応答に依存する列は持たない。
    """

    id: str
    trace_id: str
    seq: int
    task: str
    logical_model: str
    adapter: str
    request_text: str
    parent_span_id: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    render_hash: str | None = None
    model_version: int | None = None
    resolved_target: str | None = None
    attempt: int = 1


@dataclass
class SpanEnd:
    """`spans` を呼び出し後に UPDATE する内容。"""

    success: bool
    response_text: str | None = None
    raw_response_json: str | None = None
    degraded: bool = False
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    api_duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    provider_session: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Prompt Registry
# ---------------------------------------------------------------------------


@dataclass
class PromptVersionRow:
    """`prompt_versions` の1行。"""

    prompt_id: str
    version: int
    status: str
    body: str
    front_matter: str
    content_hash: str
    source_path: str
    var_schema: str | None = None
    owner: str | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# Model Registry
# ---------------------------------------------------------------------------


@dataclass
class ModelVersionRow:
    """`model_versions` の1行。"""

    logical_name: str
    version: int
    adapter: str
    params_json: str
    config_hash: str
    price_json: str | None = None
    fallback_to: str | None = None
    status: str = "active"


# ---------------------------------------------------------------------------
# Gateway の入出力(設計 §5.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletionRequest:
    """Gateway への1リクエスト。`prompt_id` が None のときは adhoc(Registry 管理外)。"""

    trace_id: str
    task: str
    prompt_id: str | None = None
    variables: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    version: int | None = None
    as_json: bool = False
    no_fallback: bool = False
    degraded: bool = False
    text: str | None = None
    parent_span_id: str | None = None
    attempts: int | None = None


@dataclass(frozen=True)
class CompletionResult:
    """Gateway の戻り(設計 §5.1)。"""

    text: str
    span_id: str
    logical_model: str
    duration_ms: int
    prompt_id: str | None = None
    prompt_version: int | None = None
    render_hash: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    degraded: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    parsed: Any = None

    @property
    def json(self) -> Any:
        """`as_json=True` で呼んだときのパース結果(設計 §5.1 の利用例に合わせる)。"""
        return self.parsed
