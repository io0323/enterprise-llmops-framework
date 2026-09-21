"""Judge の応答パースと重み付き平均(Step 2-2)。

**N-026 の直接の再発防止テスト。** `extract_json` は Prompt 内の出力例 JSON を
拾いうるので、「JSONとして読めた」だけでスコアを作らせない。
期待スキーマまで通って初めて採点成功とする。
"""

from __future__ import annotations

import pytest

from llmops.eval.judge import JudgeResponseError, parse_judge_payload, weighted_average

VALID = {"score": 0.8, "reason": "context に帰属している", "violations": []}


def test_valid_payload() -> None:
    score, reason, violations = parse_judge_payload(VALID, metric="groundedness")
    assert score == 0.8
    assert reason == "context に帰属している"
    assert violations == []


def test_single_element_list_is_unwrapped() -> None:
    """「オブジェクトを配列で包む」応答は実機で頻出するので1件だけ許す。"""
    score, _, _ = parse_judge_payload([VALID], metric="groundedness")
    assert score == 0.8


def test_multi_element_list_is_rejected() -> None:
    """2件以上は「どれが採点か」決められない。推測で1つ選ばない。"""
    with pytest.raises(JudgeResponseError, match="1つに決められません"):
        parse_judge_payload([VALID, VALID], metric="groundedness")


def test_violations_are_kept() -> None:
    payload = {**VALID, "violations": ["「2026年時点」は context に無い"]}
    _, _, violations = parse_judge_payload(payload, metric="groundedness")
    assert violations == ["「2026年時点」は context に無い"]


# ---------------------------------------------------------------------------
# N-026 の再発防止: スキーマに合わない JSON を「採点成功」にしない
# ---------------------------------------------------------------------------


def test_prompt_example_json_is_not_a_score() -> None:
    """Prompt 内の出力例(プレースホルダ)を拾っても採点にしない。

    N-026 で実際に起きたのは、mock がプロンプトをそのまま返し、その中の
    出力例 JSON を `extract_json` が拾って**偽のスコアが通った**という事故。
    """
    example = {"score": "0.0〜1.0の数値", "reason": "判断の根拠(必須)", "violations": []}
    with pytest.raises(JudgeResponseError, match="数値ではありません"):
        parse_judge_payload(example, metric="groundedness")


def test_cgmp_rubric_example_is_not_a_score() -> None:
    """N-026 の実例。rubric プロンプトの出力例は score を持たない。"""
    rubric_example = {"outline_alignment": 85, "claim_consistency": 90, "audience_fit": 75}
    with pytest.raises(JudgeResponseError, match="score がありません"):
        parse_judge_payload(rubric_example, metric="groundedness")


def test_non_object_is_rejected() -> None:
    with pytest.raises(JudgeResponseError, match="オブジェクトではありません"):
        parse_judge_payload("0.8", metric="groundedness")


def test_score_out_of_range_is_rejected() -> None:
    with pytest.raises(JudgeResponseError, match="範囲外"):
        parse_judge_payload({**VALID, "score": 85}, metric="groundedness")


def test_boolean_is_not_a_score() -> None:
    """bool は int のサブクラス。True を 1.0 として通さない。"""
    with pytest.raises(JudgeResponseError, match="数値ではありません"):
        parse_judge_payload({**VALID, "score": True}, metric="groundedness")


def test_missing_reason_is_rejected() -> None:
    """理由の無いスコアは改善に使えないので採点失敗として扱う(Step 2-2)。"""
    with pytest.raises(JudgeResponseError, match="reason が空"):
        parse_judge_payload({"score": 0.9, "violations": []}, metric="groundedness")


def test_blank_reason_is_rejected() -> None:
    with pytest.raises(JudgeResponseError, match="reason が空"):
        parse_judge_payload({**VALID, "reason": "   "}, metric="groundedness")


def test_violations_of_wrong_type_are_ignored_not_fatal() -> None:
    """violations の型崩れは致命ではない(score と reason があれば採点は成立する)。"""
    _, _, violations = parse_judge_payload({**VALID, "violations": "なし"}, metric="x")
    assert violations == []


# ---------------------------------------------------------------------------
# 加重平均: 欠損は分母から外す(0点にしない)
# ---------------------------------------------------------------------------


def test_weighted_average() -> None:
    scores = {"groundedness": 1.0, "relevance": 0.5}
    weights = {"groundedness": 0.5, "relevance": 0.5}
    assert weighted_average(scores, weights) == pytest.approx(0.75)


def test_missing_metric_is_excluded_from_denominator() -> None:
    """採点できなかった指標は分母からも外す。0点として平均を押し下げない。"""
    scores = {"groundedness": 1.0}  # relevance は採点失敗
    weights = {"groundedness": 0.5, "relevance": 0.5}
    assert weighted_average(scores, weights) == pytest.approx(1.0)


def test_no_scores_returns_none() -> None:
    """1つも採点できなければスコア欠損。0.0 とは区別する。"""
    assert weighted_average({}, {"groundedness": 1.0}) is None


def test_zero_weight_returns_none() -> None:
    assert weighted_average({"a": 1.0}, {"a": 0.0}) is None
