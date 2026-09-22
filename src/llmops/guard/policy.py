"""宣言的 Policy(Step 3-4)。

**文書だけの Policy にしない**(19章 §11)。`policies.yaml` に書いたものを
`llmops policy check` が実データに照らして検査する。

Phase 3 のスコープは「検査して列挙する」ところまで。Gateway の実行時判定は
既に個別の仕組みとして入っている(published ゲート・Quota・未公開版の認可)。
Policy はそれらを**宣言として一覧できる**ようにし、CI で回せる形にするのが役目。

検査の設計方針は評価と同じ「迷ったら止める」:
`action` の既定は `block`(= `policy check` が非ゼロ終了する)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llmops.logging_utils import get_logger

logger = get_logger(__name__)

BLOCK = "block"
WARN = "warn"

#: 実装しているルール種別。`policies.yaml` でこれ以外を書いたらロード時にエラー
RULE_TYPES = (
    "no_unpublished_in_production",
    "no_raw_completion",
    "no_model_fallback",
    "owner_required",
    "no_forced_publish",
    "eval_before_publish",
)


@dataclass(frozen=True)
class Policy:
    id: str
    rule: str
    action: str = BLOCK
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.action == BLOCK


@dataclass
class Violation:
    policy_id: str
    rule: str
    action: str
    subject: str
    detail: str

    @property
    def blocking(self) -> bool:
        return self.action == BLOCK


@dataclass
class PolicyReport:
    checked: int
    violations: list[Violation] = field(default_factory=list)

    @property
    def blocking(self) -> list[Violation]:
        return [v for v in self.violations if v.blocking]

    @property
    def ok(self) -> bool:
        return not self.blocking


class PolicyError(ValueError):
    """`policies.yaml` の書き方が不正。"""


def load_policies(path: Path) -> list[Policy]:
    """`policies.yaml` を読む。ファイルが無ければ空(Policy 未設定)。"""
    if not path.is_file():
        logger.info("policies.yaml がありません(Policy 検査はスキップされます): %s", path)
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = loaded.get("policies") if isinstance(loaded, dict) else None
    if not isinstance(raw, list):
        raise PolicyError(f"{path}: policies: のリストがありません")

    policies: list[Policy] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise PolicyError(f"{path}: policies の要素がマッピングではありません")
        policy_id = str(entry.get("id") or "")
        rule = str(entry.get("rule") or "")
        if not policy_id:
            raise PolicyError(f"{path}: id の無い policy があります")
        if rule not in RULE_TYPES:
            raise PolicyError(
                f"{path}: 未対応の rule です: {rule}(使えるのは {', '.join(RULE_TYPES)})"
            )
        action = str(entry.get("action", BLOCK))
        if action not in (BLOCK, WARN):
            raise PolicyError(f"{path}: action は block か warn: {action}")
        policies.append(
            Policy(
                id=policy_id,
                rule=rule,
                action=action,
                description=str(entry.get("description", "")),
                params={k: v for k, v in entry.items()
                        if k not in {"id", "rule", "action", "description"}},
            )
        )
    return policies


# ---------------------------------------------------------------------------
# ルール実装
# ---------------------------------------------------------------------------


def _no_unpublished_in_production(
    policy: Policy, repo: Any, *, since: str, **_: Any
) -> list[Violation]:
    """published でない版が実行された形跡を検出する(Step 3-4)。

    span の meta に付いた印を数える。評価実行(eval_runs に裏付けがあるもの)は
    正規の経路なので除く。**裏付けの無いものだけを違反として挙げる。**
    """
    violations: list[Violation] = []
    for span in repo.spans_with_meta_flag("unpublished_execution", since=since):
        trace_id = str(span["trace_id"])
        eval_run = repo.eval_run_for_trace(trace_id)
        if eval_run is not None and str(eval_run["prompt_id"]) == str(span["prompt_id"]):
            continue  # 評価経路。想定内
        violations.append(
            Violation(
                policy.id,
                policy.rule,
                policy.action,
                f"{span['prompt_id']}@{span['prompt_version']}",
                f"system={span['system']} trace={trace_id} span={span['id']}"
                " が未公開版で実行されています(評価実行の裏付けなし)",
            )
        )
    return violations


def _no_raw_completion(policy: Policy, repo: Any, *, since: str, **_: Any) -> list[Violation]:
    """Registry 管理外の呼び出し(`complete_raw`)が使われていないか。"""
    return [
        Violation(
            policy.id,
            policy.rule,
            policy.action,
            str(row["subject"] or "-"),
            f"{row['created_at']} actor={row['actor']} detail={row['detail_json']}",
        )
        for row in repo.list_audit_logs(event="raw_completion", limit=200)
        if str(row["created_at"] or "") >= since
    ]


def _no_model_fallback(
    policy: Policy, repo: Any, *, models: Any = None, **_: Any
) -> list[Violation]:
    """本番の論理モデルに `fallback_to` が復活していないか(N-026 の再発検知)。

    落とし先が mock だと「それらしい偽の結果」が通る。設定が戻されたことを
    人が気付ける前に機械で拾う。
    """
    if models is None:
        return []
    allowed = {str(name) for name in policy.params.get("allow", [])}
    violations: list[Violation] = []
    for model in models.list_all():
        if not model.fallback_to or model.logical_name in allowed:
            continue
        violations.append(
            Violation(
                policy.id,
                policy.rule,
                policy.action,
                model.logical_name,
                f"fallback_to='{model.fallback_to}' が設定されています"
                "(NOTES.md N-026: 落とし先が mock だと偽の結果が通る)",
            )
        )
    return violations


def _owner_required(policy: Policy, repo: Any, *, assets: Any = None, **_: Any) -> list[Violation]:
    """資産に所有者が設定されているか。`risk` を指定するとその水準だけを見る。"""
    if assets is None:
        return []
    risk = policy.params.get("risk")
    violations: list[Violation] = []
    for asset in assets:
        if asset.owner:
            continue
        if risk is not None and asset.risk_level != str(risk):
            continue
        violations.append(
            Violation(
                policy.id, policy.rule, policy.action,
                f"{asset.asset_type}:{asset.asset_id}",
                "所有者が未設定です(`llmops catalog set-owner` で設定する)",
            )
        )
    return violations


def _no_forced_publish(policy: Policy, repo: Any, *, since: str, **_: Any) -> list[Violation]:
    """評価ゲートを飛ばした publish(`--force`)が使われていないか。"""
    return [
        Violation(
            policy.id, policy.rule, policy.action, str(row["subject"] or "-"),
            f"{row['created_at']} actor={row['actor']} {row['detail_json']}",
        )
        for row in repo.list_audit_logs(event="prompt.publish.forced", limit=200)
        if str(row["created_at"] or "") >= since
    ]


def _eval_before_publish(policy: Policy, repo: Any, **_: Any) -> list[Violation]:
    """いま published になっている版に、評価実行の裏付けがあるか。

    ゲートは publish の瞬間に効くが、`--force` や移行時の初回登録では通っていない。
    「いまの本番」に根拠があるかを別途見る。
    """
    violations: list[Violation] = []
    for row in repo.list_prompts(status="published"):
        prompt_id = str(row["prompt_id"])
        if prompt_id.startswith("_fragments."):
            continue
        active = row["active_version"]
        if active is None:
            continue
        run = repo.latest_eval_run(prompt_id, int(active))
        if run is None:
            violations.append(
                Violation(
                    policy.id, policy.rule, policy.action, f"{prompt_id}@{active}",
                    "本番の版に評価実行の裏付けがありません"
                    "(`llmops eval run <suite>` を流して記録を残す)",
                )
            )
        elif str(run["verdict"]) != "pass":
            violations.append(
                Violation(
                    policy.id, policy.rule, policy.action, f"{prompt_id}@{active}",
                    f"本番の版の直近評価が verdict='{run['verdict']}' です",
                )
            )
    return violations


_RULES = {
    "no_unpublished_in_production": _no_unpublished_in_production,
    "no_raw_completion": _no_raw_completion,
    "no_model_fallback": _no_model_fallback,
    "owner_required": _owner_required,
    "no_forced_publish": _no_forced_publish,
    "eval_before_publish": _eval_before_publish,
}


def check(
    policies: Sequence[Policy],
    repo: Any,
    *,
    since: str,
    models: Any = None,
    assets: Any = None,
) -> PolicyReport:
    """全 Policy を実データに照らす。"""
    report = PolicyReport(checked=len(policies))
    for policy in policies:
        found = _RULES[policy.rule](policy, repo, since=since, models=models, assets=assets)
        report.violations.extend(found)
        if found:
            try:
                repo.insert_audit_log(
                    event="policy.violation",
                    subject=policy.id,
                    detail={"rule": policy.rule, "count": len(found), "action": policy.action},
                )
            except Exception as exc:  # noqa: BLE001 - 記録失敗で検査を止めない
                logger.warning("Policy 違反の記録に失敗しました: %s", exc)
    return report
