"""評価レポート(Markdown)。

**Judge のスコアは絶対値として信用しない。版間の相対比較にのみ使う。**
これはレポートの先頭に毎回書く(過信の防止。実装指示 Step 2-2)。
"""

from __future__ import annotations

import json
from typing import Any

from llmops.db.repository import Repository
from llmops.eval.regression import MODE_WIRING_CHECK

DISCLAIMER = (
    "> **Judge のスコアは絶対値として信用しないこと。** LLM による採点は揺れる。"
    "ここでの用途は**版間の相対比較**(この版は前の版より下がっていないか)に限る。\n"
    "> 採点できなかったケース(errors)は平均から除外している。0点として扱っていない。"
)


def eval_run_report(repo: Repository, run_id: str) -> str:
    run = repo.get_eval_run(run_id)
    if run is None:
        return f"eval_run が見つかりません: {run_id}\n"

    results = repo.list_eval_results(run_id)
    score_text = "なし(採点できず)" if run["score"] is None else f"{float(run['score']):.4f}"
    lines = [
        f"# 評価レポート — {run['suite_id']}",
        "",
        DISCLAIMER,
        "",
        f"- run: `{run_id}`",
        f"- 対象: `{run['prompt_id']}@{run['prompt_version']}`",
        f"- 生成モデル: `{run['logical_model']}` / Judge: `{run['judge_model'] or '(なし)'}`",
        f"- verdict: **{run['verdict']}**",
        f"- スコア: {score_text}",
        f"- 決定的評価: {run['passed']}/{run['total']} 件合格",
        f"- 採点失敗: {run['errors']} 件 / degraded span: {run['degraded_spans']} 件",
        f"- コスト: {float(run['cost_usd'] or 0):.6f} USD",
        f"- 期間: {run['started_at']} → {run['finished_at']}",
    ]
    if run["baseline_run_id"]:
        lines.append(f"- ベースライン: `{run['baseline_run_id']}`")
    if run["note"]:
        lines.append(f"- 判定理由: {run['note']}")
    if str(run["mode"]) == MODE_WIRING_CHECK:
        lines += [
            "",
            "> **配線確認モード(mock Adapter 経由)の実行です。**"
            "品質の判定には使えません。baseline にも publish の根拠にもなりません。",
        ]

    lines += ["", "## 決定的評価の違反内訳", ""]
    violations = _violation_breakdown(results)
    if not violations:
        lines.append("違反はありません。")
    else:
        lines += ["| ルール | 違反ケース数 |", "|---|---:|"]
        lines += [f"| {rule} | {count} |" for rule, count in violations]

    lines += ["", "## Judge のスコア", ""]
    judged = [r for r in results if r["kind"] == "judge" and r["status"] == "ok"]
    if not judged:
        lines.append("Judge のスコアはありません(決定的評価のみ、または全件採点失敗)。")
    else:
        by_metric: dict[str, list[float]] = {}
        for row in judged:
            by_metric.setdefault(str(row["metric"]), []).append(float(row["score"] or 0.0))
        lines += ["| 指標 | 件数 | 平均 | 最小 |", "|---|---:|---:|---:|"]
        for metric, scores in sorted(by_metric.items()):
            lines.append(
                f"| {metric} | {len(scores)} | {sum(scores) / len(scores):.3f} |"
                f" {min(scores):.3f} |"
            )

    errors = [r for r in results if r["status"] == "error"]
    if errors:
        lines += ["", "## 採点できなかったケース", "", "スコアの平均から除外している。", ""]
        for row in errors:
            lines.append(f"- `{row['case_id']}` / {row['metric']}: {row['detail']}")

    lines += ["", "## ケースの出自内訳", ""]
    origins = repo.count_eval_cases_by_origin(str(run["suite_id"]))
    if not origins:
        lines.append("ケースがありません。")
    else:
        for origin, count in sorted(origins.items()):
            lines.append(f"- {origin}: {count} 件")
        if not origins.get("production-failure"):
            lines += [
                "",
                "> **production-failure 由来のケースが0件。** 本番の失敗が"
                "回帰ケースに変換されていない。このままだとスイートは陳腐化する"
                "(`llmops eval add-case <suite> --from-span <span_id>`)。",
            ]

    return "\n".join(lines) + "\n"


def _violation_breakdown(results: list[Any]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in results:
        if row["kind"] == "deterministic" and not row["passed"]:
            counts[str(row["metric"])] = counts.get(str(row["metric"]), 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def quality_report(repo: Repository, *, since: str, limit: int = 50) -> str:
    """品質レポート(Step 2-6)。版別のスコア推移と、評価基盤自体の健全性。"""
    runs = [
        run
        for run in repo.list_eval_runs(limit=limit)
        if str(run["started_at"] or "") >= since
    ]
    lines = [
        "# ELF 品質レポート",
        "",
        DISCLAIMER,
        "",
        f"- 期間: {since} 以降",
        f"- 評価実行: {len(runs)} 件",
        "",
    ]
    if not runs:
        lines.append("対象期間に評価実行はありません。")
        return "\n".join(lines) + "\n"

    lines += [
        "## Prompt 版別のスコア推移",
        "",
        "| 実行 | スイート | 対象 | mode | verdict | スコア | 決定的 | 採点失敗 | degraded |",
        "|---|---|---|---|---|---:|---:|---:|---:|",
    ]
    for run in runs:
        score = "-" if run["score"] is None else f"{float(run['score']):.4f}"
        lines.append(
            f"| {run['started_at']} | {run['suite_id']} |"
            f" {run['prompt_id']}@{run['prompt_version']} | {run['mode']} |"
            f" {run['verdict']} | {score} | {run['passed']}/{run['total']} |"
            f" {run['errors']} | {run['degraded_spans']} |"
        )

    total_runs = len(runs)
    degraded_runs = sum(1 for run in runs if int(run["degraded_spans"] or 0) > 0)
    error_runs = sum(1 for run in runs if int(run["errors"] or 0) > 0)
    wiring = sum(1 for run in runs if str(run["mode"]) == MODE_WIRING_CHECK)

    lines += [
        "",
        "## 評価基盤の健全性",
        "",
        f"- degraded を含む run: {degraded_runs} / {total_runs} 件",
        f"- 採点失敗を含む run: {error_runs} / {total_runs} 件"
        "(Judge 自体の健全性。高いなら Judge Prompt か応答形式を疑う)",
        f"- 配線確認モードの run: {wiring} / {total_runs} 件"
        "(品質判定には使えない実行)",
    ]
    if degraded_runs:
        lines.append(
            "\n> **degraded を含む run がある。** 縮退実行の結果で品質を判定しないため"
            " verdict は fail に倒してある。`llmops model health` で疎通を確認すること。"
        )
    return "\n".join(lines) + "\n"


def parse_detail(detail: str | None) -> dict[str, Any]:
    """`eval_results.detail` の JSON を読む(読めなければ空)。"""
    if not detail:
        return {}
    try:
        loaded = json.loads(detail)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
