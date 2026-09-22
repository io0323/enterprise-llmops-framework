"""異常検知(NOTES.md N-046)。**警告するだけで、止めない。**

`billable: false` のコスト(サブスク利用)には金額のハード上限を置けない。
置くと「課金していない処理を、金銭的な理由なく止める」ことになり、N-045 で直した
逆転を別の形で作ってしまう。一方、スケジューラの暴走のように「普段と桁が違う」
使われ方は、止めないまでも**気付ける**必要がある。

そこで比較するのは上限ではなく**普段との比**:

- 直近 N 日の中央値に対する倍率(平均ではなく中央値。1日の跳ねに引きずられない)
- 日額・件数の下限(`min_*`)を併用する。静かな日の小さな揺れを「10倍」と
  言い立てないため。**誤検知で無視されるレポートは、検知しないより害が大きい**

trace 生成数もコストと同じ基準で見る。1回あたりが安い処理が暴走した場合、
コストより先に件数のほうが異常として出る。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any

from llmops.config import AnomalyConfig
from llmops.db.repository import Repository

COST = "cost"
TRACES = "traces"


@dataclass(frozen=True)
class Anomaly:
    """普段と比べて外れている1件。止める根拠ではなく、人が見る材料。"""

    kind: str
    system: str
    day: str
    current: float
    baseline: float
    multiple: float
    floor: float

    @property
    def ratio(self) -> float | None:
        """中央値に対する倍率。中央値が 0 のときは倍率を出さない(無限大にしない)。"""
        return None if self.baseline <= 0 else self.current / self.baseline

    @property
    def label(self) -> str:
        return "対象外コスト" if self.kind == COST else "trace 生成数"

    def describe(self) -> str:
        unit = "USD" if self.kind == COST else "件"
        current = f"{self.current:.6f} {unit}" if self.kind == COST else f"{self.current:.0f} 件"
        if self.ratio is None:
            return (
                f"{self.day} の {self.label} が {current}。"
                f"直近の中央値は 0(普段は動いていない)"
            )
        base = f"{self.baseline:.6f} {unit}" if self.kind == COST else f"{self.baseline:.0f} 件"
        return (
            f"{self.day} の {self.label} が {current}。"
            f"直近の中央値 {base} の {self.ratio:.1f} 倍"
        )


def _day_range(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [
        (first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)
    ]


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def detect(
    series: Mapping[str, float],
    *,
    days: Sequence[str],
    baseline_days: int,
    multiple: float,
    floor: float,
    kind: str,
    system: str,
) -> list[Anomaly]:
    """`days` の各日を、その直前 `baseline_days` 日の中央値と比べる。

    記録の無い日は 0 として数える(「動いていなかった」も普段の姿のため)。
    """
    if baseline_days <= 0:
        return []
    found: list[Anomaly] = []
    for day in days:
        current = float(series.get(day, 0.0))
        if current < floor:
            continue  # 下限未満は倍率を見ない(静かな日の揺れを拾わない)
        window = [
            float(series.get(_shift(day, -offset), 0.0))
            for offset in range(1, baseline_days + 1)
        ]
        base = float(median(window)) if window else 0.0
        if base > 0 and current < base * multiple:
            continue
        found.append(
            Anomaly(
                kind=kind, system=system, day=day, current=current,
                baseline=base, multiple=multiple, floor=floor,
            )
        )
    return found


def _series_by_system(rows: Iterable[Any], value_key: str) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = {}
    for row in rows:
        system = str(row["system"])
        grouped.setdefault(system, {})[str(row["day"])] = float(row[value_key] or 0.0)
    return grouped


def _worst(found: Sequence[Anomaly]) -> Anomaly:
    """同じ system / 種別の中で最も外れている日を代表にする。"""
    return max(found, key=lambda a: (a.ratio if a.ratio is not None else float("inf"), a.current))


def find_anomalies(
    repo: Repository,
    config: AnomalyConfig,
    *,
    since_day: str,
    today: str | None = None,
) -> list[Anomaly]:
    """`since_day` 以降の各日について、system ごとに異常を探す。

    返すのは system × 種別ごとに最も外れている1日だけ。同じ原因で7行並べない。
    """
    if config.baseline_days <= 0:
        return []
    end = today or datetime.now(UTC).strftime("%Y-%m-%d")
    if since_day > end:
        return []
    days = _day_range(since_day, end)
    history_from = _shift(since_day, -config.baseline_days)

    found: list[Anomaly] = []
    # 予算の対象外(サブスク換算)のコストだけを見る。実課金は budgets が止める
    costs = _series_by_system(
        repo.daily_cost_by_system(since_day=history_from, billable=False), "cost_usd"
    )
    for system, series in costs.items():
        hits = detect(
            series, days=days, baseline_days=config.baseline_days,
            multiple=config.cost_multiple, floor=config.min_cost_usd,
            kind=COST, system=system,
        )
        if hits:
            found.append(_worst(hits))

    traces = _series_by_system(repo.daily_trace_counts(since_day=history_from), "traces")
    for system, series in traces.items():
        hits = detect(
            series, days=days, baseline_days=config.baseline_days,
            multiple=config.trace_multiple, floor=float(config.min_traces),
            kind=TRACES, system=system,
        )
        if hits:
            found.append(_worst(hits))

    return sorted(found, key=lambda a: (a.kind, a.system))


def anomaly_lines(found: Sequence[Anomaly]) -> list[str]:
    """レポートに貼る Markdown。検出0件でも「見た」ことが分かる行を返す。"""
    if not found:
        return ["普段と比べて外れた system はありません。"]
    lines = [
        "| system | 見たもの | 内容 |",
        "|---|---|---|",
    ]
    lines += [f"| {a.system} | {a.label} | {a.describe()} |" for a in found]
    lines += [
        "",
        "**これは上限ではなく「普段との比」。自動では止めない**(NOTES.md N-046)。"
        "心当たりが無ければ、スケジューラの二重起動・リトライループを疑う:",
        "`llmops trace list --since 2d` で件数と operation を見る。",
    ]
    return lines
