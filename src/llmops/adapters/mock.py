"""テスト・動作確認用の決定的 Adapter。

乱数・時刻・外部 I/O を使わない。同じ入力からは必ず同じ出力が出る
(テストが揺れると回帰検出の役に立たないため)。
"""

from __future__ import annotations

import json
from typing import Any

from llmops.adapters.base import AdapterRequest, AdapterResponse, ProviderAdapter
from llmops.errors import AdapterError

#: 文字数からトークン数を概算する係数。実測値ではなく、テストが決定的になればよい
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


class MockAdapter(ProviderAdapter):
    """`mode` で挙動を切り替える。

    - ``echo``(既定): 入力をそのまま返す
    - ``json``: `params.payload`(既定は空オブジェクト)を JSON 文字列で返す
    - ``fail``: 必ず `AdapterError`。Fallback / retry のテスト用
    """

    name = "mock"

    def invoke(self, req: AdapterRequest) -> AdapterResponse:
        mode = str(req.params.get("mode", "echo"))
        if mode == "fail":
            raise AdapterError(f"mock adapter は mode=fail のため失敗しました (name={self.name})")
        if mode == "json":
            payload: Any = req.params.get("payload", {})
            text = json.dumps(payload, ensure_ascii=False)
        elif mode == "echo":
            text = req.text
        else:
            raise AdapterError(f"mock adapter の未知の mode です: {mode}")

        return AdapterResponse(
            text=text,
            raw={"adapter": self.name, "mode": mode, "result": text},
            input_tokens=estimate_tokens(req.text),
            output_tokens=estimate_tokens(text),
            cost_usd=0.0,
            api_duration_ms=0,
            num_turns=1,
            resolved_target=f"{self.name}:{mode}",
        )
