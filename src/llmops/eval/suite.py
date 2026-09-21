"""評価スイート定義(`evals/<suite_id>.yaml`)の読み込み。

閾値・重み・禁止パターンは全てここ(YAML)から読む。コードに書かない(絶対ルール12)。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llmops.errors import EvaluationFailed

#: 決定的評価で使えるルール種別(`eval/deterministic.py` の実装と1対1)
RULE_TYPES = (
    "non_empty",
    "max_chars",
    "min_chars",
    "forbidden_patterns",
    "required_patterns",
    "no_outside_context",
    "json_schema",
)


@dataclass(frozen=True)
class DeterministicRule:
    """決定的評価1件。LLMを呼ばない。"""

    type: str
    value: Any = None
    patterns: list[str] = field(default_factory=list)
    context_var: str = "context"
    allowlist: list[str] = field(default_factory=list)
    schema_file: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, suite_id: str) -> DeterministicRule:
        rule_type = str(raw.get("type", ""))
        if rule_type not in RULE_TYPES:
            raise EvaluationFailed(
                f"{suite_id}: 未対応の決定的評価です: {rule_type}"
                f"(使えるのは {', '.join(RULE_TYPES)})"
            )
        return cls(
            type=rule_type,
            value=raw.get("value"),
            patterns=[str(p) for p in raw.get("patterns", [])],
            context_var=str(raw.get("context_var", "context")),
            allowlist=[str(a) for a in raw.get("allowlist", [])],
            schema_file=None if raw.get("schema_file") is None else str(raw["schema_file"]),
        )


@dataclass(frozen=True)
class JudgeMetric:
    """LLM-as-Judge の1指標。"""

    metric: str
    prompt_id: str
    weight: float = 1.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, suite_id: str) -> JudgeMetric:
        for key in ("metric", "prompt_id"):
            if not raw.get(key):
                raise EvaluationFailed(f"{suite_id}: judge の {key} が未指定です")
        return cls(
            metric=str(raw["metric"]),
            prompt_id=str(raw["prompt_id"]),
            weight=float(raw.get("weight", 1.0)),
        )


@dataclass(frozen=True)
class Thresholds:
    """合否閾値。スイートで上書きしなければ config.yaml の値を使う。"""

    deterministic_pass_rate: float = 1.0
    min_score: float = 0.75
    regression_tolerance: float = 0.02


@dataclass(frozen=True)
class EvalSuite:
    id: str
    prompt_id: str
    source_path: Path
    definition: str
    model: str | None = None
    judge_model: str | None = None
    as_json: bool = False
    task: str | None = None
    deterministic: list[DeterministicRule] = field(default_factory=list)
    judge: list[JudgeMetric] = field(default_factory=list)
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def definition_hash(self) -> str:
        return hashlib.sha256(self.definition.encode("utf-8")).hexdigest()[:16]

    @property
    def judge_weight_total(self) -> float:
        return sum(metric.weight for metric in self.judge)


def load_suite(path: Path, *, defaults: Thresholds | None = None) -> EvalSuite:
    """1ファイルを読む。suite_id はファイル名から取り、`id` と食い違えばエラー。"""
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise EvaluationFailed(f"{path}: スイート定義がマッピングではありません")

    suite_id = path.stem
    declared = raw.get("id")
    if declared is not None and str(declared) != suite_id:
        raise EvaluationFailed(
            f"{path}: id({declared})がファイル名由来の id({suite_id})と一致しません"
        )
    prompt_id = raw.get("prompt_id")
    if not prompt_id:
        raise EvaluationFailed(f"{path}: prompt_id が未指定です")

    base = defaults or Thresholds()
    raw_thresholds = raw.get("thresholds") or {}
    thresholds = Thresholds(
        deterministic_pass_rate=float(
            raw_thresholds.get("deterministic_pass_rate", base.deterministic_pass_rate)
        ),
        min_score=float(raw_thresholds.get("min_score", base.min_score)),
        regression_tolerance=float(
            raw_thresholds.get("regression_tolerance", base.regression_tolerance)
        ),
    )

    return EvalSuite(
        id=suite_id,
        prompt_id=str(prompt_id),
        source_path=path,
        definition=text,
        model=None if raw.get("model") is None else str(raw["model"]),
        judge_model=None if raw.get("judge_model") is None else str(raw["judge_model"]),
        as_json=bool(raw.get("as_json", False)),
        task=None if raw.get("task") is None else str(raw["task"]),
        deterministic=[
            DeterministicRule.from_dict(rule, suite_id=suite_id)
            for rule in raw.get("deterministic") or []
        ],
        judge=[
            JudgeMetric.from_dict(metric, suite_id=suite_id) for metric in raw.get("judge") or []
        ],
        thresholds=thresholds,
    )


def load_dir(evals_dir: Path, *, defaults: Thresholds | None = None) -> dict[str, EvalSuite]:
    """`evals/*.yaml` を全て読む。"""
    if not evals_dir.is_dir():
        return {}
    suites: dict[str, EvalSuite] = {}
    for path in sorted(evals_dir.glob("*.yaml")):
        suite = load_suite(path, defaults=defaults)
        suites[suite.id] = suite
    return suites
