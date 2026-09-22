"""コスト集計(FR-031)。

Provider が実コストを返すならそれを正とする(`claude -p` の `total_cost_usd`)。
返さない場合だけ `models.yaml` の単価 × トークン数で概算し、概算であることを
span の meta に刻む。「記録された数字が実測か概算か」を後から判別できないと、
コストレポートが信用できなくなるため。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from llmops.adapters.base import AdapterResponse
from llmops.db.repository import Repository
from llmops.logging_utils import get_logger
from llmops.registry.model_registry import ResolvedModel

logger = get_logger(__name__)

#: 単価から概算するときの文字数→トークン換算。実測値ではなく桁を合わせるための係数
CHARS_PER_TOKEN = 4


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def this_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class CostAmount:
    cost_usd: float
    estimated: bool


class CostTracker:
    """span 確定時に `cost_daily` を UPSERT する。"""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    @staticmethod
    def price_of(model: ResolvedModel, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1000 * model.input_price_per_1k
            + output_tokens / 1000 * model.output_price_per_1k
        )

    def estimate(self, model: ResolvedModel, text: str) -> float:
        """呼び出し**前**の見積り。Guard が予算判定に使う(絶対ルール6)。

        出力トークンは未知なので入力と同数と仮置きする。予算判定は過小評価より
        過大評価のほうが安全なため、切り上げ側に倒している。
        """
        tokens = estimate_tokens(text)
        return self.price_of(model, tokens, tokens)

    def resolve_cost(
        self, model: ResolvedModel, response: AdapterResponse, request_text: str
    ) -> CostAmount:
        if response.cost_usd is not None:
            return CostAmount(float(response.cost_usd), estimated=False)
        input_tokens = response.input_tokens or estimate_tokens(request_text)
        output_tokens = response.output_tokens or estimate_tokens(response.text)
        return CostAmount(self.price_of(model, input_tokens, output_tokens), estimated=True)

    def accrue(
        self,
        *,
        system: str,
        logical_model: str,
        cost_usd: float,
        input_tokens: int | None,
        output_tokens: int | None,
        day: str | None = None,
        billable: bool = True,
    ) -> None:
        """日次集計に加算する。失敗しても本処理は止めない(観測と同じ扱い)。

        `billable=False`(サブスク換算)も**記録はする**。可視性を落とさず、
        予算判定の対象からだけ外す(NOTES.md N-045)。
        """
        try:
            self.repo.upsert_cost_daily(
                day=day or today(),
                system=system,
                logical_model=logical_model,
                input_tokens=input_tokens or 0,
                output_tokens=output_tokens or 0,
                cost_usd=cost_usd,
                billable=billable,
            )
        except Exception as exc:  # noqa: BLE001 - 集計失敗で呼び出し結果を捨てない
            logger.warning("cost_daily の更新に失敗しました(処理は継続します): %s", exc)

    def month_total(
        self,
        *,
        system: str | None = None,
        month: str | None = None,
        billable_only: bool = True,
    ) -> float:
        """当月コスト。既定は実課金ぶんだけ(予算が見るのはこちら)。"""
        return self.repo.month_cost(
            month or this_month(), system=system, billable_only=billable_only
        )
