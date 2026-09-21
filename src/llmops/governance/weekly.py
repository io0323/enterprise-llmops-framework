"""週次レポート(Step 3-7)。

コスト・評価・Policy・台帳・監査を1枚にまとめる。

**`governance` に置く理由**: 集計対象が `observability`(コスト)と `guard`(Policy)と
`governance`(台帳・監査)に跨る。`observability` に置くと
`observability → guard → observability` の循環になる(依存の向きの絶対規約に反する)。
週次レポートは「統制のための読み物」なので、governance が置き場として正しい。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from llmops.governance.audit import attention_counts
from llmops.governance.catalog import AssetCatalog
from llmops.guard import policy as policy_module
from llmops.observability.report import collect


def weekly_report(
    runtime: Any,
    *,
    since: str,
    stale_days: int = 90,
    today: str | None = None,
) -> str:
    """週次レポート(Step 3-7)。1枚で週の状態が分かることを優先する。

    「必ず見る3つ」(運用ガイド §2)を上に置き、詳細は下に流す。
    """
    repo = runtime.repo
    stamp = today or datetime.now(UTC).strftime("%Y-%m-%d")
    buckets = collect(repo, since=_as_datetime(since), by="system")

    total_calls = sum(b.calls for b in buckets)
    total_cost = sum(b.cost_usd for b in buckets)
    failed_cost = sum(b.failed_cost_usd for b in buckets)
    degraded = sum(b.degraded for b in buckets)

    catalog = AssetCatalog(runtime)
    assets = catalog.collect(since=since)
    no_owner = [a for a in assets if not a.has_owner]
    stale = catalog.stale(days=stale_days, today=stamp, since=since)

    policies = policy_module.load_policies(runtime.config.policies_file)
    report = policy_module.check(
        policies, repo, since=since, models=runtime.models, assets=assets
    )
    attention = attention_counts(repo, since=since)

    lines = [
        f"# ELF 週次レポート — {stamp}",
        "",
        f"対象期間: {since} 以降",
        "",
        "## 必ず見る3つ",
        "",
        "| 見るもの | 値 | 判断 |",
        "|---|---:|---|",
        f"| degraded 件数 | {degraded} | "
        + (
            "**0 でない。`models.yaml` に fallback_to が復活していないか確認する**"
            if degraded
            else "問題なし(本番モデルに Fallback は無い)"
        )
        + " |",
        f"| 失敗に使ったコスト | {failed_cost:.6f} USD | "
        + (
            f"全体の {failed_cost / total_cost * 100:.1f}%"
            + ("(**10%超**。Prompt の出力形式指示を疑う)" if total_cost and
               failed_cost / total_cost > 0.1 else "")
            if total_cost
            else "コスト記録なし"
        )
        + " |",
        f"| {stale_days}日未使用の資産 | {len(stale)} | "
        + ("棚卸しの対象(`llmops catalog stale`)" if stale else "なし")
        + " |",
        "",
        "## コスト",
        "",
        f"- 呼び出し: {total_calls} 件 / コスト: {total_cost:.6f} USD",
    ]

    trace_rows = repo.trace_costs(since=_as_datetime(since))
    if trace_rows:
        costs = [float(row["cost_usd"]) for row in trace_rows]
        lines += [
            f"- **1タスクあたりのコスト(trace 単位): 平均 {sum(costs) / len(costs):.6f} USD"
            f" / 最大 {max(costs):.6f} USD**",
            "  - span 単価ではなくこちらを見る。失敗リトライが多いと trace 単価が上がる",
        ]

    if buckets:
        lines += [
            "",
            "| system | 呼び出し | 成功率 | cost(USD) | 失敗cost |",
            "|---|---:|---:|---:|---:|",
        ]
        lines += [
            f"| {b.key} | {b.calls} | {b.success_rate:.1f}% | {b.cost_usd:.6f} |"
            f" {b.failed_cost_usd:.6f} |"
            for b in buckets
        ]

    lines += ["", "## 評価", ""]
    runs = [r for r in repo.list_eval_runs(limit=50) if str(r["started_at"] or "") >= since]
    if not runs:
        lines.append("対象期間に評価実行はありません。**Prompt を変えていないなら正常。**")
    else:
        for run in runs:
            score = "-" if run["score"] is None else f"{float(run['score']):.4f}"
            lines.append(
                f"- {run['started_at']} `{run['suite_id']}` "
                f"{run['prompt_id']}@{run['prompt_version']} → **{run['verdict']}**"
                f"(score={score}, mode={run['mode']})"
            )

    lines += ["", "## Policy 違反", ""]
    if not report.violations:
        lines.append(f"違反なし({report.checked} 件の Policy を検査)。")
    else:
        for violation in report.violations:
            mark = "**BLOCK**" if violation.blocking else "warn"
            lines.append(
                f"- {mark} `{violation.policy_id}` {violation.subject}: {violation.detail}"
            )

    lines += ["", "## 資産台帳", ""]
    lines.append(f"- 登録資産: {len(assets)} 件 / 所有者なし: **{len(no_owner)} 件**")
    if no_owner:
        for asset in no_owner[:20]:
            lines.append(f"  - {asset.asset_type}:{asset.asset_id}")
        if len(no_owner) > 20:
            lines.append(f"  - ほか {len(no_owner) - 20} 件")
    if stale:
        lines.append(f"- {stale_days}日以上未使用:")
        for asset in stale[:20]:
            last = asset.last_used_at or "(未使用)"
            lines.append(f"  - {asset.asset_type}:{asset.asset_id} 最終利用={last}")

    lines += ["", "## 要注意イベント", ""]
    if not attention:
        lines.append("なし。")
    else:
        for event, count in sorted(attention.items()):
            lines.append(f"- `{event}`: {count} 件(`llmops audit tail --event {event}`)")

    lines += [
        "",
        "## 閉ループ(これを飛ばすと評価が陳腐化する)",
        "",
        "```bash",
        "llmops trace list --status failed --since 7d",
        "llmops eval add-case <suite> --from-span <span_id>",
        "```",
        "",
        "週1件でも追加する。`llmops eval list` の出自内訳で production-failure が"
        "増えているか確認すること。",
    ]
    return "\n".join(lines) + "\n"


def _as_datetime(value: str) -> datetime:
    """SQL 形式の時刻文字列を datetime へ(`collect` が datetime を取るため)。"""
    return datetime.fromisoformat(value).replace(tzinfo=UTC)
