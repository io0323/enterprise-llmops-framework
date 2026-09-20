"""利用システムが import する唯一の公開API(設計 §5.1)。

```python
from llmops.sdk import LLMOps

ops = LLMOps.load(system="cgmp")
with ops.trace("article.generate", external_id=request_id):
    outline = ops.complete(prompt_id="cgmp.outline", variables={...}, as_json=True)
```
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

from llmops.config import ENV_CONFIG, Config
from llmops.errors import PolicyViolation
from llmops.gateway import Runtime
from llmops.logging_utils import get_logger
from llmops.models import CompletionRequest, CompletionResult
from llmops.prompt.registry import EvalGate

logger = get_logger(__name__)

SUCCESS = "success"
FAILED = "failed"

#: 利用システム側 config.yaml に置く ELF 設定のセクション名(docs/05 §2.2)
CONFIG_SECTION = "llmops"


def app_config_section(config: Any) -> dict[str, Any]:
    """利用システムの Config から `llmops:` セクションを取り出す(取れなくても落とさない)。

    CGMP / DDE の Config は実装が違う(`raw` 辞書を持つもの・属性のもの)。
    どちらでも動くように順に試し、見つからなければ空とする。
    """
    candidates: list[Any] = []
    for attr in ("raw", None):
        holder = getattr(config, attr, None) if attr else config
        if holder is None:
            continue
        getter = getattr(holder, "get", None)
        if callable(getter):
            with contextlib.suppress(KeyError, TypeError):
                candidates.append(getter(CONFIG_SECTION))
        candidates.append(getattr(holder, CONFIG_SECTION, None))

    for section in candidates:
        if isinstance(section, dict):
            return dict(section)
    return {}


def app_config_path(section: Mapping[str, Any]) -> str | None:
    """ELF 設定のパス。`ELF_CONFIG` を優先し、アプリ側の `config_path` は補助(docs/05 §6)。"""
    if os.environ.get(ENV_CONFIG):
        return None
    path = section.get("config_path")
    return None if path is None else str(path)


class TraceContext:
    """`with` で使う。抜けると `finished_at` と status が確定する(設計 §5.1)。"""

    def __init__(self, ops: LLMOps, trace_id: str, operation: str) -> None:
        self.ops = ops
        self.trace_id = trace_id
        self.operation = operation
        self.status = SUCCESS

    def __enter__(self) -> TraceContext:
        self.ops._push(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.ops._pop(self)
        status = FAILED if exc_type is not None else self.status
        self.ops.runtime.tracer.end_trace(self.trace_id, status=status)

    def fail(self) -> None:
        """例外を送出せずに失敗として閉じたい場合(部分失敗の記録)。"""
        self.status = FAILED

    def partial(self) -> None:
        self.status = "partial"


class LLMOps:
    """利用システムが持つ唯一の ELF ハンドル。"""

    def __init__(self, runtime: Runtime, system: str) -> None:
        self.runtime = runtime
        self.system = system
        self._stack: list[TraceContext] = []

    # ------------------------------------------------------------------
    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        *,
        system: str,
        eval_gate: EvalGate | None = None,
    ) -> LLMOps:
        runtime = Runtime.load(config_path, system=system, eval_gate=eval_gate)
        return cls(runtime, system)

    @classmethod
    def for_app(cls, app_config: Any, *, system: str, eval_gate: EvalGate | None = None) -> LLMOps:
        """利用システムの Config(`llmops:` セクション付き)から組み立てる。

        DDE / CGMP の呼び出し側が ELF の設定探索を自前で書かずに済むようにするための入口。
        """
        section = app_config_section(app_config)
        return cls.load(app_config_path(section), system=system, eval_gate=eval_gate)

    @classmethod
    def from_runtime(cls, runtime: Runtime, *, system: str | None = None) -> LLMOps:
        """組み立て済みの Runtime を使う。

        `system` を渡した場合は Tracer / Gateway にも反映する。ここを忘れると
        `traces.system` や `cost_daily.system` が ELF 自身の名前で記録され、
        横断レポート(`report cost --by system`)が意味をなさなくなる。
        """
        if system is not None and system != runtime.gateway.system:
            runtime.gateway.system = system
            runtime.tracer.system = system
        return cls(runtime, system or runtime.gateway.system)

    @property
    def config(self) -> Config:
        return self.runtime.config

    def close(self) -> None:
        self.runtime.close()

    # ------------------------------------------------------------------
    # trace
    # ------------------------------------------------------------------
    def trace(
        self, operation: str, external_id: str | None = None, **meta: Any
    ) -> TraceContext:
        trace_id = self.runtime.tracer.start_trace(
            operation, external_id=external_id, meta=meta or None
        )
        return TraceContext(self, trace_id, operation)

    def _push(self, ctx: TraceContext) -> None:
        self._stack.append(ctx)

    def _pop(self, ctx: TraceContext) -> None:
        if ctx in self._stack:
            self._stack.remove(ctx)

    @property
    def current_trace_id(self) -> str | None:
        return self._stack[-1].trace_id if self._stack else None

    def _trace_id_for(self, trace_id: str | None) -> str:
        """明示指定 → 進行中の trace → その場で1件作る、の順に決める。"""
        if trace_id is not None:
            return trace_id
        current = self.current_trace_id
        if current is not None:
            return current
        return self.runtime.tracer.start_trace("adhoc")

    def spans(self, trace_id: str) -> Iterator[Any]:
        return iter(self.runtime.repo.list_spans(trace_id))

    # ------------------------------------------------------------------
    # 実行
    # ------------------------------------------------------------------
    def complete(
        self,
        *,
        prompt_id: str,
        variables: Mapping[str, Any],
        model: str | None = None,
        task: str | None = None,
        version: int | None = None,
        as_json: bool = False,
        retry: bool | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> CompletionResult:
        """Registry 管理の Prompt を実行する。"""
        return self.runtime.gateway.complete(
            CompletionRequest(
                trace_id=self._trace_id_for(trace_id),
                task=task or prompt_id.split(".")[-1],
                prompt_id=prompt_id,
                variables=dict(variables),
                model=model,
                version=version,
                as_json=as_json,
                attempts=None if retry is not False else 1,
                parent_span_id=parent_span_id,
                meta=dict(meta or {}),
            )
        )

    def complete_raw(
        self,
        *,
        text: str,
        model: str,
        task: str,
        as_json: bool = False,
        retry: bool | None = None,
        trace_id: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> CompletionResult:
        """Registry 管理外のアドホック呼び出し。

        既定では**無効**。`gateway.allow_raw_completion: true` が必要で、移行期間のみ
        有効にする(設計 §5.1)。使用は監査ログに残る。
        """
        if not self.config.gateway.allow_raw_completion:
            self.runtime.repo.insert_audit_log(
                event="policy.violation",
                actor=self.system,
                subject=task,
                detail={"kind": "raw_completion", "model": model},
            )
            raise PolicyViolation(
                "Registry 管理外の呼び出し(complete_raw)は無効です "
                "(移行期間のみ gateway.allow_raw_completion: true にする)"
            )

        self.runtime.repo.insert_audit_log(
            event="raw_completion",
            actor=self.system,
            subject=task,
            detail={"model": model, "chars": len(text)},
        )
        return self.runtime.gateway.complete(
            CompletionRequest(
                trace_id=self._trace_id_for(trace_id),
                task=task,
                text=text,
                model=model,
                as_json=as_json,
                attempts=None if retry is not False else 1,
                meta=dict(meta or {}),
            )
        )
