"""LLM-as-Judge。

**Judge のスコアは絶対値として信用しない。版間の相対比較にのみ使う。**
これはレポート出力にも明記する(過信の防止。`docs/impl/phase2_evaluation.md` Step 2-2)。

安全側に倒す設計(NOTES.md N-026 の再発防止):

- **スキーマ検証まで通って初めて「採点成功」**とする。`extract_json` は Prompt 内の
  出力例 JSON を拾いうるので(N-026 の直接原因)、「JSONとして読めた」だけでは足りない。
  `score` が数値で 0..1 に収まり、`reason` が空でないことまで確認する
- 採点できなかったケースは `error` として記録し、**平均から除外する**。
  0点として扱わない(「採点していない」と「0点」は別物)
- Judge が落ちても run は止めない。ただし除外件数はレポートに必ず出す
- 代替モデルで穴埋めしない。誰が採点したか分からないスコアは使えない
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from llmops.errors import LLMOpsError
from llmops.logging_utils import get_logger

logger = get_logger(__name__)

SCORE_MIN = 0.0
SCORE_MAX = 1.0


class JudgeResponseError(LLMOpsError):
    """Judge の応答が期待スキーマに適合しない(= 採点失敗)。"""


@dataclass(frozen=True)
class JudgeVerdict:
    """Judge 1回ぶんの結果。"""

    metric: str
    score: float
    reason: str
    violations: list[str]
    span_id: str
    prompt_id: str
    prompt_version: int | None


def parse_judge_payload(payload: Any, *, metric: str) -> tuple[float, str, list[str]]:
    """Judge の応答を検証する。**スキーマに合わなければ例外**(スコアを作らない)。

    Raises:
        JudgeResponseError: 形が違う / score が数値でない / 範囲外 / reason が空。
    """
    if isinstance(payload, list):
        # 「オブジェクトを配列で包む」応答は実機で頻出するので1件だけ許す。
        # 2件以上は「どれが採点か」決められないので失敗にする
        if len(payload) != 1:
            raise JudgeResponseError(
                f"{metric}: 配列で {len(payload)} 件返っており、採点を1つに決められません"
            )
        payload = payload[0]

    if not isinstance(payload, dict):
        raise JudgeResponseError(f"{metric}: オブジェクトではありません: {type(payload).__name__}")

    if "score" not in payload:
        raise JudgeResponseError(f"{metric}: score がありません(拾った JSON: {sorted(payload)})")

    raw_score = payload["score"]
    if isinstance(raw_score, bool) or not isinstance(raw_score, int | float):
        raise JudgeResponseError(f"{metric}: score が数値ではありません: {raw_score!r}")
    score = float(raw_score)
    if not SCORE_MIN <= score <= SCORE_MAX:
        raise JudgeResponseError(f"{metric}: score が 0.0-1.0 の範囲外です: {score}")

    reason = str(payload.get("reason") or "").strip()
    if not reason:
        # 理由の無いスコアは改善に使えない(Step 2-2)。採点失敗として扱う
        raise JudgeResponseError(f"{metric}: reason が空です(理由の無いスコアは採用しない)")

    raw_violations = payload.get("violations") or []
    violations = [str(v) for v in raw_violations] if isinstance(raw_violations, list) else []
    return score, reason, violations


class Judge:
    """1指標ぶんの採点を行う。"""

    def __init__(self, ops: Any, *, model: str) -> None:
        self.ops = ops
        self.model = model

    def score(
        self,
        *,
        metric: str,
        prompt_id: str,
        variables: Mapping[str, Any],
        trace_id: str,
    ) -> JudgeVerdict:
        """採点する。失敗は `JudgeResponseError` / `LLMOpsError` で送出する。

        **代替モデルへ逃がさない。** 呼び出し側はこれを捕まえて `error` として記録する。
        """
        result = self.ops.complete(
            prompt_id=prompt_id,
            variables=dict(variables),
            model=self.model,
            task=f"judge.{metric}",
            as_json=True,
            trace_id=trace_id,
        )
        score, reason, violations = parse_judge_payload(result.json, metric=metric)
        return JudgeVerdict(
            metric=metric,
            score=score,
            reason=reason,
            violations=violations,
            span_id=result.span_id,
            prompt_id=result.prompt_id or prompt_id,
            prompt_version=result.prompt_version,
        )


def weighted_average(
    scores: Mapping[str, float], weights: Mapping[str, float]
) -> float | None:
    """採点できた指標だけで加重平均を取る。

    欠損(採点失敗)は**分母からも除く**。0点として平均を押し下げない。
    1つも採点できなければ None を返す(スコア欠損)。
    """
    usable = [(scores[m], weights.get(m, 1.0)) for m in scores]
    total_weight = sum(weight for _, weight in usable)
    if not usable or total_weight <= 0:
        return None
    return sum(score * weight for score, weight in usable) / total_weight
