"""Provider Adapter(SPI)。

CLAUDE.md「モジュール依存の絶対規約」により、本パッケージは gateway / prompt / registry /
db / sdk を import しない。参照してよいのは `adapters/base.py` の SPI と、
共有モジュール(`llmops.errors` / `llmops.logging_utils`)のみ。

Adapter の登録は名前 → クラスの単純な dict。entry point 機構は使わない(依存を増やさない)。
新しい Provider を足すときは、このファイルに1行足すだけで済む(NFR-07)。
"""

from __future__ import annotations

from llmops.adapters.anthropic_sdk import AnthropicSdkAdapter
from llmops.adapters.base import AdapterRequest, AdapterResponse, ProviderAdapter
from llmops.adapters.claude_cli import ClaudeCliAdapter
from llmops.adapters.mock import MockAdapter
from llmops.errors import AdapterUnavailable

ADAPTERS: dict[str, type[ProviderAdapter]] = {
    ClaudeCliAdapter.name: ClaudeCliAdapter,
    AnthropicSdkAdapter.name: AnthropicSdkAdapter,
    MockAdapter.name: MockAdapter,
}


def get_adapter(name: str) -> ProviderAdapter:
    """名前から Adapter を生成する。未知の名前は `AdapterUnavailable`。"""
    try:
        cls = ADAPTERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(ADAPTERS))
        raise AdapterUnavailable(f"未知の adapter です: {name} (利用可能: {known})") from exc
    return cls()


__all__ = [
    "ADAPTERS",
    "AdapterRequest",
    "AdapterResponse",
    "AnthropicSdkAdapter",
    "ClaudeCliAdapter",
    "MockAdapter",
    "ProviderAdapter",
    "get_adapter",
]
