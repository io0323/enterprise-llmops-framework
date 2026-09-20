"""設計 §4 が示す例外の公開パス。実体は `llmops.errors`(理由は同ファイル冒頭)。

利用側・設計書のどちらの経路からでも同じクラスを参照できるようにするための再輸出。
"""

from __future__ import annotations

from llmops.errors import (
    AdapterError,
    AdapterUnavailable,
    AllProvidersFailed,
    EvaluationFailed,
    InvalidTransition,
    LLMBudgetExceeded,
    LLMError,
    LLMOpsError,
    MissingVariable,
    ModelNotFound,
    PolicyViolation,
    PromptError,
    PromptNotFound,
    PromptNotPublished,
    QuotaExceeded,
    ResponseParseError,
    VariableSchemaError,
)

__all__ = [
    "AdapterError",
    "AdapterUnavailable",
    "AllProvidersFailed",
    "EvaluationFailed",
    "InvalidTransition",
    "LLMBudgetExceeded",
    "LLMError",
    "LLMOpsError",
    "MissingVariable",
    "ModelNotFound",
    "PolicyViolation",
    "PromptError",
    "PromptNotFound",
    "PromptNotPublished",
    "QuotaExceeded",
    "ResponseParseError",
    "VariableSchemaError",
]
