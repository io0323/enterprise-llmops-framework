"""例外階層(`docs/03_詳細設計.md` §10)。

置き場所について: 設計 §4 のモジュール表では `gateway/errors.py` だが、`adapters/*` が
`AdapterError` を送出する必要があり、そこから `gateway` を import すると
モジュール依存の絶対規約(絶対ルール8)に反する。そのため実体を共有モジュールである
本ファイルに置き、`gateway/errors.py` は再輸出のみを行う(NOTES.md N-015)。

**`LLMError` と `LLMBudgetExceeded` は名前を変えない**(既存 DDE / CGMP が捕捉している)。
"""

from __future__ import annotations


class LLMOpsError(RuntimeError):
    """ELF の全例外の基底。

    既存 CGMP / DDE の `LLMError` が `RuntimeError` を継承しており、呼び出し側が
    `except RuntimeError` で受けている可能性があるため、基底を `RuntimeError` にして
    捕捉可能性を維持する(移行で例外が素通りするのを防ぐ)。
    """


# --- 呼び出し失敗 -----------------------------------------------------------


class LLMError(LLMOpsError):
    """LLM 呼び出しの失敗。**既存互換のため名前を変えない。**"""


class AdapterError(LLMError):
    """Adapter 実行失敗(プロセス異常終了・タイムアウト・is_error 応答)。"""


class AdapterUnavailable(AdapterError):
    """Adapter が解決できない(optional extra 未インストール等)。

    設計 §10 に無い追加(NOTES.md N-016)。`anthropic_sdk` を未インストール環境で
    「解決時に」失敗させるために必要。`AdapterError` の下に置くことで、
    既存の `except LLMError` がそのまま効く。
    """


class ResponseParseError(LLMError):
    """JSON / フェンス解析失敗。"""


class AllProvidersFailed(LLMError):
    """retry + fallback が全滅した。"""


# --- 予算・回数上限 ---------------------------------------------------------


class QuotaExceeded(LLMOpsError):
    """予算・回数上限の超過。"""


class LLMBudgetExceeded(QuotaExceeded, LLMError):
    """1トレース(=1記事)あたりの呼び出し上限超過。**既存互換のため名前を変えない。**

    設計 §10 では `QuotaExceeded` の下だけに置かれているが、既存 CGMP では
    `LLMBudgetExceeded(LLMError)` であり、`except LLMError` でも捕捉されていた。
    移行で捕捉漏れを起こさないよう両方を継承する(NOTES.md N-017)。
    """


# --- Prompt -----------------------------------------------------------------


class PromptError(LLMOpsError):
    """Prompt 資産に関する失敗。"""


class PromptNotFound(PromptError):
    pass


class PromptNotPublished(PromptError):
    """published 以外の版を本番実行で使おうとした(FR-021)。"""

    def __init__(self, prompt_id: str, version: int, status: str) -> None:
        super().__init__(
            f"Prompt {prompt_id}@{version} は status={status} のため実行できません "
            "(published のみ実行可。開発時は gateway.allow_unpublished: true)"
        )
        self.prompt_id = prompt_id
        self.version = version
        self.status = status


class MissingVariable(PromptError):
    """テンプレートが要求する変数が渡されていない(空文字で黙って通さない)。"""


class VariableSchemaError(PromptError):
    """変数がスキーマ(front matter の `variables`)に適合しない。"""


class InvalidTransition(PromptError):
    """Prompt 状態機械が許可しない遷移。"""


class PromptMismatch(PromptError):
    """Registry のレンダリング結果が、呼び出し側の想定本文と一致しない。

    設計 §10 に無い追加(NOTES.md N-044)。Prompt の版管理を自前で持つシステム
    (Harness の prompt_versions)が、ELF に登録されていない版を送ろうとしたときに出る。
    **LLM を呼ぶ前**に送出するため、課金も span も発生しない。
    """


# --- Model / Policy / Evaluation --------------------------------------------


class ModelNotFound(LLMOpsError):
    """論理モデル名が `models.yaml` に無い、または status が active でない。"""


class PolicyViolation(LLMOpsError):
    """Policy 違反(Registry 管理外呼び出しの禁止など)。

    設計 §10 に無い追加(NOTES.md N-016)。Step 1-5 の `complete_raw()` が要求する。
    """


class EvaluationFailed(LLMOpsError):
    """publish 時の評価ゲート不合格(Phase 2)。"""
