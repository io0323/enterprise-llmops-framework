"""段階展開(canary / promote / rollback)— Step 3-1。

`prompt_deployments` の1行を書き換えるだけで本番が切り替わる(FR-050)。
版の内容は動かさないので、rollback は「ポインタを戻す」だけで済む。

**自動昇格は実装しない。** 判定材料を出すところまでにして、昇格は人間が叩く
(CGMP 絶対ルール2「自動公開の実装禁止」と同じ思想。実装指示 Step 3-1)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from llmops.errors import InvalidTransition, PromptError
from llmops.logging_utils import get_logger
from llmops.prompt.registry import PUBLISHED, PromptRegistry, ResolvedPrompt

logger = get_logger(__name__)


@dataclass(frozen=True)
class DeploymentState:
    """いまの公開状態。"""

    prompt_id: str
    active_version: int
    canary_version: int | None = None
    canary_percent: int = 0
    updated_at: str | None = None
    updated_by: str | None = None

    @property
    def has_canary(self) -> bool:
        return self.canary_version is not None and self.canary_percent > 0


@dataclass
class CanaryComparison:
    """active 版と canary 版の実績比較(昇格判断の材料)。"""

    prompt_id: str
    active_version: int
    canary_version: int
    active: dict[str, Any] = field(default_factory=dict)
    canary: dict[str, Any] = field(default_factory=dict)

    @property
    def enough_samples(self) -> bool:
        """両方に実績があるか。片方0件では比較にならない。"""
        return bool(self.active.get("calls")) and bool(self.canary.get("calls"))


class Deployer:
    """publish 済みの版の間で、どれを本番に出すかを切り替える。"""

    def __init__(self, repo: Any, registry: PromptRegistry) -> None:
        self.repo = repo
        self.registry = registry

    # ------------------------------------------------------------------
    def state(self, prompt_id: str) -> DeploymentState | None:
        row = self.repo.get_deployment(prompt_id)
        if row is None:
            return None
        return DeploymentState(
            prompt_id=prompt_id,
            active_version=int(row["active_version"]),
            canary_version=None if row["canary_version"] is None else int(row["canary_version"]),
            canary_percent=int(row["canary_percent"] or 0),
            updated_at=None if row["updated_at"] is None else str(row["updated_at"]),
            updated_by=None if row["updated_by"] is None else str(row["updated_by"]),
        )

    # ------------------------------------------------------------------
    def canary(
        self, prompt_id: str, *, version: int, percent: int, actor: str | None = None
    ) -> DeploymentState:
        """新版を割合指定で段階適用する(FR-051)。"""
        self.registry.canary(prompt_id, version=version, percent=percent, actor=actor)
        state = self.state(prompt_id)
        assert state is not None  # canary() が成功した時点で必ずある
        return state

    def promote(self, prompt_id: str, *, actor: str | None = None) -> DeploymentState:
        """canary を active に昇格し、canary 設定をクリアする。

        **人間が叩く操作**。自動昇格はしない。
        """
        state = self.state(prompt_id)
        if state is None or not state.has_canary:
            raise InvalidTransition(f"{prompt_id}: canary が設定されていません")

        target = self.registry.resolve(prompt_id, state.canary_version)
        if target.status != PUBLISHED:
            raise InvalidTransition(
                f"{prompt_id}@{state.canary_version}: status={target.status} は昇格できません"
            )

        self.repo.set_deployment(
            prompt_id,
            active_version=state.canary_version,
            canary_version=None,
            canary_percent=0,
            actor=actor,
        )
        self.repo.insert_prompt_transition(
            prompt_id=prompt_id,
            version=int(state.canary_version or 0),
            from_status=PUBLISHED,
            to_status=PUBLISHED,
            actor=actor,
            reason=f"promote from {state.active_version}",
        )
        self.repo.insert_audit_log(
            event="prompt.promote",
            actor=actor,
            subject=f"{prompt_id}@{state.canary_version}",
            detail={"from_active": state.active_version, "percent": state.canary_percent},
        )
        logger.info(
            "canary を昇格しました: %s@%s → active", prompt_id, state.canary_version
        )
        new_state = self.state(prompt_id)
        assert new_state is not None
        return new_state

    def rollback(
        self, prompt_id: str, to: int | None = None, *, actor: str | None = None
    ) -> ResolvedPrompt:
        """直前の published 版へ戻す。canary が動いていれば併せて止める。"""
        state = self.state(prompt_id)
        if state is not None and state.has_canary:
            logger.warning(
                "canary(%s@%s %s%%)が動いています。rollback と同時に止めます",
                prompt_id, state.canary_version, state.canary_percent,
            )
            self.repo.set_deployment(
                prompt_id,
                active_version=state.active_version,
                canary_version=None,
                canary_percent=0,
                actor=actor,
            )
        return self.registry.rollback(prompt_id, to, actor=actor)

    def stop_canary(self, prompt_id: str, *, actor: str | None = None) -> DeploymentState:
        """canary を止めて active のみに戻す(昇格も rollback もしない)。"""
        state = self.state(prompt_id)
        if state is None:
            raise PromptError(f"{prompt_id}: 公開されていません")
        self.repo.set_deployment(
            prompt_id,
            active_version=state.active_version,
            canary_version=None,
            canary_percent=0,
            actor=actor,
        )
        self.repo.insert_audit_log(
            event="prompt.canary.stop",
            actor=actor,
            subject=f"{prompt_id}@{state.canary_version}",
            detail={"percent": state.canary_percent},
        )
        new_state = self.state(prompt_id)
        assert new_state is not None
        return new_state

    # ------------------------------------------------------------------
    def compare_canary(self, prompt_id: str, *, since: str) -> CanaryComparison | None:
        """active 版と canary 版の実績を分けて集計する(昇格判断の材料)。"""
        state = self.state(prompt_id)
        if state is None or state.canary_version is None:
            return None
        return CanaryComparison(
            prompt_id=prompt_id,
            active_version=state.active_version,
            canary_version=state.canary_version,
            active=self.repo.prompt_version_stats(prompt_id, state.active_version, since=since),
            canary=self.repo.prompt_version_stats(prompt_id, state.canary_version, since=since),
        )
