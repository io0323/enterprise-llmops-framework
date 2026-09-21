"""Gateway — 全LLM呼び出しの単一通過点(設計 §6)。

処理順は設計 §6 の擬似コードどおり:
Prompt解決 → 変数検証・レンダリング → モデル解決 → Guard → 実行(retry)→ Fallback。

固定してある判断(いずれも CLAUDE.md の絶対ルール):
- span は呼び出しの**前**に INSERT する(ルール5)
- Guard は呼び出しの**前**に判定する(ルール6)
- Fallback は**1段のみ**。`no_fallback=True` を付けて再帰し、連鎖を構造的に防ぐ(ルール7)
- Trace 記録の失敗で処理を止めない(ルール4。Tracer 側で担保)
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

from llmops.adapters import get_adapter
from llmops.adapters._parsing import extract_json, strip_fence
from llmops.adapters.base import AdapterRequest, AdapterResponse, ProviderAdapter
from llmops.config import Config
from llmops.errors import (
    AllProvidersFailed,
    LLMError,
    PolicyViolation,
    PromptNotPublished,
)
from llmops.guard.quota import Guard
from llmops.logging_utils import get_logger
from llmops.models import CompletionRequest, CompletionResult, SpanEnd
from llmops.observability.cost import CostTracker
from llmops.observability.tracer import Tracer
from llmops.prompt.registry import PUBLISHED, PromptRegistry, ResolvedPrompt
from llmops.registry.model_registry import ModelRegistry, ResolvedModel

logger = get_logger(__name__)

ADHOC_TASK = "adhoc"


class Gateway:
    """解決 → Guard → 実行 → 記録。アプリはここより下を知らない。"""

    def __init__(
        self,
        *,
        config: Config,
        prompts: PromptRegistry,
        models: ModelRegistry,
        guard: Guard,
        tracer: Tracer,
        cost: CostTracker,
        system: str | None = None,
    ) -> None:
        self.config = config
        self.prompts = prompts
        self.models = models
        self.guard = guard
        self.tracer = tracer
        self.cost = cost
        self.system = system or config.system
        self._adapters: dict[str, ProviderAdapter] = {}

    # ------------------------------------------------------------------
    def adapter(self, name: str) -> ProviderAdapter:
        """Adapter は使い回す(生成コストを毎回払わない)。"""
        if name not in self._adapters:
            self._adapters[name] = get_adapter(name)
        return self._adapters[name]

    # ------------------------------------------------------------------
    def complete(self, req: CompletionRequest) -> CompletionResult:
        prompt, text, render_hash = self._prepare_text(req)
        model = self.models.resolve(req.model or self._default_model(prompt))

        attempts = req.attempts if req.attempts is not None else self.config.gateway.retry_max

        # Guard は呼び出しの「前」(絶対ルール6)。再試行ぶんもまとめて確保する
        self.guard.check(
            system=self.system,
            trace_id=req.trace_id,
            logical_model=model.logical_name,
            estimated_cost=self.cost.estimate(model, text) * attempts,
            needed_calls=attempts,
        )

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._invoke_once(req, prompt, text, render_hash, model, attempt)
            except Exception as exc:  # noqa: BLE001 - 記録してから再試行 / Fallback
                last_error = exc
                logger.warning(
                    "LLM呼び出し失敗 (task=%s, model=%s, %d/%d): %s",
                    req.task,
                    model.logical_name,
                    attempt,
                    attempts,
                    exc,
                )

        return self._fallback(req, model, last_error)

    # ------------------------------------------------------------------
    def _prepare_text(
        self, req: CompletionRequest
    ) -> tuple[ResolvedPrompt | None, str, str | None]:
        """Registry 管理の Prompt を解決・レンダリングする。adhoc なら素通し。"""
        if req.prompt_id is None:
            if req.text is None:
                raise LLMError("prompt_id も text も指定されていません")
            return None, req.text, None

        prompt = self.prompts.resolve(req.prompt_id, req.version, trace_id=req.trace_id)
        if prompt.status != PUBLISHED:
            self._authorize_unpublished(req, prompt)

        rendered = self.prompts.render(prompt, req.variables)
        return prompt, rendered.text, rendered.hash

    def _authorize_unpublished(self, req: CompletionRequest, prompt: ResolvedPrompt) -> None:
        """published でない版の実行を許すかどうかを決める(Step 3-4)。

        published ゲートを迂回できる経路なので、**呼び出し側の申告では判定しない**。
        許すのは次の2つだけ:

        1. `gateway.allow_unpublished: true`(開発時の設定。本番では false)
        2. **その trace が評価実行のものである**こと。`eval_runs` に実体があるかで
           判定する(`req.allow_unpublished` が True でも、実体が無ければ通さない)

        どちらで通した場合も `audit_logs` に残し、span の meta にも印を付ける。
        「誰が・どの版を・未公開のまま実行したか」を後から数えられるようにするため。
        """
        if self.config.gateway.allow_unpublished:
            logger.warning(
                "gateway.allow_unpublished: true のため未公開版を実行します: %s@%s (%s)",
                prompt.prompt_id, prompt.version, prompt.status,
            )
            self._audit_unpublished(req, prompt, reason="config.allow_unpublished")
            return

        eval_run = self._eval_run_for(req.trace_id)
        if eval_run is not None and str(eval_run["prompt_id"]) == prompt.prompt_id:
            self._audit_unpublished(
                req, prompt, reason="eval_run", run_id=str(eval_run["id"])
            )
            return

        if req.allow_unpublished:
            # 申告はあったが裏付けが無い。**通さない**(これが権限昇格の入口になる)
            raise PolicyViolation(
                f"{prompt.prompt_id}@{prompt.version}: 未公開版の実行が要求されましたが、"
                "この trace に対応する評価実行(eval_runs)がありません。"
                "評価経路かどうかは呼び出し側の申告ではなく DB の実体で判定します"
            )
        raise PromptNotPublished(prompt.prompt_id, prompt.version, prompt.status)

    def _eval_run_for(self, trace_id: str) -> Any | None:
        try:
            return self.tracer.repo.eval_run_for_trace(trace_id)
        except Exception as exc:  # noqa: BLE001 - 判定できないなら「許さない」側に倒す
            logger.warning("評価実行の照合に失敗しました(未公開版は許可しません): %s", exc)
            return None

    def _audit_unpublished(
        self,
        req: CompletionRequest,
        prompt: ResolvedPrompt,
        *,
        reason: str,
        run_id: str | None = None,
    ) -> None:
        try:
            self.tracer.repo.insert_audit_log(
                event="policy.unpublished_execution",
                actor=self.system,
                subject=f"{prompt.prompt_id}@{prompt.version}",
                detail={
                    "status": prompt.status,
                    "reason": reason,
                    "eval_run_id": run_id,
                    "trace_id": req.trace_id,
                    "task": req.task,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 監査の失敗で本処理を止めない
            logger.warning("未公開版実行の監査記録に失敗しました: %s", exc)

    @staticmethod
    def _default_model(prompt: ResolvedPrompt | None) -> str:
        if prompt is None or prompt.default_model is None:
            raise LLMError(
                "論理モデル名が決まりません(呼び出しで model を指定するか、"
                "Prompt の front matter に model: を書く)"
            )
        return prompt.default_model

    # ------------------------------------------------------------------
    def _invoke_once(
        self,
        req: CompletionRequest,
        prompt: ResolvedPrompt | None,
        text: str,
        render_hash: str | None,
        model: ResolvedModel,
        attempt: int,
    ) -> CompletionResult:
        span_id, truncated = self.tracer.start_span(
            trace_id=req.trace_id,
            task=req.task,
            logical_model=model.logical_name,
            adapter=model.adapter,
            request_text=text,
            prompt_id=None if prompt is None else prompt.prompt_id,
            prompt_version=None if prompt is None else prompt.version,
            render_hash=render_hash,
            model_version=model.version,
            parent_span_id=req.parent_span_id,
            attempt=attempt,
        )

        started = time.monotonic()
        try:
            adapter_request = AdapterRequest(text, dict(model.params))
            response = self.adapter(model.adapter).invoke(adapter_request)
            # 既存 cgmp/llm/client.py と同じ解釈:
            # JSON 期待なら extract_json、本文期待なら strip_fence
            parsed = extract_json(response.text) if req.as_json else None
            body = strip_fence(response.text)
            if not req.as_json and not body:
                raise LLMError("LLM応答が空です")
        except Exception as exc:
            self._end_failed(span_id, exc, started)
            raise

        duration_ms = int((time.monotonic() - started) * 1000)
        amount = self.cost.resolve_cost(model, response, text)
        meta: dict[str, Any] = dict(req.meta)
        if prompt is not None and prompt.status != PUBLISHED:
            # `llmops policy check` がここを数える(後から選別できる形で残す)
            meta["unpublished_execution"] = prompt.status
        if truncated:
            meta["truncated"] = True
        if amount.estimated:
            meta["cost_estimated"] = True

        self.tracer.end_span(
            span_id,
            SpanEnd(
                success=True,
                response_text=response.text,
                raw_response_json=_dump_raw(response),
                degraded=req.degraded,
                duration_ms=duration_ms,
                api_duration_ms=response.api_duration_ms,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cache_read_tokens=response.cache_read_tokens,
                cache_write_tokens=response.cache_write_tokens,
                cost_usd=amount.cost_usd,
                num_turns=response.num_turns,
                provider_session=response.session_id,
                meta=meta,
            ),
        )
        self.cost.accrue(
            system=self.system,
            logical_model=model.logical_name,
            cost_usd=amount.cost_usd,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

        return CompletionResult(
            text=body,
            span_id=span_id,
            logical_model=model.logical_name,
            duration_ms=duration_ms,
            prompt_id=None if prompt is None else prompt.prompt_id,
            prompt_version=None if prompt is None else prompt.version,
            render_hash=render_hash,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=amount.cost_usd,
            degraded=req.degraded,
            raw=response.raw,
            parsed=parsed,
        )

    def _end_failed(self, span_id: str, exc: Exception, started: float) -> None:
        self.tracer.end_span(
            span_id,
            SpanEnd(
                success=False,
                error_type=type(exc).__name__,
                error_message=str(exc)[:1000],
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
        )

    # ------------------------------------------------------------------
    def _fallback(
        self, req: CompletionRequest, model: ResolvedModel, last_error: Exception | None
    ) -> CompletionResult:
        """Fallback は1段のみ(絶対ルール7)。`no_fallback=True` を付けて再帰する。"""
        can_fallback = (
            self.config.gateway.fallback_enabled
            and model.fallback_to is not None
            and not req.no_fallback
        )
        if not can_fallback:
            raise AllProvidersFailed(
                f"LLM呼び出しが失敗しました (task={req.task}, model={model.logical_name}): "
                f"{last_error}"
            ) from last_error

        logger.warning(
            "Fallback します: %s → %s (task=%s)", model.logical_name, model.fallback_to, req.task
        )
        return self.complete(
            replace(req, model=model.fallback_to, no_fallback=True, degraded=True)
        )


def _dump_raw(response: AdapterResponse) -> str | None:
    """生応答を JSON 文字列にする。落ちても記録全体を捨てない。"""
    try:
        return json.dumps(response.raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logger.warning("生応答のJSON化に失敗しました(response_text のみ記録します): %s", exc)
        return None
