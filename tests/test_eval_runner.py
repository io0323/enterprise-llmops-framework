"""評価スイートの実行と安全装置(Step 2-1 / 2-4)。

このファイルの半分は **N-026 の再発防止**。評価基盤が「偽の合格」を作れないことを、
実装ではなく**挙動**で固定する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from llmops.config import Config
from llmops.db.repository import Repository
from llmops.errors import EvaluationFailed
from llmops.eval import regression
from llmops.eval.closed_loop import add_manual_case
from llmops.eval.gate import PublishGate
from llmops.eval.runner import EvalRunner
from llmops.gateway import Runtime
from llmops.models import CompletionRequest
from llmops.sdk import LLMOps

SUITE = {
    "id": "elf-smoke",
    "prompt_id": "elf.smoke",
    "model": "mock-echo",
    "deterministic": [{"type": "non_empty"}, {"type": "max_chars", "value": 200}],
    "thresholds": {"deterministic_pass_rate": 1.0, "min_score": 0.0},
}


def write_suite(config: Config, raw: dict[str, Any], name: str = "elf-smoke") -> Path:
    config.evals_dir.mkdir(parents=True, exist_ok=True)
    path = config.evals_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture()
def runner(runtime: Runtime, config: Config) -> EvalRunner:
    write_suite(config, SUITE)
    runner = EvalRunner(runtime, LLMOps.from_runtime(runtime))
    runner.sync()
    return runner


def _add_case(runtime: Runtime, suite_id: str = "elf-smoke", message: str = "本文") -> str:
    return add_manual_case(runtime, suite_id, {"message": message}, name=f"case-{message}").case_id


# ---------------------------------------------------------------------------
# 基本
# ---------------------------------------------------------------------------


def test_run_records_a_verdict(runner: EvalRunner, runtime: Runtime, repo: Repository) -> None:
    _add_case(runtime)
    outcome = runner.run("elf-smoke")

    run = repo.get_eval_run(outcome.run_id)
    assert run is not None
    assert run["verdict"] == outcome.verdict
    assert run["total"] == 1
    assert run["trace_id"] == outcome.trace_id


def test_run_without_cases_is_refused(runner: EvalRunner) -> None:
    with pytest.raises(EvaluationFailed, match="評価ケースが1件もありません"):
        runner.run("elf-smoke")


def test_unknown_suite_is_refused(runner: EvalRunner) -> None:
    with pytest.raises(EvaluationFailed, match="評価スイートがありません"):
        runner.run("does-not-exist")


def test_generation_goes_through_the_gateway(
    runner: EvalRunner, runtime: Runtime, repo: Repository
) -> None:
    """評価専用の抜け道を作らない。評価のコストも spans に載る(Step 2-1)。"""
    _add_case(runtime)
    outcome = runner.run("elf-smoke")
    spans = repo.list_spans(outcome.trace_id)
    assert spans
    assert all(row["prompt_id"] == "elf.smoke" for row in spans)


def test_deterministic_failure_is_recorded(
    runner: EvalRunner, runtime: Runtime, config: Config, repo: Repository
) -> None:
    write_suite(config, {**SUITE, "deterministic": [{"type": "max_chars", "value": 1}]})
    runner.sync()
    _add_case(runtime, message="長い本文をここに書く")

    outcome = runner.run("elf-smoke")

    assert outcome.verdict == regression.FAIL
    assert outcome.cases[0].failed_rules == ["max_chars"]
    results = repo.list_eval_results(outcome.run_id)
    assert any(r["metric"] == "max_chars" and not r["passed"] for r in results)


# ---------------------------------------------------------------------------
# 決定的評価が落ちたら Judge を呼ばない(コスト節約 / Step 2-1 完了条件)
# ---------------------------------------------------------------------------


def test_judge_is_not_called_when_deterministic_fails(
    runtime: Runtime, config: Config, repo: Repository
) -> None:
    """**完了条件**: 決定的評価が落ちたケースで Judge の span が発生していないこと。"""
    write_suite(
        config,
        {
            **SUITE,
            "deterministic": [{"type": "max_chars", "value": 1}],
            "judge_model": "mock-echo",
            "judge": [{"metric": "readability", "prompt_id": "judge.readability"}],
        },
    )
    runner = EvalRunner(runtime, LLMOps.from_runtime(runtime))
    runner.sync()
    _add_case(runtime, message="長い本文をここに書く")

    outcome = runner.run("elf-smoke")

    judge_spans = [
        row for row in repo.list_spans(outcome.trace_id) if str(row["task"]).startswith("judge.")
    ]
    assert judge_spans == [], "決定的評価が落ちたのに Judge を呼んでいる"
    assert not any(r["kind"] == "judge" for r in repo.list_eval_results(outcome.run_id))


# ---------------------------------------------------------------------------
# 安全装置1: judge に fallback があれば実行しない
# ---------------------------------------------------------------------------


def test_judge_model_with_fallback_is_refused(
    runtime: Runtime, config: Config, workspace: Path
) -> None:
    """代替モデルで穴埋めされたスコアは使えない。**実行前に止める。**"""
    models = yaml.safe_load((workspace / "models.yaml").read_text(encoding="utf-8"))
    models["models"]["judge-with-fallback"] = {
        "adapter": "mock",
        "params": {"mode": "echo"},
        "fallback_to": "mock-echo",
    }
    (workspace / "models.yaml").write_text(yaml.safe_dump(models), encoding="utf-8")

    write_suite(
        config,
        {
            **SUITE,
            "judge_model": "judge-with-fallback",
            "judge": [{"metric": "readability", "prompt_id": "judge.readability"}],
        },
    )
    runner = EvalRunner(runtime, LLMOps.from_runtime(runtime))
    runtime.models.sync()
    runner.sync()
    add_manual_case(runtime, "elf-smoke", {"message": "本文"}, name="c")

    with pytest.raises(EvaluationFailed, match="fallback_to"):
        runner.run("elf-smoke")


def test_guard_can_be_disabled_by_config(runtime: Runtime, config: Config) -> None:
    """既定は有効。切れることは切れるが、切ったら自己責任と分かる形にしてある。"""
    relaxed = config.model_copy(
        update={"eval": config.eval.model_copy(update={"forbid_judge_fallback": False})}
    )
    runner = EvalRunner(Runtime.build(relaxed, repo=runtime.repo), LLMOps.from_runtime(runtime))
    runner._guard_judge_model("always-fail")  # fallback_to があっても素通りする


# ---------------------------------------------------------------------------
# 安全装置2: mock を通ったら配線確認モード(baseline にしない)
# ---------------------------------------------------------------------------


def test_mock_run_is_wiring_check(runner: EvalRunner, runtime: Runtime, repo: Repository) -> None:
    _add_case(runtime)
    outcome = runner.run("elf-smoke")

    assert outcome.mode == regression.MODE_WIRING_CHECK
    assert outcome.verdict == regression.WIRING_CHECK
    run = repo.get_eval_run(outcome.run_id)
    assert run is not None
    assert run["mode"] == regression.MODE_WIRING_CHECK


def test_wiring_check_is_not_a_baseline(
    runner: EvalRunner, runtime: Runtime, repo: Repository
) -> None:
    """配線確認の run は baseline 検索に出てこない(mode で絞っている)。"""
    _add_case(runtime)
    outcome = runner.run("elf-smoke")
    assert repo.get_eval_run(outcome.run_id) is not None
    assert repo.latest_eval_run("elf.smoke", 1) is None  # mode='evaluation' が無い


def test_wiring_check_never_satisfies_the_publish_gate(
    runner: EvalRunner, runtime: Runtime, repo: Repository
) -> None:
    """配線確認では publish できない。**これが N-026 の再発防止の要。**"""
    _add_case(runtime)
    runner.run("elf-smoke")

    gate = PublishGate(repo)
    with pytest.raises(EvaluationFailed, match="評価実行がありません"):
        gate("elf.smoke", 1)


# ---------------------------------------------------------------------------
# 安全装置3: degraded が混ざったら無条件 fail
# ---------------------------------------------------------------------------


def test_degraded_span_forces_fail(
    runner: EvalRunner, runtime: Runtime, repo: Repository
) -> None:
    """縮退実行(Fallback)の結果で品質を判定しない。"""
    _add_case(runtime)
    outcome = runner.run("elf-smoke")

    # この run の span を後から degraded に変え、同じ判定ロジックを通す
    repo.conn.execute("UPDATE spans SET degraded = 1 WHERE trace_id = ?", (outcome.trace_id,))
    repo.conn.commit()
    cost, degraded = repo.trace_totals(outcome.trace_id)
    assert degraded > 0

    judgement = regression.judge_run(
        score=1.0,
        deterministic_pass_rate=1.0,
        thresholds=runner.thresholds(),
        degraded_spans=degraded,
        fail_on_degraded=True,
    )
    assert judgement.verdict == regression.FAIL
    assert "縮退実行" in judgement.reason


def test_degraded_is_checked_before_anything_else() -> None:
    """スコアが満点でも degraded があれば fail。中身を見ずに止める。"""
    from llmops.eval.suite import Thresholds

    judgement = regression.judge_run(
        score=1.0,
        deterministic_pass_rate=1.0,
        thresholds=Thresholds(),
        degraded_spans=1,
        fail_on_degraded=True,
        baseline_score=0.5,
    )
    assert judgement.verdict == regression.FAIL


# ---------------------------------------------------------------------------
# Judge の失敗はスコア欠損(run は止めない)
# ---------------------------------------------------------------------------


def test_judge_parse_failure_becomes_an_error_not_a_score(
    runtime: Runtime, config: Config, repo: Repository
) -> None:
    """mock echo はプロンプトを返すので Judge のスキーマに適合しない。

    そこで**スコアを作らず** error として記録し、run は止めない。
    N-026 ではここで偽のスコアが通っていた。
    """
    write_suite(
        config,
        {
            **SUITE,
            "judge_model": "mock-echo",
            "judge": [{"metric": "readability", "prompt_id": "judge.readability"}],
        },
    )
    runner = EvalRunner(runtime, LLMOps.from_runtime(runtime))
    runner.sync()
    _add_case(runtime)

    outcome = runner.run("elf-smoke")

    assert outcome.errors == 1
    assert outcome.cases[0].judged == {}
    assert outcome.score is None
    results = repo.list_eval_results(outcome.run_id)
    judge_rows = [r for r in results if r["kind"] == "judge"]
    assert len(judge_rows) == 1
    assert judge_rows[0]["status"] == "error"
    assert judge_rows[0]["score"] is None


def test_score_missing_is_not_a_pass(runtime: Runtime, config: Config) -> None:
    """採点できなかった run を合格にしない。"""
    from llmops.eval.suite import Thresholds

    judgement = regression.judge_run(
        score=None,
        deterministic_pass_rate=1.0,
        thresholds=Thresholds(min_score=0.0),
        degraded_spans=0,
        fail_on_degraded=True,
    )
    assert judgement.verdict == regression.FAIL
    assert "算出できません" in judgement.reason


# ---------------------------------------------------------------------------
# 閉ループ(Step 2-5)
# ---------------------------------------------------------------------------


def test_add_case_from_span(runner: EvalRunner, runtime: Runtime, repo: Repository) -> None:
    from llmops.eval.closed_loop import add_case_from_span

    trace_id = runtime.tracer.start_trace("article.generate", external_id="req-1")
    result = runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "本番の入力"}, model="mock-echo",
        )
    )

    case = add_case_from_span(runtime, "elf-smoke", result.span_id)

    rows = repo.list_eval_cases("elf-smoke")
    assert len(rows) == 1
    assert rows[0]["origin"] == "production-failure"
    assert rows[0]["source_span_id"] == result.span_id
    variables = json.loads(rows[0]["vars_json"])
    assert "本番の入力" in variables["_rendered_prompt"]
    # 復元できない変数は空文字にし、その事実を返す(推測で埋めない)
    assert case.missing_variables == ["message"]
    assert variables["message"] == ""


def test_add_case_rejects_duplicate_span(runner: EvalRunner, runtime: Runtime) -> None:
    from llmops.eval.closed_loop import add_case_from_span

    trace_id = runtime.tracer.start_trace("t")
    result = runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo",
        )
    )
    add_case_from_span(runtime, "elf-smoke", result.span_id)
    with pytest.raises(EvaluationFailed, match="既にあります"):
        add_case_from_span(runtime, "elf-smoke", result.span_id)


def test_add_case_rejects_span_from_another_prompt(
    runner: EvalRunner, runtime: Runtime, repo: Repository
) -> None:
    from llmops.eval.closed_loop import add_case_from_span

    trace_id = runtime.tracer.start_trace("t")
    result = runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo",
        )
    )
    repo.conn.execute(
        "UPDATE spans SET prompt_id = 'other.prompt' WHERE id = ?", (result.span_id,)
    )
    repo.conn.commit()
    with pytest.raises(EvaluationFailed, match="違います"):
        add_case_from_span(runtime, "elf-smoke", result.span_id)


def test_origin_breakdown(runner: EvalRunner, runtime: Runtime, repo: Repository) -> None:
    """出自内訳が取れること(陳腐化の検出に使う)。"""
    _add_case(runtime, message="a")
    from llmops.eval.closed_loop import add_case_from_span

    trace_id = runtime.tracer.start_trace("t")
    result = runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo",
        )
    )
    add_case_from_span(runtime, "elf-smoke", result.span_id)

    assert repo.count_eval_cases_by_origin("elf-smoke") == {
        "manual": 1,
        "production-failure": 1,
    }


# ---------------------------------------------------------------------------
# publish 前の候補版を評価できること(N-038)
#
# これができないと「評価してから publish」が原理的に成立しない
# (Gateway は published 以外を拒否し、ゲートは評価済みを要求するため)。
# ---------------------------------------------------------------------------


def test_candidate_version_can_be_evaluated(
    runner: EvalRunner, runtime: Runtime, workspace: Path, repo: Repository
) -> None:
    path = workspace / "prompts" / "elf" / "smoke.md"
    path.write_text(path.read_text().replace("書き写して", "写して"), encoding="utf-8")
    version = next(r.version for r in runtime.prompts.sync() if r.prompt_id == "elf.smoke")
    assert runtime.prompts.resolve("elf.smoke", version).status == "draft"
    _add_case(runtime)

    outcome = runner.run("elf-smoke", version=version)

    assert outcome.prompt_version == version
    assert outcome.errors == 0, "publish 前の版が評価できていない"
    span = repo.list_spans(outcome.trace_id)[0]
    assert span["prompt_version"] == version


def test_gateway_still_refuses_unpublished_in_the_app_path(
    runtime: Runtime, workspace: Path
) -> None:
    """緩めたのは評価経路だけ。アプリの通常呼び出しは従来どおり拒否する。"""
    from llmops.errors import PromptNotPublished

    path = workspace / "prompts" / "elf" / "smoke.md"
    path.write_text(path.read_text().replace("書き写して", "写して"), encoding="utf-8")
    runtime.prompts.sync()
    trace_id = runtime.tracer.start_trace("t")
    with pytest.raises(PromptNotPublished):
        runtime.gateway.complete(
            CompletionRequest(
                trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
                variables={"message": "x"}, model="mock-echo", version=2,
            )
        )
