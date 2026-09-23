"""Markdown レポート生成(FR-033)。

Web UI は作らない(要件定義 §7 非対象)。CLI と Markdown で代替する。
`docs/04_運用ガイド.md` の週次ルーチンが「必ず見る4つ」として挙げているのは
degraded 件数・失敗spanのコスト・stale資産・使われ方の異常なので、
それが1枚で見えることを優先する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from llmops.config import AnomalyConfig
from llmops.db.repository import Repository
from llmops.logging_utils import get_logger
from llmops.observability.anomaly import anomaly_lines, find_anomalies

logger = get_logger(__name__)

_SINCE_RE = re.compile(r"^(\d+)([dhw])$")

GROUP_KEYS = ("system", "model", "prompt")

#: 週次レポートを生成したことを残す監査イベント。鮮度の判定はこの記録を見る
WEEKLY_REPORT_EVENT = "report.weekly"


def record_weekly_report(repo: Repository, *, destination: str, since: str) -> None:
    """週次レポートを生成したことを記録する。失敗しても本処理は止めない。"""
    try:
        repo.insert_audit_log(
            event=WEEKLY_REPORT_EVENT, subject=destination, detail={"since": since}
        )
    except Exception as exc:  # noqa: BLE001 - 記録の失敗でレポート生成を無かったことにしない
        logger.warning("週次レポートの生成記録に失敗しました: %s", exc)


def last_weekly_report(repo: Repository) -> datetime | None:
    """最後に週次レポートを生成した時刻。1度も無ければ None。"""
    rows = repo.list_audit_logs(event=WEEKLY_REPORT_EVENT, limit=1)
    if not rows:
        return None
    stamp = str(rows[0]["created_at"] or "")
    try:
        return datetime.fromisoformat(stamp).replace(tzinfo=UTC)
    except ValueError:
        return None


def weekly_report_staleness(
    repo: Repository, *, stale_days: int, now: datetime | None = None
) -> str | None:
    """放置されていれば1行返す。問題なければ None。

    **出すだけで、止めない・自動実行もしない**(N-046 / N-047)。定期実行が
    落ちていても、次に誰かが `llmops` を叩いた時点で気付けるようにするためのもの。
    """
    if stale_days <= 0:
        return None
    moment = now or datetime.now(UTC)
    last = last_weekly_report(repo)
    if last is None:
        return (
            "週次レポートがまだ1度も生成されていません "
            "(`llmops report weekly --out weekly.md`。定期実行は docs/04 §2)"
        )
    days = (moment - last).days
    if days < stale_days:
        return None
    return (
        f"前回の週次レポートから {days} 日経過しています"
        f"({last.strftime('%Y-%m-%d')} が最後)。"
        "定期実行が止まっていないか確認してください(docs/04 §2)"
    )


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
    #: 実課金ぶん(予算の対象)。cost_usd との差はサブスク換算(NOTES.md N-045)
    billable_cost_usd: float = 0.0
    durations: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.durations is None:
            self.durations = []

    @property
    def subscription_cost_usd(self) -> float:
        """サブスク利用の換算コスト。追加課金は発生しないので予算の対象外。"""
        return self.cost_usd - self.billable_cost_usd

    @property
    def budget_scope(self) -> str:
        """このまとまりが予算の対象かどうかの表示(混在もありうる)。"""
        if self.billable_cost_usd <= 0:
            return "対象外"
        return "対象" if self.subscription_cost_usd <= 0 else "混在"

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
        if bool(row["billable"]):
            bucket.billable_cost_usd += cost
        if not success:
            bucket.failed_cost_usd += cost
        if row["duration_ms"] is not None:
            bucket.durations.append(int(row["duration_ms"]))
    return sorted(buckets.values(), key=lambda b: (-b.cost_usd, b.key))


@dataclass
class PeriodTotals:
    """1期間・1 system の実数。倍率ではなく**実数**で前期間と並べるためのもの。"""

    billable_usd: float = 0.0
    subscription_usd: float = 0.0
    traces: int = 0

    @property
    def total_usd(self) -> float:
        return self.billable_usd + self.subscription_usd


def _period_totals(
    repo: Repository, *, start_day: str, end_day: str
) -> dict[str, PeriodTotals]:
    """`start_day` <= day <= `end_day` の system 別合計(日単位の集計表から取る)。"""
    totals: dict[str, PeriodTotals] = {}
    for row in repo.daily_cost_by_system(since_day=start_day):
        day = str(row["day"])
        if day > end_day:
            continue
        entry = totals.setdefault(str(row["system"]), PeriodTotals())
        cost = float(row["cost_usd"] or 0.0)
        if bool(row["billable"]):
            entry.billable_usd += cost
        else:
            entry.subscription_usd += cost
    for row in repo.daily_trace_counts(since_day=start_day):
        if str(row["day"]) > end_day:
            continue
        totals.setdefault(str(row["system"]), PeriodTotals()).traces += int(row["traces"] or 0)
    return totals


def _delta(current: float, previous: float, *, decimals: int = 6) -> str:
    """実数の差。倍率にしない(前期間が 0 のときに倍率は意味を持たないため)。"""
    diff = current - previous
    sign = "+" if diff >= 0 else "−"
    return f"{sign}{abs(diff):.{decimals}f}" if decimals else f"{sign}{abs(diff):.0f}"


def compare_previous_lines(
    repo: Repository, *, since: datetime, now: datetime
) -> list[str]:
    """前期間との**絶対値**比較(NOTES.md N-046)。

    週次の異常検知は「直近の中央値に対する跳ね」を見るので、**持続した異常は
    数日で中央値に取り込まれて出なくなる**。移動基準である以上これは避けられない。
    水準そのものがずれたことは、期間をまたいだ実数の比較でしか分からない。
    """
    end_day = now.astimezone(UTC).strftime("%Y-%m-%d")
    start_day = since.astimezone(UTC).strftime("%Y-%m-%d")
    length = max(1, (date.fromisoformat(end_day) - date.fromisoformat(start_day)).days + 1)
    previous_end = (date.fromisoformat(start_day) - timedelta(days=1)).isoformat()
    previous_start = (date.fromisoformat(previous_end) - timedelta(days=length - 1)).isoformat()

    current = _period_totals(repo, start_day=start_day, end_day=end_day)
    previous = _period_totals(repo, start_day=previous_start, end_day=previous_end)
    if not current and not previous:
        return ["比較できる記録がありません。"]

    lines = [
        f"- 今期間: {start_day} 〜 {end_day}({length}日)",
        f"- 前期間: {previous_start} 〜 {previous_end}({length}日)",
        "",
        "| system | 実課金(USD) | 前期間 | 差 | 対象外(USD) | 前期間 | 差 | trace | 前期間 | 差 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in sorted(set(current) | set(previous)):
        this, last = current.get(key, PeriodTotals()), previous.get(key, PeriodTotals())
        lines.append(
            f"| {key} | {this.billable_usd:.6f} | {last.billable_usd:.6f} |"
            f" {_delta(this.billable_usd, last.billable_usd)} |"
            f" {this.subscription_usd:.6f} | {last.subscription_usd:.6f} |"
            f" {_delta(this.subscription_usd, last.subscription_usd)} |"
            f" {this.traces} | {last.traces} |"
            f" {_delta(this.traces, last.traces, decimals=0)} |"
        )
    lines += [
        "",
        "**倍率ではなく実数で見る。** 週次の異常検知は移動中央値なので、"
        "持続した異常は数日で「普段」に取り込まれて出なくなる"
        "(スケジューラの二重起動など)。水準がずれたままになっていないかは"
        "ここでしか分からない(NOTES.md N-046)。",
    ]
    return lines


def cost_report(
    repo: Repository,
    *,
    since: datetime,
    by: str = "system",
    system: str | None = None,
    now: datetime | None = None,
    anomaly_config: AnomalyConfig | None = None,
    compare_previous: bool = False,
) -> str:
    """コストレポート(Markdown)。"""
    buckets = collect(repo, since=since, by=by, system=system)
    moment = now or datetime.now(UTC)
    generated = moment.strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# ELF コストレポート",
        "",
        f"- 期間: {_sql_time(since)} 以降",
        f"- 集計軸: {by}",
        f"- 生成: {generated}",
        "",
    ]
    def with_anomalies(body: list[str]) -> str:
        """異常検知の節は、呼び出しが無い期間でも必ず出す。

        「記録が無い」ことと「見ていない」ことを混ぜない。cost_daily に記録が
        あるのに spans が無い、という食い違い自体が異常の手がかりになる。
        """
        if compare_previous:
            body = body + ["", "## 前期間との比較(水準の変化)", ""] + compare_previous_lines(
                repo, since=since, now=moment
            )
        if anomaly_config is None:
            return "\n".join(body) + "\n"
        # サブスク換算のコストには上限を置かない。代わりに普段との比で見る(N-046)
        found = find_anomalies(
            repo, anomaly_config, since_day=_sql_time(since)[:10], today=generated[:10]
        )
        return "\n".join(
            body + ["", "## 使われ方の異常(予算の対象外ぶん)", ""] + anomaly_lines(found)
        ) + "\n"

    if not buckets:
        lines.append("対象期間に記録された呼び出しはありません。")
        return with_anomalies(lines)

    lines += [
        "| " + by + " | 呼び出し | 成功率 | degraded | 入力tok | 出力tok | cost(USD) | "
        "うち実課金 | 予算 | 失敗cost | P50 ms | P95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|",
    ]
    for bucket in buckets:
        lines.append(
            f"| {bucket.key} | {bucket.calls} | {bucket.success_rate:.1f}% | {bucket.degraded} | "
            f"{bucket.input_tokens} | {bucket.output_tokens} | {bucket.cost_usd:.6f} | "
            f"{bucket.billable_cost_usd:.6f} | {bucket.budget_scope} | "
            f"{bucket.failed_cost_usd:.6f} | {bucket.p50} | {bucket.p95} |"
        )

    total_calls = sum(b.calls for b in buckets)
    total_cost = sum(b.cost_usd for b in buckets)
    billable_cost = sum(b.billable_cost_usd for b in buckets)
    failed_cost = sum(b.failed_cost_usd for b in buckets)
    degraded = sum(b.degraded for b in buckets)

    lines += [
        "",
        "## 合計",
        "",
        f"- 呼び出し: {total_calls} 件",
        f"- コスト: {total_cost:.6f} USD",
        f"- **実課金(予算の対象): {billable_cost:.6f} USD**",
        f"- サブスク換算(予算の対象外): {total_cost - billable_cost:.6f} USD",
        "  - `claude -p` の実行分。`total_cost_usd` の換算値で、追加の請求は発生しない",
        f"- 失敗に使ったコスト: {failed_cost:.6f} USD"
        + (f"(全体の {failed_cost / total_cost * 100:.1f}%)" if total_cost > 0 else ""),
        f"- degraded(Fallback で得た結果): {degraded} 件",
    ]

    if degraded:
        lines.append(
            "  - **0 より大きい場合は Fallback が発生している。"
            "`llmops model health` で疎通を確認すること**(運用ガイド §2)"
        )

    return with_anomalies(lines)
