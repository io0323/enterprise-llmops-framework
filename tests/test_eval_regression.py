"""回帰判定・Groundedness・レポート(Step 2-3 / 2-4 / 2-6)。"""

from __future__ import annotations

from typing import Any

import pytest

from llmops.db.repository import Repository
from llmops.eval import groundedness, regression
from llmops.eval.judge import Judge, JudgeResponseError, JudgeVerdict
from llmops.eval.report import eval_run_report, quality_report
from llmops.eval.suite import Thresholds

TH = Thresholds(deterministic_pass_rate=1.0, min_score=0.75, regression_tolerance=0.02)


def _judge(**kwargs: Any) -> regression.Judgement:
    base: dict[str, Any] = {
        "score": 0.9,
        "deterministic_pass_rate": 1.0,
        "thresholds": TH,
        "degraded_spans": 0,
        "fail_on_degraded": True,
    }
    base.update(kwargs)
    return regression.judge_run(**base)


# ---------------------------------------------------------------------------
# verdict の決まり方(順序に意味がある)
# ---------------------------------------------------------------------------


def test_pass_without_baseline() -> None:
    judgement = _judge()
    assert judgement.verdict == regression.PASS
    assert "初版" in judgement.reason


def test_below_min_score_fails() -> None:
    assert _judge(score=0.5).verdict == regression.FAIL


def test_deterministic_failure_fails() -> None:
    judgement = _judge(deterministic_pass_rate=0.5)
    assert judgement.verdict == regression.FAIL
    assert "決定的評価" in judgement.reason


def test_regression_beyond_tolerance() -> None:
    judgement = _judge(score=0.80, baseline_score=0.90, baseline_run_id="r1")
    assert judgement.verdict == regression.REGRESSED
    assert judgement.baseline_run_id == "r1"


def test_small_drop_is_tolerated() -> None:
    judgement = _judge(score=0.89, baseline_score=0.90)
    assert judgement.verdict == regression.PASS


def test_improvement_passes() -> None:
    assert _judge(score=0.95, baseline_score=0.90).verdict == regression.PASS


def test_regression_is_checked_before_min_score() -> None:
    """下限は満たすが劣化している場合は regressed(fail ではない)。"""
    judgement = _judge(score=0.80, baseline_score=0.99)
    assert judgement.verdict == regression.REGRESSED


def test_degraded_beats_everything() -> None:
    judgement = _judge(score=1.0, degraded_spans=1)
    assert judgement.verdict == regression.FAIL
    assert "縮退実行" in judgement.reason


def test_degraded_check_can_be_disabled() -> None:
    """既定は有効。切った場合の挙動も固定しておく。"""
    assert _judge(degraded_spans=1, fail_on_degraded=False).verdict == regression.PASS


def test_wiring_check_is_its_own_verdict() -> None:
    judgement = _judge(mode=regression.MODE_WIRING_CHECK)
    assert judgement.verdict == regression.WIRING_CHECK
    assert not judgement.ok


def test_wiring_check_still_reports_real_problems() -> None:
    """配線確認でも、決定的評価が落ちていれば fail(配線の異常なので)。"""
    judgement = _judge(deterministic_pass_rate=0.0, mode=regression.MODE_WIRING_CHECK)
    assert judgement.verdict == regression.FAIL


# ---------------------------------------------------------------------------
# Groundedness(Step 2-3)
# ---------------------------------------------------------------------------


class _StubJudge(Judge):
    """Judge の代替。文ごとに固定スコアを返す/例外を投げる。"""

    def __init__(self, scores: list[float | Exception]) -> None:  # noqa: D107
        self.scores = list(scores)
        self.calls: list[str] = []

    def score(self, *, metric: str, prompt_id: str, variables: Any, trace_id: str) -> JudgeVerdict:
        self.calls.append(str(variables["output"]))
        value = self.scores.pop(0) if self.scores else 1.0
        if isinstance(value, Exception):
            raise value
        return JudgeVerdict(metric, float(value), "理由", [], "span", prompt_id, 1)


def test_split_sentences_skips_headings_and_bullets() -> None:
    text = "## 見出し\n- 箇条書きの項目です\n本文の文章がここにあります。短い。"
    sentences = groundedness.split_sentences(text)
    assert sentences == ["本文の文章がここにあります。"]


def test_groundedness_rate() -> None:
    judge = _StubJudge([1.0, 0.0])
    result = groundedness.evaluate(
        output="最初の文がここにあります。次の文もここにあります。",
        context="context",
        judge=judge,
        trace_id="t",
    )
    assert result.total == 2
    assert result.grounded == 1
    assert result.rate == pytest.approx(0.5)
    assert len(result.unattributed) == 1


