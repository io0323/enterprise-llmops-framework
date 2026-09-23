"""使われ方の異常検知(NOTES.md N-046)。

サブスク利用のコストには金額のハード上限を置けない(置くと N-045 で直した逆転を
作り直す)。代わりに「普段との比」で見て、**止めずに知らせる**。

ここで守りたいのは2つ:
1. 暴走(スケジューラの二重起動など)を見落とさない
2. **誤検知でレポートが無視される状態にしない**。静かな日の揺れを異常と呼ばない
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from llmops.config import AnomalyConfig, Config
from llmops.db.repository import Repository
from llmops.gateway import Runtime
from llmops.governance.weekly import weekly_report
from llmops.models import TraceStart
from llmops.observability.anomaly import COST, TRACES, detect, find_anomalies
from llmops.observability.report import cost_report

TODAY = "2026-09-23"
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def _days(count: int, *, end: str = TODAY) -> list[str]:
    from datetime import date, timedelta

    last = date.fromisoformat(end)
    return [(last - timedelta(days=i)).isoformat() for i in range(count - 1, -1, -1)]


def _config(**overrides: object) -> AnomalyConfig:
    return AnomalyConfig(**overrides)  # type: ignore[arg-type]


def _steady(value: float, *, days: int = 10, end: str = TODAY) -> dict[str, float]:
    return dict.fromkeys(_days(days, end=end), value)


# ---------------------------------------------------------------------------
# 判定そのもの
# ---------------------------------------------------------------------------


def test_steady_usage_is_not_an_anomaly() -> None:
    series = _steady(10.0)
    found = detect(
        series, days=[TODAY], baseline_days=7, multiple=3.0, floor=5.0,
        kind=COST, system="cgmp",
    )
    assert found == []


def test_a_spike_over_the_multiple_is_reported() -> None:
    series = _steady(10.0)
    series[TODAY] = 40.0
    (found,) = detect(
        series, days=[TODAY], baseline_days=7, multiple=3.0, floor=5.0,
        kind=COST, system="cgmp",
    )
    assert found.day == TODAY
    assert found.ratio == pytest.approx(4.0)
    assert "4.0 倍" in found.describe()


def test_a_spike_under_the_multiple_is_not_reported() -> None:
    """2倍で騒がない。**騒ぎすぎたレポートは読まれなくなる。**"""
    series = _steady(10.0)
    series[TODAY] = 20.0
    assert detect(
        series, days=[TODAY], baseline_days=7, multiple=3.0, floor=5.0,
        kind=COST, system="cgmp",
    ) == []


def test_small_absolute_values_are_ignored() -> None:
    """中央値 0.01 USD の 100 倍(= 1 USD)で起こさない。下限が効く。"""
    series = _steady(0.01)
    series[TODAY] = 1.0
    assert detect(
        series, days=[TODAY], baseline_days=7, multiple=3.0, floor=5.0,
        kind=COST, system="cgmp",
    ) == []


def test_median_ignores_a_single_past_spike() -> None:
    """平均ではなく中央値。先週の1日の跳ねで、今週の異常が隠れないこと。"""
    series = _steady(10.0)
    series[_days(3)[0]] = 1000.0  # 過去の跳ね
    series[TODAY] = 40.0
    (found,) = detect(
        series, days=[TODAY], baseline_days=7, multiple=3.0, floor=5.0,
        kind=COST, system="cgmp",
    )
    assert found.baseline == pytest.approx(10.0)


def test_first_activity_on_a_quiet_system_is_reported_without_a_ratio() -> None:
    """普段動いていない system が急に動いたら知らせる。倍率は出さない(0 除算)。"""
    series = {TODAY: 30.0}
    (found,) = detect(
        series, days=[TODAY], baseline_days=7, multiple=3.0, floor=5.0,
        kind=TRACES, system="dde",
    )
    assert found.ratio is None
    assert "中央値は 0" in found.describe()


def test_detection_can_be_switched_off() -> None:
    series = _steady(10.0)
    series[TODAY] = 10_000.0
    assert detect(
        series, days=[TODAY], baseline_days=0, multiple=3.0, floor=5.0,
        kind=COST, system="cgmp",
    ) == []


# ---------------------------------------------------------------------------
# DB を通した検知
# ---------------------------------------------------------------------------


def _cost(repo: Repository, day: str, system: str, cost: float, *, billable: bool = False) -> None:
    repo.upsert_cost_daily(
        day=day, system=system, logical_model="chat-standard", cost_usd=cost, billable=billable
    )


def _traces(repo: Repository, day: str, system: str, count: int) -> None:
    for index in range(count):
        repo.insert_trace(
            TraceStart(id=f"{system}-{day}-{index}", system=system, operation="t"),
        )
        repo.conn.execute(
            "UPDATE traces SET started_at = ? WHERE id = ?",
            (f"{day} 01:00:00", f"{system}-{day}-{index}"),
        )
    repo.conn.commit()


def test_runaway_subscription_cost_is_detected(repo: Repository) -> None:
    for day in _days(10)[:-1]:
        _cost(repo, day, "cgmp", 2.0)
    _cost(repo, TODAY, "cgmp", 60.0)

    (found,) = find_anomalies(repo, _config(), since_day=_days(3)[0], today=TODAY)

    assert (found.kind, found.system, found.day) == (COST, "cgmp", TODAY)
    assert found.ratio == pytest.approx(30.0)


def test_billable_cost_is_not_reported_here(repo: Repository) -> None:
    """実課金ぶんは budgets が止める。ここで二重に騒がない。"""
    for day in _days(10)[:-1]:
        _cost(repo, day, "harness", 0.1, billable=True)
    _cost(repo, TODAY, "harness", 50.0, billable=True)

    assert find_anomalies(repo, _config(), since_day=_days(3)[0], today=TODAY) == []


def test_trace_count_spike_is_detected(repo: Repository) -> None:
    """コストが動く前に件数で気付ける(1回あたりが安い処理の暴走)。"""
    for day in _days(10)[:-1]:
        _traces(repo, day, "dde", 2)
    _traces(repo, TODAY, "dde", 120)

    (found,) = find_anomalies(repo, _config(), since_day=_days(3)[0], today=TODAY)

    assert (found.kind, found.system) == (TRACES, "dde")
    assert found.current == 120


def test_normal_week_reports_nothing(repo: Repository) -> None:
    for day in _days(10):
        _cost(repo, day, "cgmp", 2.0)
        _traces(repo, day, "cgmp", 3)

    assert find_anomalies(repo, _config(), since_day=_days(7)[0], today=TODAY) == []


def test_one_row_per_system_and_kind(repo: Repository) -> None:
    """同じ原因で7行並べない(最も外れた日を代表にする)。"""
    for day in _days(10)[:-3]:
        _cost(repo, day, "cgmp", 2.0)
    for day in _days(3):
        _cost(repo, day, "cgmp", 90.0)

    found = find_anomalies(repo, _config(), since_day=_days(3)[0], today=TODAY)

    assert len(found) == 1
    assert found[0].current == pytest.approx(90.0)


def test_thresholds_come_from_config(repo: Repository) -> None:
    """閾値はコードではなく config.yaml(絶対ルール12)。"""
    for day in _days(10)[:-1]:
        _cost(repo, day, "cgmp", 2.0)
    _cost(repo, TODAY, "cgmp", 8.0)  # 4倍だが既定の下限(5 USD)を超える

    assert find_anomalies(repo, _config(), since_day=_days(3)[0], today=TODAY)
    # 倍率を上げれば鳴らない
    strict = _config(cost_multiple=10.0)
    assert find_anomalies(repo, strict, since_day=_days(3)[0], today=TODAY) == []


# ---------------------------------------------------------------------------
# レポートへの出方(止めない / 気付ける)
# ---------------------------------------------------------------------------


def test_cost_report_shows_the_anomaly_section(repo: Repository) -> None:
    for day in _days(10)[:-1]:
        _cost(repo, day, "cgmp", 2.0)
    _cost(repo, TODAY, "cgmp", 60.0)

    markdown = cost_report(
        repo, since=datetime(2026, 9, 21, tzinfo=UTC), now=NOW, anomaly_config=_config()
    )

    assert "使われ方の異常" in markdown
    assert "自動では止めない" in markdown
    assert "cgmp" in markdown


def test_cost_report_says_so_when_nothing_is_odd(repo: Repository) -> None:
    markdown = cost_report(
        repo, since=datetime(2026, 9, 21, tzinfo=UTC), now=NOW, anomaly_config=_config()
    )
    assert "普段と比べて外れた system はありません" in markdown


def test_weekly_report_lists_it_in_the_four_things(
    runtime: Runtime, repo: Repository, config: Config
) -> None:
    for day in _days(10)[:-1]:
        _cost(repo, day, "cgmp", 2.0)
    _cost(repo, TODAY, "cgmp", 60.0)

    markdown = weekly_report(runtime, since=_days(7)[0], today=TODAY)

    assert "必ず見る4つ" in markdown
    assert "使われ方の異常" in markdown
    assert "普段と桁が違う system がある" in markdown


def test_anomaly_does_not_block_calls(runtime: Runtime, repo: Repository) -> None:
    """**検出しても止めない。** 止める判断は人がする(N-046)。"""
    for day in _days(10):
        _cost(repo, day, "elf", 999.0)

    trace_id = runtime.tracer.start_trace("t")
    from llmops.models import CompletionRequest

    result = runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo",
        )
    )
    assert result.text


# ---------------------------------------------------------------------------
# 移動中央値の盲点と、月次の絶対値比較(N-046 追記)
# ---------------------------------------------------------------------------


def _sustained_doubling(repo: Repository) -> None:
    """10日前から二重起動が続いている状態(日額 2.0 → 60.0 のまま)。"""
    from datetime import date, timedelta

    start = date(2026, 7, 1)
    changed = date(2026, 9, 13)
    end = date.fromisoformat(TODAY)
    day = start
    while day <= end:
        _cost(repo, day.isoformat(), "cgmp", 60.0 if day >= changed else 2.0)
        day += timedelta(days=1)


def test_a_sustained_anomaly_disappears_from_weekly_detection(repo: Repository) -> None:
    """**盲点そのものを挙動として残す。**

    二重起動が続くと、異常値のほうが中央値になり、週次の検知は沈黙する。
    移動基準である以上これは避けられない(閾値を厳しくすると誤検知が増えて
    レポートが読まれなくなる)。だから月次の絶対値比較で拾う、という役割分担にした。
    """
    _sustained_doubling(repo)

    # 直近7日を見る週次レポートでは、もう何も出ない
    assert find_anomalies(repo, _config(), since_day=_days(7)[0], today=TODAY) == []


def test_previous_period_comparison_catches_what_weekly_misses(repo: Repository) -> None:
    """同じ状況を、前期間との実数比較なら拾える(週次=跳ね / 月次=水準)。"""
    _sustained_doubling(repo)

    markdown = cost_report(
        repo,
        since=datetime(2026, 8, 25, tzinfo=UTC),
        now=NOW,
        anomaly_config=_config(),
        compare_previous=True,
    )

    # 週次の見方(移動中央値)は沈黙している
    weekly = find_anomalies(repo, _config(), since_day=_days(7)[0], today=TODAY)
    assert weekly == []
    # 月次の実数比較では水準の差がはっきり出る(698.0 - 60.0)
    assert "前期間との比較" in markdown
    assert "+638.000000" in markdown


def test_comparison_uses_the_same_length_window(repo: Repository) -> None:
    _cost(repo, "2026-09-20", "cgmp", 5.0)
    _cost(repo, "2026-09-10", "cgmp", 3.0)

    markdown = cost_report(
        repo, since=datetime(2026, 9, 17, tzinfo=UTC), now=NOW, compare_previous=True
    )

    assert "今期間: 2026-09-17 〜 2026-09-23(7日)" in markdown
    assert "前期間: 2026-09-10 〜 2026-09-16(7日)" in markdown
    assert "| cgmp | 0.000000 | 0.000000 | +0.000000 | 5.000000 | 3.000000 | +2.000000" in markdown


def test_comparison_separates_billable_from_subscription(repo: Repository) -> None:
    _cost(repo, "2026-09-20", "harness", 1.0, billable=True)
    _cost(repo, "2026-09-20", "cgmp", 4.0)

    markdown = cost_report(
        repo, since=datetime(2026, 9, 17, tzinfo=UTC), now=NOW, compare_previous=True
    )

    assert "| harness | 1.000000 |" in markdown
    assert "| cgmp | 0.000000 | 0.000000 | +0.000000 | 4.000000 |" in markdown


def test_comparison_counts_traces(repo: Repository) -> None:
    """コストが動かない暴走(安い処理)は trace 数の差で出る。"""
    _traces(repo, "2026-09-20", "dde", 200)
    _traces(repo, "2026-09-10", "dde", 3)

    markdown = cost_report(
        repo, since=datetime(2026, 9, 17, tzinfo=UTC), now=NOW, compare_previous=True
    )

    assert "| 200 | 3 | +197 |" in markdown


def test_comparison_is_off_by_default(repo: Repository) -> None:
    _cost(repo, "2026-09-20", "cgmp", 5.0)
    markdown = cost_report(repo, since=datetime(2026, 9, 17, tzinfo=UTC), now=NOW)
    assert "前期間との比較" not in markdown


def test_comparison_without_records_says_so(repo: Repository) -> None:
    markdown = cost_report(
        repo, since=datetime(2026, 9, 17, tzinfo=UTC), now=NOW, compare_previous=True
    )
    assert "比較できる記録がありません" in markdown
