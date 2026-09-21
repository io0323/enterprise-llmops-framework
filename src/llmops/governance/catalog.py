"""AI 資産台帳(Step 3-3 / 19章 §14)。

所有者不在・用途不明の資産が増えることを防ぐ。

`last_used_at` は `spans` から導出する。台帳に手で書かせると「記録はあるのに
台帳が古い」というズレが必ず起きるため(Phase 1 の catalog と同じ考え方)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from llmops.logging_utils import get_logger

logger = get_logger(__name__)

TYPE_PROMPT = "prompt"
TYPE_MODEL = "model"
TYPE_EVAL_SUITE = "eval_suite"
ASSET_TYPES = (TYPE_PROMPT, TYPE_MODEL, TYPE_EVAL_SUITE)

RISK_LEVELS = ("low", "medium", "high")


@dataclass
class Asset:
    """台帳の1行。登録されていない資産も「所有者なし」として列挙する。"""

    asset_type: str
    asset_id: str
    owner: str | None = None
    purpose: str | None = None
    systems: str | None = None
    risk_level: str = "low"
    last_used_at: str | None = None
    recent_calls: int = 0
    status: str | None = None
    registered: bool = False

    @property
    def has_owner(self) -> bool:
        return bool(self.owner)


class AssetCatalog:
    """Prompt / Model / 評価スイートを1つの台帳として見る。"""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.repo = runtime.repo

    # ------------------------------------------------------------------
    def collect(self, *, since: str) -> list[Asset]:
        """実体(DB に登録済みの資産)を基準に台帳を組み立てる。

        台帳側にしか無い行(実体が消えた資産)も残す。棚卸しの対象になるため。
        """
        registered = {
            (str(row["asset_type"]), str(row["asset_id"])): row for row in self.repo.list_assets()
        }
        assets: list[Asset] = []
        seen: set[tuple[str, str]] = set()

        prompt_calls = self.repo.prompt_call_counts(since=since)
        for row in self.repo.list_prompts():
            prompt_id = str(row["prompt_id"])
            key = (TYPE_PROMPT, prompt_id)
            seen.add(key)
            assets.append(
                self._build(
                    key,
                    registered.get(key),
                    last_used_at=self.repo.prompt_last_used_at(prompt_id),
                    recent_calls=prompt_calls.get(prompt_id, 0),
                    status=str(row["status"]),
                )
            )

        model_calls = self.repo.model_call_counts(since=since)
        model_last_used = self.repo.model_last_used()
        for model in self.runtime.models.list_all():
            key = (TYPE_MODEL, model.logical_name)
            seen.add(key)
            assets.append(
                self._build(
                    key,
                    registered.get(key),
                    last_used_at=model_last_used.get(model.logical_name),
                    recent_calls=model_calls.get(model.logical_name, 0),
                    status=model.status,
                )
            )

        for row in self.repo.list_eval_suites():
            key = (TYPE_EVAL_SUITE, str(row["id"]))
            seen.add(key)
            runs = self.repo.list_eval_runs(suite_id=str(row["id"]), limit=1)
            assets.append(
                self._build(
                    key,
                    registered.get(key),
                    last_used_at=None if not runs else str(runs[0]["started_at"]),
                    recent_calls=len(self.repo.list_eval_cases(str(row["id"]))),
                    status=None,
                )
            )

        for key, row in registered.items():
            if key in seen:
                continue
            asset = self._build(key, row, last_used_at=None, recent_calls=0, status="missing")
            assets.append(asset)

        return sorted(assets, key=lambda a: (a.asset_type, a.asset_id))

    @staticmethod
    def _build(
        key: tuple[str, str],
        row: Any,
        *,
        last_used_at: str | None,
        recent_calls: int,
        status: str | None,
    ) -> Asset:
        asset_type, asset_id = key
        if row is None:
            return Asset(
                asset_type=asset_type,
                asset_id=asset_id,
                last_used_at=last_used_at,
                recent_calls=recent_calls,
                status=status,
            )
        return Asset(
            asset_type=asset_type,
            asset_id=asset_id,
            owner=None if row["owner"] is None else str(row["owner"]),
            purpose=None if row["purpose"] is None else str(row["purpose"]),
            systems=None if row["systems"] is None else str(row["systems"]),
            risk_level=str(row["risk_level"] or "low"),
            last_used_at=last_used_at or (
                None if row["last_used_at"] is None else str(row["last_used_at"])
            ),
            recent_calls=recent_calls,
            status=status,
            registered=True,
        )

    # ------------------------------------------------------------------
    def set_owner(
        self,
        asset_type: str,
        asset_id: str,
        *,
        owner: str,
        risk: str = "low",
        purpose: str | None = None,
        systems: str | None = None,
    ) -> Asset:
        if asset_type not in ASSET_TYPES:
            raise ValueError(f"未知の資産種別です: {asset_type}({', '.join(ASSET_TYPES)})")
        if risk not in RISK_LEVELS:
            raise ValueError(f"risk は {', '.join(RISK_LEVELS)} のいずれか: {risk}")

        self.repo.upsert_asset(
            asset_type=asset_type, asset_id=asset_id, owner=owner,
            purpose=purpose, systems=systems, risk_level=risk,
        )
        self.repo.insert_audit_log(
            event="catalog.set_owner",
            subject=f"{asset_type}:{asset_id}",
            detail={"owner": owner, "risk": risk, "purpose": purpose},
        )
        row = self.repo.get_asset(asset_type, asset_id)
        return self._build(
            (asset_type, asset_id), row, last_used_at=None, recent_calls=0, status=None
        )

    def refresh_usage(self) -> int:
        """`spans` から最終利用日を台帳へ書き戻す(バッチ)。"""
        updated = 0
        for row in self.repo.list_assets():
            asset_type, asset_id = str(row["asset_type"]), str(row["asset_id"])
            if asset_type == TYPE_PROMPT:
                last_used = self.repo.prompt_last_used_at(asset_id)
            elif asset_type == TYPE_MODEL:
                last_used = self.repo.model_last_used().get(asset_id)
            else:
                runs = self.repo.list_eval_runs(suite_id=asset_id, limit=1)
                last_used = None if not runs else str(runs[0]["started_at"])
            if last_used and last_used != row["last_used_at"]:
                self.repo.touch_asset_usage(asset_type, asset_id, last_used)
                updated += 1
        return updated

    # ------------------------------------------------------------------
    def without_owner(self, *, since: str) -> list[Asset]:
        return [asset for asset in self.collect(since=since) if not asset.has_owner]

    def stale(self, *, days: int, today: str, since: str) -> list[Asset]:
        """`days` 日以上使われていない資産(未使用を含む)。"""
        threshold = (date.fromisoformat(today[:10]) - timedelta(days=days)).isoformat()
        return [
            asset
            for asset in self.collect(since=since)
            if asset.last_used_at is None or asset.last_used_at[:10] < threshold
        ]
