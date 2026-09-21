"""決定的評価(純コード。**LLMを一切呼ばない**)。

これが落ちたケースは Judge へ進めない(`eval.deterministic_first`)。
CGMP 絶対ルール6(決定的判定を先に)と同方針で、無駄なコストを払わないため。

本モジュールの肝は `no_outside_context`。CGMP 絶対ルール9 / 19章 §6 Grounding の
機械検証で、**context に無い数値・固有名詞を書いていないか**を見る。
完全な判定は不可能なので**再現率優先**(見逃しを減らす)にし、誤検出は
`allowlist` で人が潰す前提にする。誤検出が多すぎて使われなくなるのが最悪であるため。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llmops.eval.suite import DeterministicRule
from llmops.logging_utils import get_logger

logger = get_logger(__name__)

#: 数値・年号(全角半角の数字を含む)
_NUMBER_RE = re.compile(r"[0-9０-９]+(?:[.,][0-9０-９]+)*")
#: カタカナ語(3文字以上。短いものは一般語が多く誤検出になりやすい)
_KATAKANA_RE = re.compile(r"[ァ-ヴー]{3,}")
#: 英数字トークン(3文字以上)
_ALNUM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{2,}")

#: 常に許す語(単位・助数詞など。context に無くても違反ではない)
DEFAULT_ALLOWLIST = frozenset(
    {
        "http",
        "https",
        "www",
        "com",
        "jp",
        "Markdown",
        "markdown",
        "JSON",
        "json",
    }
)


@dataclass(frozen=True)
class RuleOutcome:
    """決定的評価1件の結果。"""

    rule: str
    passed: bool
    detail: str = ""
    violations: list[str] | None = None

    def as_detail(self) -> str:
        if not self.violations:
            return self.detail
        listed = "、".join(self.violations[:20])
        more = "" if len(self.violations) <= 20 else f" ほか{len(self.violations) - 20}件"
        return f"{self.detail}: {listed}{more}" if self.detail else f"{listed}{more}"


def extract_tokens(text: str) -> list[str]:
    """帰属を確認したいトークン(数値・カタカナ語・英数字)を抜き出す。"""
    tokens: list[str] = []
    for pattern in (_NUMBER_RE, _KATAKANA_RE, _ALNUM_RE):
        tokens.extend(match.group(0) for match in pattern.finditer(text))
    # 出現順を保ったまま重複を除く(レポートの読みやすさのため)
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def unattributed_tokens(
    output: str, context: str, *, allowlist: Sequence[str] = ()
) -> list[str]:
    """context に出てこないトークンを列挙する(違反候補)。"""
    allowed = DEFAULT_ALLOWLIST | {str(a) for a in allowlist}
    return [
        token
        for token in extract_tokens(output)
        if token not in allowed and token not in context
    ]


# ---------------------------------------------------------------------------
# ルール実装
# ---------------------------------------------------------------------------


def _non_empty(output: str, _rule: DeterministicRule, _vars: Mapping[str, Any]) -> RuleOutcome:
    filled = bool(output.strip())
    return RuleOutcome("non_empty", filled, "" if filled else "出力が空です")


def _max_chars(output: str, rule: DeterministicRule, _vars: Mapping[str, Any]) -> RuleOutcome:
    limit = int(rule.value)
    length = len(output)
    return RuleOutcome(
        "max_chars", length <= limit, "" if length <= limit else f"{length}字(上限{limit}字)"
    )


def _min_chars(output: str, rule: DeterministicRule, _vars: Mapping[str, Any]) -> RuleOutcome:
    limit = int(rule.value)
    length = len(output)
    return RuleOutcome(
        "min_chars", length >= limit, "" if length >= limit else f"{length}字(下限{limit}字)"
    )


def _forbidden_patterns(
    output: str, rule: DeterministicRule, _vars: Mapping[str, Any]
) -> RuleOutcome:
    hits = [pattern for pattern in rule.patterns if pattern in output]
    return RuleOutcome("forbidden_patterns", not hits, "禁止表現", hits or None)


def _required_patterns(
    output: str, rule: DeterministicRule, _vars: Mapping[str, Any]
) -> RuleOutcome:
    missing = [pattern for pattern in rule.patterns if pattern not in output]
    return RuleOutcome("required_patterns", not missing, "必須表現の欠落", missing or None)


def _no_outside_context(
    output: str, rule: DeterministicRule, variables: Mapping[str, Any]
) -> RuleOutcome:
    context = str(variables.get(rule.context_var, "") or "")
    if not context.strip():
        # context が無いケースでこのルールを当てると全トークンが違反になる。
        # 「context 無しで書かせる」Prompt は別スイートで見るべきなので、ここは通す
        logger.warning(
            "no_outside_context: 変数 %s が空のため判定をスキップしました", rule.context_var
        )
        return RuleOutcome("no_outside_context", True, f"{rule.context_var} が空のためスキップ")
    violations = unattributed_tokens(output, context, allowlist=rule.allowlist)
    return RuleOutcome(
        "no_outside_context", not violations, "contextに無い語", violations or None
    )


def _json_schema(
    output: str, rule: DeterministicRule, _vars: Mapping[str, Any], *, base_dir: Path | None = None
) -> RuleOutcome:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        return RuleOutcome("json_schema", False, f"JSONとして読めません: {exc}")

    if rule.schema_file is None:
        return RuleOutcome("json_schema", True, "schema_file 未指定(JSON妥当性のみ検証)")

    path = Path(rule.schema_file)
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    if not path.is_file():
        return RuleOutcome("json_schema", False, f"スキーマファイルがありません: {path}")

    schema = json.loads(path.read_text(encoding="utf-8"))
    try:
        import jsonschema  # noqa: PLC0415 - optional extra
    except ImportError:
        logger.warning("jsonschema が未インストールのため JSON 妥当性のみ検証します")
        return RuleOutcome("json_schema", True, "jsonschema 未インストール(妥当性のみ)")

    try:
        jsonschema.validate(payload, schema)
    except jsonschema.ValidationError as exc:
        return RuleOutcome("json_schema", False, f"スキーマ不適合: {exc.message}")
    return RuleOutcome("json_schema", True)


_RULES = {
    "non_empty": _non_empty,
    "max_chars": _max_chars,
    "min_chars": _min_chars,
    "forbidden_patterns": _forbidden_patterns,
    "required_patterns": _required_patterns,
    "no_outside_context": _no_outside_context,
}


def evaluate(
    output: str,
    rules: Sequence[DeterministicRule],
    variables: Mapping[str, Any],
    *,
    base_dir: Path | None = None,
) -> list[RuleOutcome]:
    """全ルールを順に適用する。**どのルールも LLM を呼ばない。**"""
    outcomes: list[RuleOutcome] = []
    for rule in rules:
        if rule.type == "json_schema":
            outcomes.append(_json_schema(output, rule, variables, base_dir=base_dir))
            continue
        outcomes.append(_RULES[rule.type](output, rule, variables))
    return outcomes


def all_passed(outcomes: Sequence[RuleOutcome]) -> bool:
    return all(outcome.passed for outcome in outcomes)
