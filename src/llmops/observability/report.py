"""Markdown レポート生成(FR-033)。

Web UI は作らない(要件定義 §7 非対象)。CLI と Markdown で代替する。
`docs/04_運用ガイド.md` の週次ルーチンが「必ず見る3つ」として挙げているのは
degraded 件数・失敗spanのコスト・stale資産なので、それが1枚で見えることを優先する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from llmops.db.repository import Repository

_SINCE_RE = re.compile(r"^(\d+)([dhw])$")

GROUP_KEYS = ("system", "model", "prompt")


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """`7d` / `24h` / `2w` を起点時刻へ。ISO 日付(`2026-09-01`)も受ける。"""
    reference = now or datetime.now(UTC)
    match = _SINCE_RE.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = {"d": timedelta(days=amount), "h": timedelta(hours=amount),
                 "w": timedelta(weeks=amount)}[unit]
        return reference - delta
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--since の書式が不正です: {value}(例: 7d / 24h / 2026-09-01)") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _sql_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def percentile(values: list[int], ratio: float) -> int:
    """最近傍順位法。件数が少ない環境なので補間はしない。"""
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class Bucket:
    """集計単位1つぶん。"""

    key: str
    calls: int = 0
    succeeded: int = 0
    degraded: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    failed_cost_usd: float = 0.0
    durations: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.durations is None:
            self.durations = []

    @property
    def success_rate(self) -> float:
        return 0.0 if self.calls == 0 else self.succeeded / self.calls * 100

    @property
    def p50(self) -> int:
        return percentile(self.durations, 0.5)

    @property
    def p95(self) -> int:
        return percentile(self.durations, 0.95)


def _group_key(row: object, by: str) -> str:
    mapping = {"system": "system", "model": "logical_model", "prompt": "prompt_id"}
    value = row[mapping[by]]  # type: ignore[index]
    if by == "prompt":
        version = row["prompt_version"]  # type: ignore[index]
        return "(adhoc)" if value is None else f"{value}@{version}"
    return str(value)


def collect(
    repo: Repository, *, since: datetime, by: str = "system", system: str | None = None
) -> list[Bucket]:
    if by not in GROUP_KEYS:
        raise ValueError(f"--by は {' / '.join(GROUP_KEYS)} のいずれか: {by}")

    buckets: dict[str, Bucket] = {}
    for row in repo.spans_since(_sql_time(since), system=system):
        bucket = buckets.setdefault(_group_key(row, by), Bucket(key=_group_key(row, by)))
        bucket.calls += 1
        success = bool(row["success"])
        bucket.succeeded += 1 if success else 0
        bucket.degraded += 1 if row["degraded"] else 0
        bucket.input_tokens += int(row["input_tokens"] or 0)
        bucket.output_tokens += int(row["output_tokens"] or 0)
        cost = float(row["cost_usd"] or 0.0)
        bucket.cost_usd += cost
        if not success:
            bucket.failed_cost_usd += cost
        if row["duration_ms"] is not None:
            bucket.durations.append(int(row["duration_ms"]))
    return sorted(buckets.values(), key=lambda b: (-b.cost_usd, b.key))


def cost_report(
    repo: Repository,
    *,
    since: datetime,
    by: str = "system",
    system: str | None = None,
    now: datetime | None = None,
) -> str:
    """コストレポート(Markdown)。"""
    buckets = collect(repo, since=since, by=by, system=system)
    generated = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# ELF コストレポート",
        "",
        f"- 期間: {_sql_time(since)} 以降",
        f"- 集計軸: {by}",
        f"- 生成: {generated}",
        "",
    ]
    if not buckets:
        lines.append("対象期間に記録された呼び出しはありません。")
        return "\n".join(lines) + "\n"

    lines += [
        "| " + by + " | 呼び出し | 成功率 | degraded | 入力tok | 出力tok | cost(USD) | "
        "失敗cost | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for bucket in buckets:
        lines.append(
            f"| {bucket.key} | {bucket.calls} | {bucket.success_rate:.1f}% | {bucket.degraded} | "
            f"{bucket.input_tokens} | {bucket.output_tokens} | {bucket.cost_usd:.6f} | "
            f"{bucket.failed_cost_usd:.6f} | {bucket.p50} | {bucket.p95} |"
        )

    total_calls = sum(b.calls for b in buckets)
    total_cost = sum(b.cost_usd for b in buckets)
    failed_cost = sum(b.failed_cost_usd for b in buckets)
    degraded = sum(b.degraded for b in buckets)

    lines += [
        "",
        "## 合計",
        "",
        f"- 呼び出し: {total_calls} 件",
        f"- コスト: {total_cost:.6f} USD",
        f"- 失敗に使ったコスト: {failed_cost:.6f} USD"
        + (f"(全体の {failed_cost / total_cost * 100:.1f}%)" if total_cost > 0 else ""),
        f"- degraded(Fallback で得た結果): {degraded} 件",
    ]
    if degraded:
        lines.append(
            "  - **0 より大きい場合は Fallback が発生している。"
            "`llmops model health` で疎通を確認すること**(運用ガイド §2)"
        )
    return "\n".join(lines) + "\n"
