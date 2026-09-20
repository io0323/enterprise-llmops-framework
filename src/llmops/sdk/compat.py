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

from collections.abc import Mapping
from typing import Any

from llmops.errors import LLMBudgetExceeded, LLMError
from llmops.gateway import Runtime
from llmops.logging_utils import get_logger
from llmops.models import CompletionResult
from llmops.sdk.client import LLMOps, app_config_path, app_config_section

logger = get_logger(__name__)

#: trace の operation 名の既定値。アプリ側は `llmops.operation` か
#: `LLMClient(..., operation=...)` で意味のある名前を付ける
DEFAULT_OPERATION = "compat.llm_client"

__all__ = ["LLMBudgetExceeded", "LLMClient", "LLMError"]

def _app_call_limit(config: Any, section: Mapping[str, Any]) -> int | None:
    """アプリ側が宣言している1記事あたりの呼び出し上限(無ければ None)。

    優先順位は `llmops.calls_per_trace_limit` → 既存 CGMP の
    `config.llm_calls_per_article_limit`。後者は移行対象システムの既存プロパティ名で、
    そこを尊重しないと CGMP 絶対ルール3 の事前検算と shim の判定が食い違う。
    """
    declared = section.get("calls_per_trace_limit")
    if declared is not None:
        return int(declared)
    legacy = getattr(config, "llm_calls_per_article_limit", None)
    return None if legacy is None else int(legacy)


