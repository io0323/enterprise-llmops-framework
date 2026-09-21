"""決定的評価(Step 2-1)。**LLMを一切呼ばない**ことも含めて検証する。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llmops.errors import EvaluationFailed
from llmops.eval.deterministic import (
    RuleOutcome,
    all_passed,
    evaluate,
    extract_tokens,
    unattributed_tokens,
)
from llmops.eval.suite import DeterministicRule


def rule(**kwargs: object) -> DeterministicRule:
    return DeterministicRule.from_dict(dict(kwargs), suite_id="t")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 基本ルール
# ---------------------------------------------------------------------------


def test_non_empty() -> None:
    assert evaluate("本文", [rule(type="non_empty")], {})[0].passed
    assert not evaluate("   ", [rule(type="non_empty")], {})[0].passed


def test_max_chars() -> None:
    assert evaluate("あ" * 10, [rule(type="max_chars", value=10)], {})[0].passed
    outcome = evaluate("あ" * 11, [rule(type="max_chars", value=10)], {})[0]
    assert not outcome.passed
    assert "11字" in outcome.detail


def test_min_chars() -> None:
    assert evaluate("あ" * 10, [rule(type="min_chars", value=10)], {})[0].passed
    assert not evaluate("あ", [rule(type="min_chars", value=10)], {})[0].passed


def test_forbidden_patterns() -> None:
    outcome = evaluate("本文\n```\ncode\n```", [rule(type="forbidden_patterns",
                                                    patterns=["```"])], {})[0]
    assert not outcome.passed
    assert outcome.violations == ["```"]


def test_required_patterns() -> None:
    outcome = evaluate("本文", [rule(type="required_patterns", patterns=["## 見出し"])], {})[0]
    assert not outcome.passed
    assert outcome.violations == ["## 見出し"]


def test_unknown_rule_type_is_rejected_at_load() -> None:
    with pytest.raises(EvaluationFailed, match="未対応の決定的評価"):
        rule(type="vibes")


# ---------------------------------------------------------------------------
# no_outside_context — このプロジェクトの肝(CGMP 絶対ルール9 の機械検証)
# ---------------------------------------------------------------------------


def test_extract_tokens_picks_numbers_katakana_and_alnum() -> None:
    tokens = extract_tokens("2026年にClaude Codeで5自治体へ申請した")
    assert "2026" in tokens
    assert "5" in tokens
    assert "Claude" in tokens


def test_attributed_text_passes() -> None:
    context = "寄附先は5自治体以内(2026年時点)。Claude Code を使う。"
    output = "寄附先は5自治体以内だ(2026年時点)。"
    assert unattributed_tokens(output, context) == []


def test_unattributed_number_is_flagged() -> None:
    """context に無い数値を書いたら拾う(これが最も重要な検出)。"""
    context = "寄附先は5自治体以内。"
    output = "寄附先は7自治体以内で、還元率は30%だ。"
    violations = unattributed_tokens(output, context)
    assert "7" in violations
    assert "30" in violations


def test_allowlist_suppresses_false_positives() -> None:
    """誤検出は allowlist で潰す。潰せないと検査が使われなくなる。"""
    context = "制度の説明。"
    output = "Markdown で書く。Claude Code を使う。"
    assert "Claude" in unattributed_tokens(output, context)
    assert "Claude" not in unattributed_tokens(output, context, allowlist=["Claude", "Code"])


def test_no_outside_context_rule(  ) -> None:
    outcome = evaluate(
        "寄附先は7自治体だ。",
        [rule(type="no_outside_context", context_var="context")],
        {"context": "寄附先は5自治体以内。"},
    )[0]
    assert not outcome.passed
    assert outcome.violations is not None
    assert "7" in outcome.violations


def test_no_outside_context_skips_when_context_is_empty() -> None:
    """context が無いケースで当てると全トークンが違反になる。そこは通す。"""
    outcome = evaluate(
        "何かの本文。2026年。",
        [rule(type="no_outside_context", context_var="context")],
        {"context": ""},
    )[0]
    assert outcome.passed
    assert "スキップ" in outcome.detail


# ---------------------------------------------------------------------------
# json_schema
# ---------------------------------------------------------------------------


def test_json_schema_rejects_non_json() -> None:
    outcome = evaluate("これはJSONではない", [rule(type="json_schema")], {})[0]
    assert not outcome.passed


def test_json_schema_accepts_valid_json_without_schema_file() -> None:
    outcome = evaluate('{"a": 1}', [rule(type="json_schema")], {})[0]
    assert outcome.passed


def test_json_schema_validates_against_file(tmp_path: Path) -> None:
    schema = tmp_path / "s.json"
    schema.write_text(
        json.dumps({"type": "object", "required": ["sections"]}), encoding="utf-8"
    )
    passing = evaluate(
        '{"sections": []}', [rule(type="json_schema", schema_file="s.json")], {}, base_dir=tmp_path
    )[0]
    failing = evaluate(
        '{"other": 1}', [rule(type="json_schema", schema_file="s.json")], {}, base_dir=tmp_path
    )[0]
    assert passing.passed
    assert not failing.passed


def test_json_schema_missing_file_is_a_failure() -> None:
    """スキーマが見つからないときに「合格」にしない(黙って通さない)。"""
    outcome = evaluate(
        '{"a": 1}', [rule(type="json_schema", schema_file="nope.json")], {}, base_dir=Path("/tmp")
    )[0]
    assert not outcome.passed
    assert "スキーマファイルがありません" in outcome.detail


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def test_all_passed() -> None:
    assert all_passed([RuleOutcome("a", True), RuleOutcome("b", True)])
    assert not all_passed([RuleOutcome("a", True), RuleOutcome("b", False)])


def test_evaluate_never_calls_an_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """決定的評価は純コード。subprocess も HTTP も使わない(Step 2-1)。"""
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("決定的評価が外部プロセスを呼んだ")
    )
    outcomes = evaluate(
        "寄附先は5自治体以内だ。",
        [
            rule(type="non_empty"),
            rule(type="max_chars", value=100),
            rule(type="no_outside_context"),
        ],
        {"context": "寄附先は5自治体以内。"},
    )
    assert all_passed(outcomes)
