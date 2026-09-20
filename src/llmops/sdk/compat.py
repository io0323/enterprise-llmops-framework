"""既存 `LLMClient` のドロップイン代替(FR-013 / 設計 §5.2)。

移行 Step 1 用。既存2システムの差分を **import 文の変更だけ**にするためのもの。

```python
- from cgmp.llm.client import LLMBudgetExceeded, LLMClient
+ from llmops.sdk.compat import LLMBudgetExceeded, LLMClient
```

提供するもの:
- CGMP 版: `call_json` / `call_text` / `ensure_budget` / `calls_used` / `remaining_calls`
- DDE 版: `call(prompt, task, batch_size)`(NOTES.md N-020)
- 例外: `LLMError` / `LLMBudgetExceeded` を同名で再輸出

既存との差分:
- prompt 文字列を直接受け取るため、span は `prompt_id=NULL` の adhoc として記録される
  (移行 Step 2 で `ops.complete(prompt_id=...)` に置き換わり、版が刻まれるようになる)
- 呼び出し回数上限は ELF の Guard が担う(CGMP 絶対ルール3 / `guard.calls_per_trace_limit`)
- `repo` 引数は**受け取るが使わない**(既存呼び出し側の互換のため)
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from llmops.config import ENV_CONFIG
from llmops.errors import LLMBudgetExceeded, LLMError
from llmops.gateway import Runtime
from llmops.logging_utils import get_logger
from llmops.models import CompletionResult
from llmops.sdk.client import LLMOps

logger = get_logger(__name__)

__all__ = ["LLMBudgetExceeded", "LLMClient", "LLMError"]

#: 既存 config の `llmops:` セクション名(`docs/05_既存システム統合.md` §2.2)
CONFIG_SECTION = "llmops"


def _llmops_section(config: Any) -> dict[str, Any]:
    """アプリ側 Config から `llmops:` セクションを取り出す(取れなくても落とさない)。

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


class LLMClient:
    """既存 `cgmp.llm.client.LLMClient` / `dde.llm.client.LLMClient` の代替。"""

    def __init__(
        self,
        config: Any = None,
        repo: Any = None,
        *,
        system: str,
        ops: LLMOps | None = None,
        model: str | None = None,
    ) -> None:
        del repo  # 既存互換のため受け取るが使わない(記録先は ELF の DB)
        section = _llmops_section(config)
        self.system = system
        # `docs/05_既存システム統合.md` §6: ELF_CONFIG を優先し、config_path は補助
        config_path = None if os.environ.get(ENV_CONFIG) else section.get("config_path")
        self.ops = ops or LLMOps.load(config_path, system=system)
        self.model = model or section.get("model") or self.ops.config.gateway.default_model
        #: request_id → trace_id。呼び出し回数は trace 単位で数える
        self._traces: dict[str | None, str] = {}

    # ------------------------------------------------------------------
    @classmethod
    def from_runtime(
        cls, runtime: Runtime, *, system: str, model: str | None = None
    ) -> LLMClient:
        """テスト用。組み立て済みの Runtime を使う。"""
        return cls(
            None,
            None,
            system=system,
            ops=LLMOps.from_runtime(runtime, system=system),
            model=model,
        )

    @property
    def call_limit(self) -> int:
        return self.ops.config.guard.calls_per_trace_limit

    def _trace_id(self, request_id: str | None) -> str:
        """request_id ごとに1 trace を割り当てる(1記事 = 1 trace)。"""
        if request_id not in self._traces:
            self._traces[request_id] = self.ops.runtime.tracer.start_trace(
                "compat.llm_client", external_id=request_id
            )
        return self._traces[request_id]

    # ------------------------------------------------------------------
    # 呼び出し回数(CGMP 絶対ルール3)
    # ------------------------------------------------------------------
    def calls_used(self, request_id: str) -> int:
        """この request_id で既に消費した呼び出し回数。"""
        return self.ops.runtime.repo.count_spans(self._trace_id(request_id))

    def remaining_calls(self, request_id: str) -> int:
        return max(0, self.call_limit - self.calls_used(request_id))

    def ensure_budget(self, request_id: str, needed: int = 1) -> None:
        """必要回数ぶんの残枠が無ければ中断エラー。

        Raises:
            LLMBudgetExceeded: 残枠不足。
        """
        self.ops.runtime.guard.check(
            system=self.system,
            trace_id=self._trace_id(request_id),
            logical_model=self.model,
            estimated_cost=0.0,
            needed_calls=needed,
        )

    # ------------------------------------------------------------------
    # 呼び出し(CGMP 互換)
    # ------------------------------------------------------------------
    def call_json(
        self, prompt: str, task: str, request_id: str | None = None, section_seq: int | None = None
    ) -> Any:
        """JSON応答を期待する呼び出し。"""
        result = self._call(prompt, task, request_id, section_seq, as_json=True, retry=True)
        return result.json

    def call_text(
        self,
        prompt: str,
        task: str,
        request_id: str | None = None,
        section_seq: int | None = None,
        retry: bool = True,
    ) -> str:
        """Markdown本文を期待する呼び出し。"""
        return self._call(prompt, task, request_id, section_seq, as_json=False, retry=retry).text

    # ------------------------------------------------------------------
    # 呼び出し(DDE 互換)
    # ------------------------------------------------------------------
    def call(self, prompt: str, task: str, batch_size: int = 1) -> Any:
        """プロンプトを投げて本文JSONを返す(DDE 版シグネチャ。NOTES.md N-020)。

        `batch_size` は既存では llm_logs の列だった。ELF では span の meta_json に残す。
        """
        result = self._call(
            prompt, task, None, None, as_json=True, retry=True, batch_size=batch_size
        )
        return result.json

    # ------------------------------------------------------------------
    def _call(
        self,
        prompt: str,
        task: str,
        request_id: str | None,
        section_seq: int | None,
        *,
        as_json: bool,
        retry: bool,
        batch_size: int | None = None,
    ) -> CompletionResult:
        meta: dict[str, Any] = {}
        if section_seq is not None:
            meta["section_seq"] = section_seq
        if batch_size is not None:
            meta["batch_size"] = batch_size
        return self.ops.complete_raw(
            text=prompt,
            model=self.model,
            task=task,
            as_json=as_json,
            retry=retry,
            trace_id=self._trace_id(request_id),
            meta=meta,
        )
