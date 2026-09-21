"""Evaluation(Phase 2)— 決定的評価・Judge・Groundedness・回帰ゲート。

このパッケージの設計方針は一つ: **偽の合格を作らない**。
迷ったら「黙って通す」より「止める・欠損として残す」を選ぶ(NOTES.md N-026)。
"""

from __future__ import annotations

from llmops.eval.judge import Judge, JudgeResponseError, JudgeVerdict, parse_judge_payload
from llmops.eval.regression import (
    FAIL,
    MODE_EVALUATION,
    MODE_WIRING_CHECK,
    PASS,
    REGRESSED,
    WIRING_CHECK,
    Judgement,
    judge_run,
)
from llmops.eval.runner import CaseOutcome, EvalRunner, RunOutcome
from llmops.eval.suite import EvalSuite, Thresholds, load_dir, load_suite

__all__ = [
    "FAIL",
    "MODE_EVALUATION",
    "MODE_WIRING_CHECK",
    "PASS",
    "REGRESSED",
    "WIRING_CHECK",
    "CaseOutcome",
    "EvalRunner",
    "EvalSuite",
    "Judge",
    "JudgeResponseError",
    "JudgeVerdict",
    "Judgement",
    "RunOutcome",
    "Thresholds",
    "judge_run",
    "load_dir",
    "load_suite",
    "parse_judge_payload",
]