class LLMClient:
    """既存 `cgmp.llm.client.LLMClient` / `dde.llm.client.LLMClient` の代替。"""

    def __init__(
        self,
        config: Any = None,
        repo: Any = None,
        *,
        system: str | None = None,
        ops: LLMOps | None = None,
        model: str | None = None,
        operation: str | None = None,
    ) -> None:
        """
        Args:
            system: 記録先の system 名。省略時はアプリ側 config の `llmops.system` を使う。
                既存呼び出し `LLMClient(config, repo)` をそのまま動かすための逃げ道
                (NFR-04: 差分を import 変更と config 追記のみに保つ)。
                どちらからも決まらない場合は ELF 自身の system になり、
                横断レポートが成立しなくなるので WARN を出す。
        """
        del repo  # 既存互換のため受け取るが使わない(記録先は ELF の DB)
        section = app_config_section(config)
        resolved_system = system or section.get("system")
        self.ops = ops or LLMOps.load(
            app_config_path(section), system=str(resolved_system or "")
        )
        if not resolved_system:
            resolved_system = self.ops.config.system
            logger.warning(
                "LLMClient の system が決まりません。config の llmops.system を設定してください"
                "(暫定で %s として記録します)",
                resolved_system,
            )
        self.system = str(resolved_system)
        self.ops = LLMOps.from_runtime(self.ops.runtime, system=self.system)
        self._call_limit = _app_call_limit(config, section)
        #: trace の operation 名。アプリ側で意味のある名前を付けられるようにする
        #: (既定のままだと「何の処理の trace か」が後から判らない)
        self.operation = operation or str(section.get("operation") or DEFAULT_OPERATION)
        self.model = model or section.get("model") or self.ops.config.gateway.default_model
        #: request_id → trace_id。呼び出し回数は trace 単位で数える
        self._traces: dict[str | None, str] = {}

    # ------------------------------------------------------------------
    @classmethod
    def from_runtime(
        cls,
        runtime: Runtime,
        *,
        system: str,
        model: str | None = None,
        operation: str | None = None,
    ) -> LLMClient:
        """テスト用。組み立て済みの Runtime を使う。"""
        return cls(
            None,
            None,
            system=system,
            ops=LLMOps.from_runtime(runtime, system=system),
            model=model,
            operation=operation,
        )

    @property
    def call_limit(self) -> int:
        """1記事あたりの呼び出し上限。

        **アプリ側の値を優先する。** CGMP は `writer.llm_calls_per_article_limit` を
        自前でも参照して事前検算しており(`pipeline.py` / `outline/generator.py`)、
        shim だけ別の値を使うと両者が食い違う。ELF の `guard.calls_per_trace_limit` は
        Gateway 側で独立に効く(`docs/05_既存システム統合.md` §2.2 の「二重に効く」)。
        """
        if self._call_limit is not None:
            return self._call_limit
        return self.ops.config.guard.calls_limit_for(self.system)

    def _trace_id(self, request_id: str | None) -> str:
        """request_id ごとに1 trace を割り当てる(1記事 = 1 trace)。

        **既にある trace を再利用する。** 移行前の `llm_logs` は request_id 単位で
        行を数えており、プロセスや LLMClient のインスタンスを跨いでも回数が積算された。
        毎回新しい trace を作ると「再開したら枠が戻る」ことになり、
        CGMP 絶対ルール3(1記事10回)が実質無効になる。
        """
        if request_id not in self._traces:
            existing = (
                None
                if request_id is None
                else self.ops.runtime.repo.find_trace_by_external_id(self.system, request_id)
            )
            self._traces[request_id] = (
                str(existing["id"])
                if existing is not None
                else self.ops.runtime.tracer.start_trace(
                    self.operation, external_id=request_id
                )
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

        既存 `cgmp/llm/client.py` の同名メソッドと同じ判定・同じ文面にしてある
        (呼び出し側がメッセージで分岐していなくても、ログの見え方を変えないため)。

        Raises:
            LLMBudgetExceeded: 残枠不足。
        """
        used = self.calls_used(request_id)
        if used + needed > self.call_limit:
            raise LLMBudgetExceeded(
                f"1記事あたりのLLM呼び出し上限({self.call_limit}回)を超えるため中断しました "
                f"(使用済み={used}, 必要={needed}, request_id={request_id})"
            )

    # ------------------------------------------------------------------
    # 呼び出し(CGMP 互換)
    # ------------------------------------------------------------------
    def call_json(
        self, prompt: str, task: str, request_id: str | None = None, section_seq: int | None = None
    ) -> Any:
        """JSON応答を期待する呼び出し。"""
        result = self._call_raw(prompt, task, request_id, section_seq, as_json=True, retry=True)
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
        result = self._call_raw(
            prompt, task, request_id, section_seq, as_json=False, retry=retry
        )
        return result.text

    # ------------------------------------------------------------------
    # Registry 管理の Prompt を使う呼び出し(移行 Step 2 以降)
    #
    # `call_json` / `call_text` は Prompt 文字列を直接受け取る**移行用**の入口で、
    # span に版が刻まれない。こちらは prompt_id を渡すので版・render_hash が残る。
    # 呼び出し回数の管理(CGMP 絶対ルール3)は従来どおり本クラスが担う(NOTES.md N-028)。
    # ------------------------------------------------------------------
    def complete_json(
        self,
        prompt_id: str,
        variables: Mapping[str, Any],
        task: str,
        request_id: str | None = None,
        section_seq: int | None = None,
        retry: bool = True,
        version: int | None = None,
    ) -> Any:
        """JSON応答を期待する呼び出し(`call_json` の Registry 版)。"""
        return self._complete(
            prompt_id, variables, task, request_id, section_seq,
            as_json=True, retry=retry, version=version,
        ).json

    def complete_text(
        self,
        prompt_id: str,
        variables: Mapping[str, Any],
        task: str,
        request_id: str | None = None,
        section_seq: int | None = None,
        retry: bool = True,
        version: int | None = None,
    ) -> str:
        """Markdown本文を期待する呼び出し(`call_text` の Registry 版)。"""
        return self._complete(
            prompt_id, variables, task, request_id, section_seq,
            as_json=False, retry=retry, version=version,
        ).text

    def _complete(
        self,
        prompt_id: str,
        variables: Mapping[str, Any],
        task: str,
        request_id: str | None,
        section_seq: int | None,
        *,
        as_json: bool,
        retry: bool,
        version: int | None,
    ) -> CompletionResult:
        attempts = self.ops.config.gateway.retry_max if retry else 1
        if request_id is not None:
            self.ensure_budget(request_id, needed=attempts)
        meta = {} if section_seq is None else {"section_seq": section_seq}
        return self.ops.complete(
            prompt_id=prompt_id,
            variables=variables,
            task=task,
            as_json=as_json,
            retry=retry,
            version=version,
            trace_id=self._trace_id(request_id),
            meta=meta,
        )

    # ------------------------------------------------------------------
    # 呼び出し(DDE 互換)
    # ------------------------------------------------------------------
    def call(self, prompt: str, task: str, batch_size: int = 1) -> Any:
        """プロンプトを投げて本文JSONを返す(DDE 版シグネチャ。NOTES.md N-020)。

        `batch_size` は既存では llm_logs の列だった。ELF では span の meta_json に残す。
        """
        result = self._call_raw(
            prompt, task, None, None, as_json=True, retry=True, batch_size=batch_size
        )
        return result.json

    # ------------------------------------------------------------------
    def _call_raw(
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
        # 既存 `cgmp/llm/client.py::_call` と同じく、呼び出し**前**に残枠を確認する。
        # 再試行ぶんもまとめて確保するのも既存と同じ(捨てるトークンを増やさないため)。
        attempts = self.ops.config.gateway.retry_max if retry else 1
        if request_id is not None:
            self.ensure_budget(request_id, needed=attempts)

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
