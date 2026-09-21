"""評価スイートの実行。

設計上の固定判断:

- 生成も Judge も**通常の Gateway 経由**で呼ぶ。評価専用の抜け道を作らない。
  評価自体のコストも `spans` に載る(実装指示 Step 2-1)
- 1 run = 1 trace。degraded と実コストはその trace から導出する
- 決定的評価が落ちたケースは Judge を呼ばない(`eval.deterministic_first`)
- Judge の失敗は `error` として記録し、run は止めない。スコアは欠損として扱う

安全装置(NOTES.md N-026 の再発防止)。いずれも既定で有効:

- `forbid_judge_fallback`: judge 論理モデルに `fallback_to` があれば**実行前に拒否**
- `fail_on_degraded`: degraded な span が混ざった run は verdict を fail に倒す
- `mock_is_wiring_check`: mock Adapter を通る run は `mode='wiring_check'` として記録し、
  baseline にも publish の根拠にもしない
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llmops.errors import EvaluationFailed, LLMOpsError
from llmops.eval import deterministic, regression
from llmops.eval.judge import Judge, JudgeResponseError, weighted_average
from llmops.eval.suite import EvalSuite, Thresholds, load_dir, load_suite
from llmops.logging_utils import get_logger
from llmops.observability.tracer import new_id

logger = get_logger(__name__)

MOCK_ADAPTER = "mock"
DETERMINISTIC = "deterministic"
JUDGE = "judge"


@dataclass
class CaseOutcome:
    """1ケースぶんの結果。"""

    case_id: str
    name: str
    deterministic_passed: bool
    score: float | None = None
    judged: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    generation_span_id: str | None = None
    failed_rules: list[str] = field(default_factory=list)


@dataclass
class RunOutcome:
    run_id: str
    suite_id: str
    prompt_id: str
    prompt_version: int
    mode: str
    verdict: str
    reason: str
    score: float | None
    passed: int
    total: int
    errors: int
    degraded_spans: int
    cost_usd: float
    baseline_run_id: str | None
    trace_id: str
    cases: list[CaseOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == regression.PASS


class EvalRunner:
    """スイート1つを実行する。"""

    def __init__(self, runtime: Any, ops: Any) -> None:
        self.runtime = runtime
        self.ops = ops
        self.config = runtime.config

    # ------------------------------------------------------------------
    def thresholds(self) -> Thresholds:
        return Thresholds(
            min_score=self.config.eval.min_score,
            regression_tolerance=self.config.eval.regression_tolerance,
        )

    def load_suites(self) -> dict[str, EvalSuite]:
        return load_dir(self.config.evals_dir, defaults=self.thresholds())

    def load(self, suite_id: str) -> EvalSuite:
        path = self.config.evals_dir / f"{suite_id}.yaml"
        if not path.is_file():
            raise EvaluationFailed(f"評価スイートがありません: {path}")
        return load_suite(path, defaults=self.thresholds())

    def sync(self) -> list[str]:
        """`evals/*.yaml` を DB へ取り込む(ケースは別管理)。"""
        synced: list[str] = []
        for suite in self.load_suites().values():
            self.runtime.repo.upsert_eval_suite(
                suite_id=suite.id,
                prompt_id=suite.prompt_id,
                definition=suite.definition,
                thresholds_json=json.dumps(
                    {
                        "deterministic_pass_rate": suite.thresholds.deterministic_pass_rate,
                        "min_score": suite.thresholds.min_score,
                        "regression_tolerance": suite.thresholds.regression_tolerance,
                    },
                    ensure_ascii=False,
                ),
            )
            synced.append(suite.id)
        return synced

    # ------------------------------------------------------------------
    def _guard_judge_model(self, judge_model: str) -> None:
        """judge 論理モデルに Fallback があれば**実行前に止める**。

        代替モデルで穴埋めされたスコアは「誰が採点したか」が分からず使えない。
        黙って別モデルの点数を採用するくらいなら、実行しないほうがよい。
        """
        if not self.config.eval.forbid_judge_fallback:
            return
        model = self.runtime.models.resolve(judge_model)
        if model.fallback_to:
            raise EvaluationFailed(
                f"judge 論理モデル '{judge_model}' に fallback_to='{model.fallback_to}' が"
                " 設定されています。Judge が落ちたときに別モデルの点数が混ざるため、"
                " 評価を実行しません(models.yaml から fallback_to を外してください)"
            )

    def _resolve_mode(self, *model_names: str) -> str:
        """mock Adapter が経路に入るなら配線確認モードにする。"""
        if not self.config.eval.mock_is_wiring_check:
            return regression.MODE_EVALUATION
        for name in model_names:
            if self.runtime.models.resolve(name).adapter == MOCK_ADAPTER:
                logger.warning(
                    "論理モデル '%s' は mock Adapter です。この実行は配線確認モードとして"
                    "記録し、baseline にも publish の根拠にもしません",
                    name,
                )
                return regression.MODE_WIRING_CHECK
        return regression.MODE_EVALUATION

    # ------------------------------------------------------------------
    def run(
        self,
        suite_id: str,
        *,
        version: int | None = None,
        model: str | None = None,
        judge_model: str | None = None,
    ) -> RunOutcome:
        suite = self.load(suite_id)
        repo = self.runtime.repo

        prompt = self.runtime.prompts.resolve(suite.prompt_id, version)
        generation_model = model or suite.model or prompt.default_model
        if not generation_model:
            raise EvaluationFailed(
                f"{suite_id}: 生成に使う論理モデルが決まりません"
                "(--model かスイートの model、Prompt の front matter のいずれかで指定する)"
            )
        judge_name = judge_model or suite.judge_model or self.config.eval.judge_model

        if suite.judge:
            self._guard_judge_model(judge_name)
        judged_models = [judge_name] if suite.judge else []
        mode = self._resolve_mode(generation_model, *judged_models)

        cases = repo.list_eval_cases(suite_id)
        if not cases:
            raise EvaluationFailed(f"{suite_id}: 評価ケースが1件もありません")

        run_id = new_id()
        trace_id = self.runtime.tracer.start_trace(
            "eval.run", external_id=run_id, meta={"suite": suite_id, "mode": mode}
        )
        repo.insert_eval_run(
            run_id=run_id,
            suite_id=suite_id,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            logical_model=generation_model,
            judge_model=judge_name if suite.judge else None,
            total=len(cases),
            mode=mode,
            trace_id=trace_id,
        )

        outcomes = [
            self._run_case(
                run_id=run_id,
                trace_id=trace_id,
                suite=suite,
                prompt=prompt,
                case=case,
                generation_model=generation_model,
                judge_name=judge_name,
            )
            for case in cases
        ]

        return self._finish(run_id, trace_id, suite, prompt, mode, outcomes)

    # ------------------------------------------------------------------
    def _run_case(
        self,
        *,
        run_id: str,
        trace_id: str,
        suite: EvalSuite,
        prompt: Any,
        case: Any,
        generation_model: str,
        judge_name: str,
    ) -> CaseOutcome:
        repo = self.runtime.repo
        case_id = str(case["id"])
        variables = json.loads(case["vars_json"])
        outcome = CaseOutcome(case_id=case_id, name=str(case["name"]), deterministic_passed=False)

        # --- 生成(通常の Gateway 経由。評価専用の抜け道は作らない)-------------
        try:
            result = self.ops.complete(
                prompt_id=prompt.prompt_id,
                variables=variables,
                model=generation_model,
                version=prompt.version,
                task=suite.task or "eval",
                as_json=suite.as_json,
                trace_id=trace_id,
                # 評価対象は publish 前の候補版であることが普通。
                # 状態チェックだけを緩める(Guard / Trace / Cost は通常どおり)
                allow_unpublished=True,
            )
        except LLMOpsError as exc:
            outcome.errors.append(f"generation: {exc}")
            repo.insert_eval_result(
                run_id=run_id, case_id=case_id, metric="generation", kind=DETERMINISTIC,
                passed=False, status="error", detail=str(exc)[:1000],
            )
            return outcome

        outcome.generation_span_id = result.span_id
        output = result.text

        # --- 決定的評価(LLMを呼ばない)---------------------------------------
        rule_outcomes = deterministic.evaluate(
            output, suite.deterministic, variables, base_dir=self.config.evals_dir
        )
        for rule in rule_outcomes:
            repo.insert_eval_result(
                run_id=run_id, case_id=case_id, metric=rule.rule, kind=DETERMINISTIC,
                passed=rule.passed, score=1.0 if rule.passed else 0.0,
                detail=rule.as_detail() or None, span_id=result.span_id,
            )
        outcome.deterministic_passed = deterministic.all_passed(rule_outcomes)
        outcome.failed_rules = [r.rule for r in rule_outcomes if not r.passed]

        if not outcome.deterministic_passed and self.config.eval.deterministic_first:
            # Judge を呼ばずに fail 確定(コスト節約。CGMP 絶対ルール6と同方針)
            logger.info(
                "決定的評価が落ちたため Judge をスキップします (case=%s, rules=%s)",
                outcome.name, ", ".join(outcome.failed_rules),
            )
            return outcome

        # --- Judge ------------------------------------------------------------
        if not suite.judge:
            outcome.score = 1.0 if outcome.deterministic_passed else 0.0
            return outcome

        judge = Judge(self.ops, model=judge_name)
        weights = {metric.metric: metric.weight for metric in suite.judge}
        for metric in suite.judge:
            try:
                verdict = judge.score(
                    metric=metric.metric,
                    prompt_id=metric.prompt_id,
                    variables=self._judge_variables(metric.metric, variables, output),
                    trace_id=trace_id,
                )
            except (JudgeResponseError, LLMOpsError) as exc:
                # **代替モデルで穴埋めしない。** 採点できなかった事実を残す
                outcome.errors.append(f"{metric.metric}: {exc}")
                repo.insert_eval_result(
                    run_id=run_id, case_id=case_id, metric=metric.metric, kind=JUDGE,
                    passed=False, status="error", detail=str(exc)[:1000],
                )
                continue
            outcome.judged[metric.metric] = verdict.score
            repo.insert_eval_result(
                run_id=run_id, case_id=case_id, metric=metric.metric, kind=JUDGE,
                passed=True, score=verdict.score, span_id=verdict.span_id,
                detail=json.dumps(
                    {"reason": verdict.reason, "violations": verdict.violations},
                    ensure_ascii=False,
                ),
            )

        outcome.score = weighted_average(outcome.judged, weights)
        return outcome

    @staticmethod
    def _judge_variables(
        metric: str, case_variables: dict[str, Any], output: str
    ) -> dict[str, Any]:
        """Judge Prompt へ渡す変数。指標ごとに必要なものだけを渡す。"""
        base: dict[str, Any] = {"output": output}
        if metric == "groundedness":
            base["context"] = str(case_variables.get("context", "") or "")
        if metric == "relevance":
            base["instruction"] = str(
                case_variables.get("instruction")
                or case_variables.get("section_heading")
                or case_variables.get("points")
                or case_variables.get("topic")
                or ""
            )
        return base

    # ------------------------------------------------------------------
    def _finish(
        self,
        run_id: str,
        trace_id: str,
        suite: EvalSuite,
        prompt: Any,
        mode: str,
        outcomes: list[CaseOutcome],
    ) -> RunOutcome:
        repo = self.runtime.repo
        total = len(outcomes)
        deterministic_passed = sum(1 for o in outcomes if o.deterministic_passed)
        pass_rate = deterministic_passed / total if total else 0.0
        scored = [o.score for o in outcomes if o.score is not None]
        score = sum(scored) / len(scored) if scored else None
        errors = sum(len(o.errors) for o in outcomes)

        cost_usd, degraded_spans = repo.trace_totals(trace_id)
        baseline = self._baseline(prompt.prompt_id, prompt.version)

        judgement = regression.judge_run(
            score=score,
            deterministic_pass_rate=pass_rate,
            thresholds=suite.thresholds,
            degraded_spans=degraded_spans,
            fail_on_degraded=self.config.eval.fail_on_degraded,
            baseline_score=None if baseline is None else baseline[1],
            baseline_run_id=None if baseline is None else baseline[0],
            mode=mode,
        )

        passed = sum(
            1 for o in outcomes if o.deterministic_passed and (o.score is None or o.score > 0)
        )
        repo.finish_eval_run(
            run_id,
            passed=passed,
            score=score,
            verdict=judgement.verdict,
            baseline_run_id=judgement.baseline_run_id,
            cost_usd=cost_usd,
            errors=errors,
            degraded_spans=degraded_spans,
            note=judgement.reason,
        )
        self.runtime.tracer.end_trace(
            trace_id, status="success" if judgement.ok else "partial"
        )
        repo.insert_audit_log(
            event="eval.run",
            actor=self.config.system,
            subject=f"{suite.id}:{prompt.prompt_id}@{prompt.version}",
            detail={"run_id": run_id, "verdict": judgement.verdict, "mode": mode,
                    "score": score, "errors": errors, "degraded_spans": degraded_spans},
        )

        return RunOutcome(
            run_id=run_id,
            suite_id=suite.id,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            mode=mode,
            verdict=judgement.verdict,
            reason=judgement.reason,
            score=score,
            passed=passed,
            total=total,
            errors=errors,
            degraded_spans=degraded_spans,
            cost_usd=cost_usd,
            baseline_run_id=judgement.baseline_run_id,
            trace_id=trace_id,
            cases=outcomes,
        )

    def _baseline(self, prompt_id: str, version: int) -> tuple[str, float] | None:
        """ベースライン = 現在 published の版に対する最新の評価実行。

        評価対象が published 版そのものの場合はベースラインなし(自分と比べない)。
        """
        deployment = self.runtime.repo.get_deployment(prompt_id)
        if deployment is None:
            return None
        active = int(deployment["active_version"])
        if active == version:
            return None
        run = self.runtime.repo.latest_eval_run(prompt_id, active)
        if run is None or run["score"] is None:
            return None
        return str(run["id"]), float(run["score"])


def suite_paths(evals_dir: Path) -> list[Path]:
    return sorted(evals_dir.glob("*.yaml")) if evals_dir.is_dir() else []
