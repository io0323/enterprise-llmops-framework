"""Provider Adapter SPI(`docs/03_詳細設計.md` §5.3)。

Adapter は **Registry / DB / Gateway を一切 import しない**(絶対ルール8)。
参照してよいのは本モジュールと `llmops.errors` / `llmops.logging_utils` だけ。
テスト時に単体で差し替えられることを保証するための制約。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterRequest:
    text: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResponse:
    text: str
    raw: dict[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    api_duration_ms: int | None = None
    num_turns: int | None = None
    session_id: str | None = None
    resolved_target: str | None = None


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    def invoke(self, req: AdapterRequest) -> AdapterResponse: ...

    def health(self) -> bool:
        return True
