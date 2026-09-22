"""trace / span の記録(FR-030 / NFR-02)。

**記録の失敗でアプリ処理を止めない**(絶対ルール4)。DB書き込みは全て try/except で
包み、失敗は WARN ログのみにする。観測が本体の可用性を下げるのは本末転倒であるため。
`config.trace.strict`(または `ELF_STRICT_TRACE=1`)のときだけ例外を送出する — テスト用。

**span は LLM 呼び出しの「前」に INSERT する**(絶対ルール5)。プロセスが落ちても
「呼び出そうとした」記録が残るようにするため。
"""

from __future__ import annotations

import uuid
from typing import Any

from llmops.config import Config
from llmops.db.repository import Repository
from llmops.logging_utils import get_logger
from llmops.models import SpanEnd, SpanStart, TraceStart

logger = get_logger(__name__)

TRUNCATION_MARKER = "…"


def new_id() -> str:
    return str(uuid.uuid4())


class Tracer:
    """best-effort な記録係。"""

    def __init__(self, repo: Repository, config: Config, *, system: str | None = None) -> None:
        self.repo = repo
        self.config = config
        self.system = system or config.system

    # ------------------------------------------------------------------
    @property
    def strict(self) -> bool:
        return self.config.trace.strict

    def _guard(self, what: str, action: Any) -> Any:
        """書き込みを包む。失敗は WARN のみ(strict のときだけ送出)。"""
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - 観測の失敗で本処理を止めない
            logger.warning("trace の記録に失敗しました(処理は継続します): %s: %s", what, exc)
            if self.strict:
                raise
            return None

    def _truncate(self, text: str | None) -> tuple[str | None, bool]:
        limit = self.config.trace.max_text_chars
        if text is None or len(text) <= limit:
            return text, False
        return text[:limit] + TRUNCATION_MARKER, True

    # ------------------------------------------------------------------
    # trace
    # ------------------------------------------------------------------
    def start_trace(
        self,
        operation: str,
        *,
        external_id: str | None = None,
        trace_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        trace_id = trace_id or new_id()
        self._guard(
            "start_trace",
            lambda: self.repo.insert_trace(
                TraceStart(
                    id=trace_id,
                    system=self.system,
                    operation=operation,
                    external_id=external_id,
                    meta=meta,
                )
            ),
        )
        return trace_id

    def end_trace(
        self, trace_id: str, *, status: str, meta: dict[str, Any] | None = None
    ) -> None:
        self._guard(
            "end_trace", lambda: self.repo.update_trace(trace_id, status=status, meta=meta)
        )

    # ------------------------------------------------------------------
    # span
    # ------------------------------------------------------------------
    def start_span(
        self,
        *,
        trace_id: str,
        task: str,
        logical_model: str,
        adapter: str,
        request_text: str,
        prompt_id: str | None = None,
        prompt_version: int | None = None,
        render_hash: str | None = None,
        model_version: int | None = None,
        resolved_target: str | None = None,
        parent_span_id: str | None = None,
        attempt: int = 1,
        billable: bool = True,
    ) -> tuple[str, bool]:
        """呼び出し**前**に1行入れる。戻りは (span_id, request_text を切り詰めたか)。"""
        span_id = new_id()
        recorded, truncated = (
            self._truncate(request_text)
            if self.config.trace.record_request_text
            else (None, False)
        )
        seq = self._guard("next_span_seq", lambda: self.repo.next_span_seq(trace_id)) or 1

        self._guard(
            "start_span",
            lambda: self.repo.insert_span(
                SpanStart(
                    id=span_id,
                    trace_id=trace_id,
                    seq=int(seq),
                    task=task,
                    logical_model=logical_model,
                    adapter=adapter,
                    request_text=recorded or "",
                    prompt_id=prompt_id,
                    prompt_version=prompt_version,
                    render_hash=render_hash,
                    model_version=model_version,
                    resolved_target=resolved_target,
                    parent_span_id=parent_span_id,
                    attempt=attempt,
                    billable=billable,
                )
            ),
        )
        return span_id, truncated

    def end_span(self, span_id: str, end: SpanEnd) -> None:
        if self.config.trace.record_response_text:
            text, truncated = self._truncate(end.response_text)
            end.response_text = text
            if truncated:
                end.meta = {**end.meta, "truncated": True}
        else:
            end.response_text = None
        self._guard("end_span", lambda: self.repo.update_span(span_id, end))

    def count_calls(self, trace_id: str) -> int:
        """この trace で既に消費した呼び出し回数(Guard が使う)。"""
        return int(self._guard("count_spans", lambda: self.repo.count_spans(trace_id)) or 0)
