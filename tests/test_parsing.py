"""`adapters/_parsing.py` の検証(Step 1-2)。

移植元(`cgmp/llm/client.py`)と**同じ挙動**であることを確かめるためのテスト。
改善はしない(絶対ルール10)。ここが変わると既存2システムの出力解釈が変わる。
"""

from __future__ import annotations

import pytest

from llmops.adapters._parsing import extract_json, strip_fence
from llmops.errors import LLMError, ResponseParseError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain text", "plain text"),
        ("  padded  ", "padded"),
        ("```\nbody\n```", "body"),
        ("```json\n{\"a\": 1}\n```", '{"a": 1}'),
        ("```markdown\n# 見出し\n```", "# 見出し"),
        ("```md\n# 見出し\n```", "# 見出し"),
        ("```JSON\n1\n```", "1"),
    ],
)
def test_strip_fence(raw: str, expected: str) -> None:
    assert strip_fence(raw) == expected


def test_strip_fence_keeps_inner_fences() -> None:
    """途中にフェンスが出る本文は、最外だけを剥がす(DOTALL の貪欲一致)。"""
    assert strip_fence("```md\n本文\n```py\nx=1\n```\n```") == "本文\n```py\nx=1\n```"


def test_extract_json_object() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_array() -> None:
    assert extract_json("[1, 2]") == [1, 2]


def test_extract_json_from_fence() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_surrounding_prose() -> None:
    assert extract_json('以下が結果です。\n{"a": 1}\nご確認ください。') == {"a": 1}


def test_extract_json_array_with_surrounding_prose() -> None:
    assert extract_json("結果:\n[1, 2]\n以上") == [1, 2]


def test_extract_json_none_is_error() -> None:
    with pytest.raises(ResponseParseError, match="空"):
        extract_json(None)


def test_extract_json_empty_is_error() -> None:
    with pytest.raises(ResponseParseError, match="空"):
        extract_json("   ")


def test_extract_json_garbage_is_error() -> None:
    with pytest.raises(ResponseParseError, match="パースに失敗"):
        extract_json("これはJSONではありません")


def test_parse_error_is_llm_error() -> None:
    """既存コードの `except LLMError` が効くこと。"""
    assert issubclass(ResponseParseError, LLMError)
