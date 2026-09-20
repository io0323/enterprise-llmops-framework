"""`claude -p --output-format json` を subprocess で呼ぶ Adapter(設計 §5.4)。

既存 DDE / CGMP が捨てていた `total_cost_usd` / `usage` / `session_id` / `num_turns` を
すべて拾う(FR-031)。ただし **必須は `result` のみ**。他は `.get()` で欠損を許容し、
欠損時は WARN ログを出して処理を継続する(絶対ルール9 / NOTES.md N-007)。
CLI のバージョン差で全システムが止まる事態を作らないため。

`raw` には応答を丸ごと入れる。構造化カラムのパースに失敗しても、生データから
後で再集計できるようにするため(設計 §2.1)。
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from llmops.adapters.base import AdapterRequest, AdapterResponse, ProviderAdapter
from llmops.errors import AdapterError
from llmops.logging_utils import get_logger

logger = get_logger(__name__)

DEFAULT_COMMAND = ["claude", "-p", "--output-format", "json"]
DEFAULT_TIMEOUT_SEC = 600

#: 欠損していたら WARN を出すフィールド(欠損しても処理は続ける)
_EXPECTED_FIELDS = ("total_cost_usd", "usage", "session_id", "num_turns")


class ClaudeCliAdapter(ProviderAdapter):
    name = "claude_cli"

    def invoke(self, req: AdapterRequest) -> AdapterResponse:
        command = [str(c) for c in req.params.get("command", DEFAULT_COMMAND)]
        timeout_sec = int(req.params.get("timeout_sec", DEFAULT_TIMEOUT_SEC))

        try:
            completed = subprocess.run(
                command,
                input=req.text,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterError(f"claude コマンドがタイムアウトしました ({timeout_sec}秒)") from exc
        except OSError as exc:
            raise AdapterError(f"claude コマンドを起動できませんでした: {exc}") from exc

        # 既存実装の判定を踏襲する: 非ゼロ終了は失敗
        if completed.returncode != 0:
            raise AdapterError(
                f"claude コマンドが異常終了しました (code={completed.returncode}): "
                f"{completed.stderr[:200]}"
            )

        payload = self._parse_payload(completed.stdout)

        if payload.get("is_error") is True:
            raise AdapterError(
                f"claude コマンドが is_error を返しました: {str(payload.get('result'))[:200]}"
            )

        if "result" not in payload:
            raise AdapterError("claude の応答に必須フィールド `result` がありません")

        self._warn_missing(payload)
        usage: dict[str, Any] = payload.get("usage") or {}

        return AdapterResponse(
            text=str(payload["result"]),
            raw=payload,
            cost_usd=payload.get("total_cost_usd"),
            api_duration_ms=payload.get("duration_api_ms") or payload.get("duration_ms"),
            num_turns=payload.get("num_turns"),
            session_id=payload.get("session_id"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_write_tokens=usage.get("cache_creation_input_tokens"),
            resolved_target=" ".join(command),
        )

    @staticmethod
    def _parse_payload(stdout: str) -> dict[str, Any]:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"claude の応答がJSONではありません: {stdout[:200]}") from exc
        if not isinstance(payload, dict):
            raise AdapterError(f"claude の応答がオブジェクトではありません: {stdout[:200]}")
        return payload

    @staticmethod
    def _warn_missing(payload: dict[str, Any]) -> None:
        """欠損フィールドを記録する。**処理は止めない**(絶対ルール9)。"""
        missing = [name for name in _EXPECTED_FIELDS if payload.get(name) is None]
        if missing:
            logger.warning(
                "claude の応答に想定フィールドがありません(処理は継続します): %s",
                ", ".join(missing),
            )

    def health(self) -> bool:
        """疎通確認。`claude --version` が 0 で終わるかだけを見る(LLMは呼ばない)。"""
        try:
            completed = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0
