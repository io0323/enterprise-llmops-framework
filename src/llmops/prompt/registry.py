"""Prompt Registry — 読込・版採番・解決・状態遷移(設計 §2.2 / §3 / §5.2)。

状態機械は PEP と同一にする。将来 PEP へ資産を移すときに状態の対応表を書かずに
済ませるため(`docs/05_既存システム統合.md` §5)。
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from llmops.db.repository import Repository
from llmops.errors import InvalidTransition, PromptError, PromptNotFound
from llmops.logging_utils import get_logger
from llmops.models import PromptVersionRow
from llmops.prompt import loader
from llmops.prompt.render import RenderResult, content_hash, expand_includes, render

logger = get_logger(__name__)

DRAFT = "draft"
REVIEW = "review"
APPROVED = "approved"
PUBLISHED = "published"
DEPRECATED = "deprecated"
ARCHIVED = "archived"

STATUSES = (DRAFT, REVIEW, APPROVED, PUBLISHED, DEPRECATED, ARCHIVED)

#: 許可する遷移(設計 §2.2)。`review → draft` は差戻し、`published → published` は rollback。
ALLOWED_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        (DRAFT, REVIEW),
        (REVIEW, APPROVED),
        (REVIEW, DRAFT),
        (APPROVED, PUBLISHED),
        (APPROVED, DRAFT),
        (PUBLISHED, DEPRECATED),
        (PUBLISHED, PUBLISHED),
        (DEPRECATED, ARCHIVED),
    }
)


class EvalGate(Protocol):
    """publish 時の評価ゲート。Phase 2 で中身が入る差し込み口(Phase 1 では未設定)。"""

    def __call__(self, prompt_id: str, version: int) -> str | None:
        """合格なら `eval_run_id` を返し、不合格なら `EvaluationFailed` を送出する。"""


@dataclass(frozen=True)
class ResolvedPrompt:
    """実行に使う1版。`body` は fragment 展開済み。"""

    prompt_id: str
    version: int
    status: str
    body: str
    front_matter: dict[str, Any]
    content_hash: str
    source_path: str
    var_schema: dict[str, Any] | None = None

    @property
    def default_model(self) -> str | None:
        model = self.front_matter.get("model")
        return None if model is None else str(model)

    @property
    def tags(self) -> list[str]:
        raw = self.front_matter.get("tags") or []
        return [str(tag) for tag in raw] if isinstance(raw, list) else []

    @property
    def eval_suite(self) -> str | None:
        suite = self.front_matter.get("eval_suite")
        return None if suite is None else str(suite)


@dataclass(frozen=True)
class SyncResult:
    prompt_id: str
    version: int
    status: str
    action: str  # 'created' / 'unchanged'


class PromptRegistry:
    """`prompts/**/*.md` と `prompt_versions` の間を取り持つ。"""

    def __init__(
        self,
        repo: Repository,
        prompts_dir: Path,
        *,
        rng: random.Random | None = None,
        eval_gate: EvalGate | None = None,
    ) -> None:
        self.repo = repo
        self.prompts_dir = prompts_dir
        self._rng = rng or random.Random()
        self._eval_gate = eval_gate

    # ------------------------------------------------------------------
    # sync(ファイル → DB)
    # ------------------------------------------------------------------
    def sync(self, *, actor: str | None = None) -> list[SyncResult]:
        """`content_hash` の差分で新 version を採番する。

        fragment 自身も `_fragments.<name>` として版管理する(設計 §3.1)。
        fragment を変更すると、展開後テキストが変わるため参照元にも新 version が出る。
        """
        prompt_set = loader.load_dir(self.prompts_dir)
        # fragment は本文へ**行中に**差し込まれる断片。ファイル末尾の改行はファイルの
        # 体裁であって内容ではないので落とす(残すと参照元に空行が1つ増える)。
        fragments = {
            prompt_id[len(loader.FRAGMENT_PREFIX) :]: file.body.rstrip("\n")
            for prompt_id, file in prompt_set.fragments.items()
        }

        results: list[SyncResult] = []
        for file in prompt_set.all_files():
            body = file.body if file.is_fragment else expand_includes(file.body, fragments)
            self._check_declared_includes(file, body)
            results.append(self._sync_one(file, body, actor=actor))
        return results

    @staticmethod
    def _check_declared_includes(file: loader.PromptFile, expanded: str) -> None:
        """宣言漏れ(`includes:` に書いていない fragment の参照)を検出する。

        `expand_includes` は未知の fragment でエラーにするため、ここでは逆
        (宣言したのに本文で使っていない)を WARN する。
        """
        del expanded
        if file.is_fragment and file.includes:
            raise PromptError(f"{file.path}: fragment は他の fragment を include できない")
        for name in file.includes:
            if f"include.{name}" not in file.body:
                logger.warning(
                    "%s: includes に宣言された %s が本文で使われていません", file.path, name
                )

    def _sync_one(
        self, file: loader.PromptFile, body: str, *, actor: str | None
    ) -> SyncResult:
        digest = content_hash(body, file.front_matter_text)
        latest = self.repo.latest_prompt_version(file.prompt_id)

        if latest is not None and str(latest["content_hash"]) == digest:
            return SyncResult(
                file.prompt_id, int(latest["version"]), str(latest["status"]), "unchanged"
            )

        version = 1 if latest is None else int(latest["version"]) + 1
        status = self._status_for_new_version(file, is_first=latest is None)

        self.repo.insert_prompt_version(
            PromptVersionRow(
                prompt_id=file.prompt_id,
                version=version,
                status=status,
                body=body,
                front_matter=file.front_matter_text,
                var_schema=json.dumps(file.var_schema, ensure_ascii=False)
                if file.var_schema
                else None,
                content_hash=digest,
                source_path=str(file.path),
                owner=file.owner,
            )
        )
        self.repo.insert_prompt_transition(
            prompt_id=file.prompt_id,
            version=version,
            from_status=None,
            to_status=status,
            actor=actor,
            reason="sync",
        )
        if status == PUBLISHED:
            self.repo.set_deployment(file.prompt_id, active_version=version, actor=actor)
        self.repo.insert_audit_log(
            event="prompt.sync",
            actor=actor,
            subject=f"{file.prompt_id}@{version}",
            detail={"status": status, "content_hash": digest},
        )
        if file.declared_version is not None and file.declared_version != version:
            logger.warning(
                "%s: front matter の version(%s)と採番された version(%s)が一致しません"
                "(採番が正。NOTES.md N-018)",
                file.path,
                file.declared_version,
                version,
            )
        return SyncResult(file.prompt_id, version, status, "created")

    @staticmethod
    def _status_for_new_version(file: loader.PromptFile, *, is_first: bool) -> str:
        """新 version の初期状態(NOTES.md N-018)。

        初回登録に限り front matter の `status` を尊重する(既に本番で動いている
        Prompt を移行するため)。2回目以降の変更は必ず `draft` から始め、
        状態機械を通らないと本番に出せないようにする。
        """
        declared = file.declared_status
        if declared is not None and declared not in STATUSES:
            raise PromptError(f"{file.path}: 未知の status です: {declared}")
        if is_first and declared is not None:
            return declared
        if declared is not None and declared != DRAFT:
            logger.warning(
                "%s: front matter の status(%s)は初回登録時のみ有効です。draft で登録します",
                file.path,
                declared,
            )
        return DRAFT

    # ------------------------------------------------------------------
    # resolve / render
    # ------------------------------------------------------------------
    def resolve(self, prompt_id: str, version: int | None = None) -> ResolvedPrompt:
        """版を決める。未指定なら deployments の active(canary 設定があれば確率で canary)。"""
        if version is not None:
            row = self.repo.get_prompt_version(prompt_id, version)
            if row is None:
                raise PromptNotFound(f"Prompt が見つかりません: {prompt_id}@{version}")
            return self._to_resolved(row)

        deployment = self.repo.get_deployment(prompt_id)
        if deployment is not None:
            chosen = self._choose_version(deployment)
            row = self.repo.get_prompt_version(prompt_id, chosen)
            if row is None:
                raise PromptNotFound(f"Prompt が見つかりません: {prompt_id}@{chosen}")
            return self._to_resolved(row)

        row = self.repo.latest_prompt_version(prompt_id)
        if row is None:
            raise PromptNotFound(
                f"Prompt が見つかりません: {prompt_id}(`llmops sync` は実行済みか)"
            )
        return self._to_resolved(row)

    def _choose_version(self, deployment: Any) -> int:
        canary_version = deployment["canary_version"]
        percent = int(deployment["canary_percent"] or 0)
        if canary_version is not None and percent > 0 and self._rng.randint(1, 100) <= percent:
            return int(canary_version)
        return int(deployment["active_version"])

    @staticmethod
    def _to_resolved(row: Any) -> ResolvedPrompt:
        front_matter = yaml.safe_load(row["front_matter"]) or {}
        var_schema = json.loads(row["var_schema"]) if row["var_schema"] else None
        return ResolvedPrompt(
            prompt_id=str(row["prompt_id"]),
            version=int(row["version"]),
            status=str(row["status"]),
            body=str(row["body"]),
            front_matter=front_matter,
            content_hash=str(row["content_hash"]),
            source_path=str(row["source_path"]),
            var_schema=var_schema,
        )

    def render(self, resolved: ResolvedPrompt, variables: Mapping[str, Any]) -> RenderResult:
        """body は sync 時に fragment 展開済みなので、ここでは変数展開のみ。"""
        return render(resolved.body, variables, var_schema=resolved.var_schema)

    # ------------------------------------------------------------------
    # 状態遷移
    # ------------------------------------------------------------------
    def _transition(
        self,
        prompt_id: str,
        version: int | None,
        *,
        to_status: str,
        actor: str | None,
        reason: str | None,
        from_statuses: tuple[str, ...],
        eval_run_id: str | None = None,
    ) -> ResolvedPrompt:
        target = self._version_in(prompt_id, version, from_statuses)
        if (target.status, to_status) not in ALLOWED_TRANSITIONS:
            raise InvalidTransition(
                f"{prompt_id}@{target.version}: {target.status} → {to_status} は許可されていません"
            )
        self.repo.set_prompt_status(prompt_id, target.version, to_status)
        self.repo.insert_prompt_transition(
            prompt_id=prompt_id,
            version=target.version,
            from_status=target.status,
            to_status=to_status,
            actor=actor,
            reason=reason,
            eval_run_id=eval_run_id,
        )
        self.repo.insert_audit_log(
            event=f"prompt.{to_status}",
            actor=actor,
            subject=f"{prompt_id}@{target.version}",
            detail={"from": target.status, "to": to_status, "reason": reason},
        )
        return self.resolve(prompt_id, target.version)

    def _version_in(
        self, prompt_id: str, version: int | None, statuses: tuple[str, ...]
    ) -> ResolvedPrompt:
        """版の指定が無いとき、対象となる状態の最新版を選ぶ。"""
        if version is not None:
            return self.resolve(prompt_id, version)
        candidates = [
            row for row in self.repo.list_prompt_versions(prompt_id) if row["status"] in statuses
        ]
        if not candidates:
            raise InvalidTransition(
                f"{prompt_id}: status が {' / '.join(statuses)} の版がありません"
            )
        return self._to_resolved(candidates[-1])

    def submit(self, prompt_id: str, version: int | None = None, *, actor: str | None = None,
               reason: str | None = None) -> ResolvedPrompt:
        return self._transition(prompt_id, version, to_status=REVIEW, actor=actor, reason=reason,
                                from_statuses=(DRAFT,))

    def approve(self, prompt_id: str, version: int | None = None, *, actor: str | None = None,
                reason: str | None = None) -> ResolvedPrompt:
        return self._transition(prompt_id, version, to_status=APPROVED, actor=actor, reason=reason,
                                from_statuses=(REVIEW,))

    def reject(self, prompt_id: str, version: int | None = None, *, actor: str | None = None,
               reason: str | None = None) -> ResolvedPrompt:
        """差戻し(review / approved → draft)。"""
        return self._transition(prompt_id, version, to_status=DRAFT, actor=actor, reason=reason,
                                from_statuses=(REVIEW, APPROVED))

    def publish(
        self,
        prompt_id: str,
        version: int | None = None,
        *,
        actor: str | None = None,
        reason: str | None = None,
        force: bool = False,
    ) -> ResolvedPrompt:
        """approved → published。ポインタ(`prompt_deployments`)も切り替える。

        評価ゲートは Phase 2 で `eval_gate` に注入する。Phase 1 は差し込み口のみ。
        `force=True` はゲートを飛ばす(緊急用。監査ログに残る)。
        """
        target = self._version_in(prompt_id, version, (APPROVED,))
        eval_run_id: str | None = None
        if self._eval_gate is not None and not force:
            eval_run_id = self._eval_gate(prompt_id, target.version)
        elif force:
            self.repo.insert_audit_log(
                event="prompt.publish.forced",
                actor=actor,
                subject=f"{prompt_id}@{target.version}",
                detail={"reason": reason},
            )

        published = self._transition(
            prompt_id,
            target.version,
            to_status=PUBLISHED,
            actor=actor,
            reason=reason,
            from_statuses=(APPROVED,),
            eval_run_id=eval_run_id,
        )
        self.repo.set_deployment(prompt_id, active_version=published.version, actor=actor)
        return published

    def deprecate(self, prompt_id: str, version: int | None = None, *, actor: str | None = None,
                  reason: str | None = None) -> ResolvedPrompt:
        return self._transition(prompt_id, version, to_status=DEPRECATED, actor=actor,
                                reason=reason, from_statuses=(PUBLISHED,))

    def archive(self, prompt_id: str, version: int | None = None, *, actor: str | None = None,
                reason: str | None = None) -> ResolvedPrompt:
        return self._transition(prompt_id, version, to_status=ARCHIVED, actor=actor, reason=reason,
                                from_statuses=(DEPRECATED,))

    def rollback(
        self, prompt_id: str, to: int | None = None, *, actor: str | None = None
    ) -> ResolvedPrompt:
        """直前の published 版へポインタを戻す(FR-050)。

        版の内容は変えない。`prompt_deployments` の1行を書き換えるだけで完結する。
        """
        deployment = self.repo.get_deployment(prompt_id)
        if deployment is None:
            raise InvalidTransition(f"{prompt_id}: published 版がないため rollback できません")
        active = int(deployment["active_version"])

        target_version = to
        if target_version is None:
            previous = [
                int(row["version"])
                for row in self.repo.list_prompt_versions(prompt_id)
                if row["status"] == PUBLISHED and int(row["version"]) < active
            ]
            if not previous:
                raise InvalidTransition(f"{prompt_id}: 戻せる published 版がありません")
            target_version = previous[-1]

        target = self.resolve(prompt_id, target_version)
        if target.status != PUBLISHED:
            raise InvalidTransition(
                f"{prompt_id}@{target_version}: status={target.status} は rollback 先にできません"
            )

        self.repo.set_deployment(prompt_id, active_version=target_version, actor=actor)
        self.repo.insert_prompt_transition(
            prompt_id=prompt_id,
            version=target_version,
            from_status=PUBLISHED,
            to_status=PUBLISHED,
            actor=actor,
            reason=f"rollback from {active}",
        )
        self.repo.insert_audit_log(
            event="prompt.rollback",
            actor=actor,
            subject=f"{prompt_id}@{target_version}",
            detail={"from_active": active},
        )
        return target

    def canary(
        self, prompt_id: str, *, version: int, percent: int, actor: str | None = None
    ) -> None:
        """新版を割合指定で段階適用する(FR-051。本格運用は Phase 3)。"""
        if not 0 <= percent <= 100:
            raise PromptError(f"canary の割合は 0-100 で指定する: {percent}")
        deployment = self.repo.get_deployment(prompt_id)
        if deployment is None:
            raise InvalidTransition(f"{prompt_id}: published 版がないため canary を設定できません")
        target = self.resolve(prompt_id, version)
        if target.status != PUBLISHED:
            raise InvalidTransition(
                f"{prompt_id}@{version}: status={target.status} は canary 先にできません"
            )
        self.repo.set_deployment(
            prompt_id,
            active_version=int(deployment["active_version"]),
            canary_version=version,
            canary_percent=percent,
            actor=actor,
        )
        self.repo.insert_audit_log(
            event="prompt.canary",
            actor=actor,
            subject=f"{prompt_id}@{version}",
            detail={"percent": percent},
        )
