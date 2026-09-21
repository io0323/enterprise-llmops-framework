"""Groundedness — 生成文が context に帰属しているかの検証(Step 2-3 / 19章 §6)。

2段構えにする:

1. **決定的な帰属チェック**(`deterministic.no_outside_context`)。
   数値・カタカナ語・英数字トークンが context に出てくるかを見る。LLMを呼ばない
2. **文単位の帰属判定**(本モジュール)。生成文を文に割り、各文を Judge に問う

1 で足りるケースが多いので、2 は「1 を通ったのに怪しい」ときのための精密検査という
位置付けにする。どちらも**再現率優先**(見逃しを減らす)で、誤検出は人が潰す前提。

**RAG 本体は再実装しない**(絶対ルール3)。ここは評価だけを行う。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from llmops.eval.judge import Judge, JudgeResponseError
from llmops.logging_utils import get_logger

logger = get_logger(__name__)

#: 文の区切り。日本語(。!?)と改行を見る
_SENTENCE_RE = re.compile(r"[^。!?\n]+[。!?]?")

#: 見出し・箇条書き記号・表の行。事実主張ではないので帰属判定から除く
_SKIP_RE = re.compile(r"^\s*(#{1,6}\s|[-*+]\s|\||\d+\.\s*$)")

#: これより短い文は判定しない(「そうだ。」のような断片で Judge を呼ぶ意味がない)
MIN_SENTENCE_CHARS = 12


@dataclass
class SentenceVerdict:
    sentence: str
    grounded: bool
    reason: str = ""
    span_id: str | None = None
    error: str | None = None


@dataclass
class GroundednessResult:
    total: int
    grounded: int
    verdicts: list[SentenceVerdict] = field(default_factory=list)
    errors: int = 0

    @property
    def rate(self) -> float | None:
        """帰属率(context 由来の文 / 判定できた文)。

        判定できた文が0件なら None(スコア欠損)。0.0 とは別物として扱う。
        """
        judged = self.total - self.errors
        return None if judged <= 0 else self.grounded / judged

    @property
    def unattributed(self) -> list[str]:
        return [v.sentence for v in self.verdicts if not v.grounded and v.error is None]


def split_sentences(text: str) -> list[str]:
    """帰属を問う対象の文を取り出す。見出し・箇条書き記号・短い断片は除く。"""
    sentences: list[str] = []
    for line in text.splitlines():
        if _SKIP_RE.match(line):
            continue
        for match in _SENTENCE_RE.finditer(line):
            sentence = match.group(0).strip()
            if len(sentence) >= MIN_SENTENCE_CHARS:
                sentences.append(sentence)
    return sentences


def evaluate(
    *,
    output: str,
    context: str,
    judge: Judge,
    trace_id: str,
    prompt_id: str = "judge.groundedness",
    threshold: float = 0.5,
) -> GroundednessResult:
    """文単位で帰属を問う。

    Judge が落ちた文は `error` として数え、**帰属率の分母から外す**。
    「判定できなかった」を「帰属している」にも「していない」にもしない。
    """
    sentences = split_sentences(output)
    result = GroundednessResult(total=len(sentences), grounded=0)
    if not sentences:
        return result

    for sentence in sentences:
        try:
            verdict = judge.score(
                metric="groundedness",
                prompt_id=prompt_id,
                variables={"context": context, "output": sentence},
                trace_id=trace_id,
            )
        except JudgeResponseError as exc:
            result.errors += 1
            result.verdicts.append(SentenceVerdict(sentence, False, error=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - Judge の失敗で run を止めない
            logger.warning("groundedness の採点に失敗しました: %s", exc)
            result.errors += 1
            result.verdicts.append(SentenceVerdict(sentence, False, error=str(exc)))
            continue

        grounded = verdict.score >= threshold
        result.grounded += 1 if grounded else 0
        result.verdicts.append(
            SentenceVerdict(sentence, grounded, verdict.reason, verdict.span_id)
        )
    return result


def context_from_case(variables: dict[str, Any], *, context_var: str = "context") -> str:
    """評価ケースの変数から context を取り出す。

    CGMP の `rag/retriever.py` が返したチャンクは、移行時に `context` 変数として
    渡されている(`docs/05` §2.2)。RAG 本体には触らない。
    """
    return str(variables.get(context_var, "") or "")