def test_groundedness_errors_are_excluded_from_the_denominator() -> None:
    """採点できなかった文は分母から外す。「帰属していない」とみなさない。"""
    judge = _StubJudge([1.0, JudgeResponseError("形が違う")])
    result = groundedness.evaluate(
        output="最初の文がここにあります。次の文もここにあります。",
        context="context",
        judge=judge,
        trace_id="t",
    )
    assert result.errors == 1
    assert result.rate == pytest.approx(1.0)  # 1/1(採点できた文だけ)


def test_groundedness_without_sentences() -> None:
    result = groundedness.evaluate(
        output="## 見出しだけ", context="c", judge=_StubJudge([]), trace_id="t"
    )
    assert result.total == 0
    assert result.rate is None  # スコア欠損。0.0 ではない


def test_context_from_case() -> None:
    assert groundedness.context_from_case({"context": "本文"}) == "本文"
    assert groundedness.context_from_case({}) == ""


# ---------------------------------------------------------------------------
# レポート(Step 2-6)
# ---------------------------------------------------------------------------


def _record(repo: Repository, **kwargs: Any) -> str:
    defaults: dict[str, Any] = {
        "run_id": "r1", "suite_id": "s", "prompt_id": "p", "prompt_version": 1,
        "logical_model": "chat-standard", "judge_model": "judge", "total": 2,
        "mode": regression.MODE_EVALUATION, "trace_id": "t",
    }
    finish = {
        "passed": 2, "score": 0.9, "verdict": regression.PASS, "baseline_run_id": None,
        "cost_usd": 0.01, "errors": 0, "degraded_spans": 0, "note": "ok",
    }
    for key in list(kwargs):
        if key in finish:
            finish[key] = kwargs.pop(key)
    defaults.update(kwargs)
    repo.insert_eval_run(**defaults)
    repo.finish_eval_run(defaults["run_id"], **finish)
    return str(defaults["run_id"])


def test_run_report_includes_the_disclaimer(repo: Repository) -> None:
    """**Judge のスコアを絶対値として信用させない**注意書きを必ず出す。"""
    run_id = _record(repo)
    markdown = eval_run_report(repo, run_id)
    assert "絶対値として信用しない" in markdown
    assert "版間の相対比較" in markdown


def test_run_report_shows_violations(repo: Repository) -> None:
    run_id = _record(repo)
    repo.insert_eval_result(
        run_id=run_id, case_id="c1", metric="max_chars", kind="deterministic",
        passed=False, detail="1500字(上限1200字)",
    )
    markdown = eval_run_report(repo, run_id)
    assert "| max_chars | 1 |" in markdown


def test_run_report_lists_scoring_errors(repo: Repository) -> None:
    run_id = _record(repo, errors=1)
    repo.insert_eval_result(
        run_id=run_id, case_id="c1", metric="groundedness", kind="judge",
        passed=False, status="error", detail="reason が空です",
    )
    markdown = eval_run_report(repo, run_id)
    assert "採点できなかったケース" in markdown
    assert "reason が空です" in markdown


def test_run_report_warns_when_wiring_check(repo: Repository) -> None:
    run_id = _record(repo, mode=regression.MODE_WIRING_CHECK, verdict=regression.WIRING_CHECK)
    markdown = eval_run_report(repo, run_id)
    assert "配線確認モード" in markdown
    assert "publish の根拠にもなりません" in markdown


def test_run_report_flags_a_stale_suite(repo: Repository) -> None:
    """production-failure 由来が0件なら陳腐化のサインとして書く(Step 2-5)。"""
    run_id = _record(repo, suite_id="s")
    repo.upsert_eval_suite(suite_id="s", prompt_id="p", definition="x", thresholds_json="{}")
    repo.insert_eval_case(case_id="c1", suite_id="s", name="n", vars_json="{}")
    markdown = eval_run_report(repo, run_id)
    assert "陳腐化" in markdown


def test_run_report_for_unknown_run(repo: Repository) -> None:
    assert "見つかりません" in eval_run_report(repo, "nope")


def test_quality_report_summarises_health(repo: Repository) -> None:
    _record(repo, run_id="r1")
    _record(repo, run_id="r2", degraded_spans=3, verdict=regression.FAIL)
    markdown = quality_report(repo, since="2000-01-01 00:00:00")
    assert "評価基盤の健全性" in markdown
    assert "degraded を含む run: 1 / 2 件" in markdown
    assert "model health" in markdown


def test_quality_report_when_empty(repo: Repository) -> None:
    assert "評価実行はありません" in quality_report(repo, since="2999-01-01 00:00:00")
