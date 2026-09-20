"""コードフェンス除去と JSON 抽出。

`content-generation-platform/src/cgmp/llm/client.py` からの**そのままの移植**
(CLAUDE.md 絶対ルール10)。実績のある挙動が正であり、差分は回帰の原因になる。
改善は評価スイート(Phase 2)が用意できてからにする。

移植にあたっての変更は1点のみで、挙動は変わらない:
例外を `cgmp.llm.client.LLMError` から ELF の例外階層へ合わせ、`ResponseParseError` を
送出する。`ResponseParseError` は `LLMError` のサブクラスなので、既存の
`except LLMError` はそのまま成立する。正規表現・分岐・メッセージ文言は一字も変えていない。

DDE 版(`dde/llm/client.py`)との差分については NOTES.md N-014 を参照。
"""

from __future__ import annotations

import json
import re
from typing import Any

from llmops.errors import ResponseParseError

_FENCE_RE = re.compile(r"^\s*```(?:json|markdown|md)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def strip_fence(text: str) -> str:
    """コードフェンス(```md ... ```)があれば剥がす。"""
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    return fenced.group(1).strip() if fenced else stripped


def extract_json(text: str | None) -> Any:
    """本文テキストから JSON を取り出す(コードフェンス・前後の説明文に対応)。"""
    if text is None:
        raise ResponseParseError("LLM応答が空です")
    stripped = strip_fence(text)
    if not stripped:
        raise ResponseParseError("LLM応答が空です")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 前後に説明文が付いた場合に備え、最外の {} / [] を切り出して再試行する
    for opener, closer in (("{", "}"), ("[", "]")):
        start = stripped.find(opener)
        end = stripped.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ResponseParseError(f"JSONのパースに失敗しました: {stripped[:200]}")
