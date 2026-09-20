"""Hard Quota — 呼び出し**前**の判定(絶対ルール6 / FR-034)。

事後判定では予算超過を止められない。よって Gateway は Adapter を呼ぶ前に必ずここを通す。

止めるものは2つ:
1. trace 単位の呼び出し回数上限(CGMP 絶対ルール3 を ELF 側で強制)→ `LLMBudgetExceeded`
2. 月次予算(`budgets` と `cost_daily` の当月合計)→ `QuotaExceeded`

`hard_quota: false` のときは WARN ログのみで通す(記録はする)。
"""

from __future__ import annotations

from dataclasses import dataclass

from llmops.config import Config
from llmops.db.repository import Repository
from llmops.errors import LLMBudgetExceeded, QuotaExceeded
from llmops.logging_utils import get_logger
from llmops.observability.cost import CostTracker

logger = get_logger(__name__)

GLOBAL_BUDGET_ID = "global"


@dataclass(frozen=True)
class BudgetState:
    budget_id: str
    limit_usd: float
    used_usd: float
    hard_limit: bool

    @property
    def remaining(self) -> float:
        return self.limit_usd - self.used_usd

    @property
    def percent(self) -> float:
        return 0.0 if self.limit_usd <= 0 else self.used_usd / self.limit_usd * 100


class Guard:
    """呼び出し前の可否判定。"""

    def __init__(self, repo: Repository, config: Config, cost: CostTracker) -> None:
        self.repo = repo
        self.config = config
        self.cost = cost

    # ------------------------------------------------------------------
    def check(
        self,
        *,
        system: str,
        trace_id: str | None,
        logical_model: str,
        estimated_cost: float,
        needed_calls: int = 1,
    ) -> None:
        """拒否すべきなら例外を送出する。Adapter を呼ぶ前に必ず通す。"""
        if trace_id is not None:
            self._check_calls(system=system, trace_id=trace_id, needed=needed_calls)
        self._check_budget(
            system=system, logical_model=logical_model, estimated_cost=estimated_cost
        )

    # ------------------------------------------------------------------
    def _check_calls(self, *, system: str, trace_id: str, needed: int) -> None:
        limit = self.config.guard.calls_limit_for(system)
        if limit <= 0:
            return  # 0 は無制限(記事単位の上限を持たないシステム向け。N-023)
        used = self.repo.count_spans(trace_id)
        if used + needed <= limit:
            return

        message = (
            f"1トレースあたりのLLM呼び出し上限({limit}回)を超えるため中断しました "
            f"(使用済み={used}, 必要={needed}, trace_id={trace_id})"
        )
        self._audit("quota.exceeded", system, {"kind": "calls_per_trace", "used": used,
                                               "limit": limit, "trace_id": trace_id})
        if not self.config.guard.hard_quota:
            logger.warning("%s(hard_quota: false のため継続します)", message)
            return
        raise LLMBudgetExceeded(message)

    def _check_budget(self, *, system: str, logical_model: str, estimated_cost: float) -> None:
        for state in self.budget_states(system):
            self._warn_thresholds(state)
            if state.used_usd + estimated_cost <= state.limit_usd:
                continue

            message = (
                f"予算上限を超えるため中断しました (budget={state.budget_id}, "
                f"上限={state.limit_usd:.4f} USD, 使用済み={state.used_usd:.4f} USD, "
                f"今回の見積り={estimated_cost:.4f} USD, model={logical_model})"
            )
            self._audit(
                "quota.exceeded",
                system,
                {
                    "kind": "budget",
                    "budget": state.budget_id,
                    "limit_usd": state.limit_usd,
                    "used_usd": state.used_usd,
                    "estimated_usd": estimated_cost,
                },
            )
            if not (state.hard_limit and self.config.guard.hard_quota):
                logger.warning("%s(hard_limit が無効のため継続します)", message)
                continue
            raise QuotaExceeded(message)

    # ------------------------------------------------------------------
    def budget_states(self, system: str) -> list[BudgetState]:
        """効いている予算の一覧(global + system 別)。"""
        states: list[BudgetState] = []

        global_row = self.repo.get_budget(GLOBAL_BUDGET_ID)
        if global_row is None:
            # budgets に行が無ければ config.yaml の値を使う(既定で予算が効く状態にする)
            limit = self.config.budget.global_monthly_usd
            if limit > 0:
                states.append(
                    BudgetState(
                        budget_id=GLOBAL_BUDGET_ID,
                        limit_usd=limit,
                        used_usd=self.cost.month_total(),
                        hard_limit=self.config.guard.hard_quota,
                    )
                )
        elif int(global_row["enabled"]):
            states.append(
                BudgetState(
                    budget_id=GLOBAL_BUDGET_ID,
                    limit_usd=float(global_row["limit_usd"]),
                    used_usd=self.cost.month_total(),
                    hard_limit=bool(int(global_row["hard_limit"])),
                )
            )

        system_row = self.repo.get_budget(system)
        if system_row is not None and int(system_row["enabled"]):
            states.append(
                BudgetState(
                    budget_id=system,
                    limit_usd=float(system_row["limit_usd"]),
                    used_usd=self.cost.month_total(system=system),
                    hard_limit=bool(int(system_row["hard_limit"])),
                )
            )
        return states

    def _warn_thresholds(self, state: BudgetState) -> None:
        for percent in sorted(self.config.budget.warn_percents):
            if state.percent >= percent:
                logger.warning(
                    "予算の %d%% に到達しています (budget=%s, 使用済み=%.4f / 上限=%.4f USD)",
                    percent,
                    state.budget_id,
                    state.used_usd,
                    state.limit_usd,
                )

    def _audit(self, event: str, system: str, detail: dict[str, object]) -> None:
        try:
            self.repo.insert_audit_log(event=event, actor=system, subject=system, detail=detail)
        except Exception as exc:  # noqa: BLE001 - 監査記録の失敗で判定を止めない
            logger.warning("監査ログの記録に失敗しました: %s", exc)
