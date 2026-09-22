"""Model Registry — `models.yaml` の読込と論理モデル名の解決(設計 §2.3 / §4)。

アプリコードは論理名(`chat-standard` 等)だけを知る。実 Provider・実モデル名は
ここから先にしか出てこない(FR-011 / 絶対ルール13)。

`config_hash` が既存最新版と違えば**自動で新 version を採番する**。Provider 側の
silent update を含む設定変更が、必ず版として残るようにするため(FR-011 / 19章 §3.3)。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llmops.db.repository import Repository
from llmops.errors import ModelNotFound
from llmops.logging_utils import get_logger
from llmops.models import ModelVersionRow

logger = get_logger(__name__)

ACTIVE = "active"
DEPRECATED = "deprecated"
BLOCKED = "blocked"

#: `billable` を書かなかったときに「課金されない」と見なす adapter(NOTES.md N-045)。
#: 既定は **課金される**側に倒す。新しい Provider を足して宣言を忘れても、
#: 予算の対象から外れて黙って上限を超える、という事故にはならない。
NON_BILLABLE_ADAPTERS = ("claude_cli", "mock")


def default_billable(adapter: str) -> bool:
    """`billable` 未宣言のときの既定。"""
    return adapter not in NON_BILLABLE_ADAPTERS


def config_hash(entry: dict[str, Any]) -> str:
    """models.yaml の1エントリのハッシュ。キー順に依存しない形で取る。"""
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedModel:
    """1回の呼び出しに使うモデル定義。"""

    logical_name: str
    version: int
    adapter: str
    params: dict[str, Any] = field(default_factory=dict)
    price: dict[str, float] = field(default_factory=dict)
    fallback_to: str | None = None
    status: str = ACTIVE
    #: 実際に課金が発生するか。False は「サブスク利用の換算値」で、予算判定の対象外
    #: (記録とレポートには従来どおり載る)。NOTES.md N-045
    billable: bool = True

    @property
    def input_price_per_1k(self) -> float:
        return float(self.price.get("input_per_1k", 0.0))

    @property
    def output_price_per_1k(self) -> float:
        return float(self.price.get("output_per_1k", 0.0))


def _entry_billable(entry: dict[str, Any]) -> bool:
    declared = entry.get("billable")
    if declared is None:
        return default_billable(str(entry.get("adapter", "")))
    return bool(declared)


@dataclass(frozen=True)
class ModelSyncResult:
    logical_name: str
    version: int
    action: str  # 'created' / 'unchanged'


class ModelRegistry:
    """`models.yaml` と `model_versions` の間を取り持つ。"""

    def __init__(self, repo: Repository, models_file: Path) -> None:
        self.repo = repo
        self.models_file = models_file

    # ------------------------------------------------------------------
    def load_file(self) -> dict[str, dict[str, Any]]:
        if not self.models_file.is_file():
            raise ModelNotFound(f"models.yaml がありません: {self.models_file}")
        loaded = yaml.safe_load(self.models_file.read_text(encoding="utf-8")) or {}
        models = loaded.get("models") if isinstance(loaded, dict) else None
        if not isinstance(models, dict):
            raise ModelNotFound(f"{self.models_file}: models: セクションがありません")
        return {str(name): dict(entry or {}) for name, entry in models.items()}

    def sync(self) -> list[ModelSyncResult]:
        """設定 → DB。`config_hash` の差分で新 version を採番する。"""
        results: list[ModelSyncResult] = []
        for logical_name, entry in self.load_file().items():
            digest = config_hash(entry)
            latest = self.repo.latest_model_version(logical_name)
            if latest is not None and str(latest["config_hash"]) == digest:
                results.append(
                    ModelSyncResult(logical_name, int(latest["version"]), "unchanged")
                )
                continue

            version = 1 if latest is None else int(latest["version"]) + 1
            self.repo.insert_model_version(
                ModelVersionRow(
                    logical_name=logical_name,
                    version=version,
                    adapter=str(entry.get("adapter", "")),
                    params_json=json.dumps(entry.get("params") or {}, ensure_ascii=False),
                    price_json=json.dumps(entry.get("price") or {}, ensure_ascii=False),
                    fallback_to=(
                        None if entry.get("fallback_to") is None else str(entry["fallback_to"])
                    ),
                    status=str(entry.get("status", ACTIVE)),
                    config_hash=digest,
                    billable=_entry_billable(entry),
                )
            )
            self.repo.insert_audit_log(
                event="model.sync",
                subject=f"{logical_name}@{version}",
                detail={"adapter": entry.get("adapter"), "config_hash": digest},
            )
            results.append(ModelSyncResult(logical_name, version, "created"))
        return results

    # ------------------------------------------------------------------
    def resolve(self, logical_name: str, version: int | None = None) -> ResolvedModel:
        """論理名 → Adapter / パラメータ / 単価。status が active の版のみ返す。"""
        row = (
            self.repo.get_model_version(logical_name, version)
            if version is not None
            else self.repo.latest_model_version(logical_name)
        )
        if row is None:
            raise ModelNotFound(
                f"論理モデル名が見つかりません: {logical_name}"
                " (`llmops sync` は実行済みか / models.yaml に定義があるか)"
            )
        status = str(row["status"])
        if status != ACTIVE:
            raise ModelNotFound(f"{logical_name}@{row['version']} は status={status} です")

        return ResolvedModel(
            logical_name=logical_name,
            version=int(row["version"]),
            adapter=str(row["adapter"]),
            params=json.loads(row["params_json"]) if row["params_json"] else {},
            price=json.loads(row["price_json"]) if row["price_json"] else {},
            fallback_to=None if row["fallback_to"] is None else str(row["fallback_to"]),
            status=status,
            billable=bool(row["billable"]),
        )

    def list_all(self) -> list[ResolvedModel]:
        """論理名ごとの最新版(status 問わず)。"""
        models: list[ResolvedModel] = []
        for row in self.repo.list_models():
            models.append(
                ResolvedModel(
                    logical_name=str(row["logical_name"]),
                    version=int(row["version"]),
                    adapter=str(row["adapter"]),
                    params=json.loads(row["params_json"]) if row["params_json"] else {},
                    price=json.loads(row["price_json"]) if row["price_json"] else {},
                    fallback_to=None if row["fallback_to"] is None else str(row["fallback_to"]),
                    status=str(row["status"]),
                    billable=bool(row["billable"]),
                )
            )
        return models
