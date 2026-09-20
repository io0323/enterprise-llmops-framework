"""最小テンプレートレンダラ(設計 §3.2 / NOTES.md N-003)。

**Jinja2 を使わない。** 構文は次の3つだけで、制御構文(if/for)を持たない:

- ``{{ var }}``
- ``{{ var | filter }}`` / ``{{ var | filter(arg) }}``(filter は join / default / upper / trim)
- ``{{ include.<name> }}``(fragment の展開。ネスト禁止・変数渡し禁止)

分岐が必要なら Prompt を分ける。ロジックが Prompt に入ると、変更の影響範囲が
テストで追えなくなるため。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from llmops.errors import MissingVariable, PromptError, VariableSchemaError

#: `{{ name }}` / `{{ name | filter }}` / `{{ name | filter(arg) }}` / `{{ include.name }}`
_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*"
    r"(?:\|\s*(?P<filter>[A-Za-z_]+)\s*(?:\((?P<arg>[^)]*)\))?\s*)?\}\}"
)

INCLUDE_PREFIX = "include."

#: 実装するフィルタ。これ以外は `PromptError`(構文を意図的に絞る)
FILTERS = ("join", "default", "upper", "trim")

#: render_hash の桁数。PEP の renderHash と同じ定義(SHA-256 の先頭16桁)
HASH_LENGTH = 16


def render_hash(text: str) -> str:
    """レンダリング後テキストの SHA-256 先頭16桁。同一入力で必ず同一値になる。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def content_hash(expanded_body: str, front_matter_text: str) -> str:
    """fragment 展開後の body + front matter のハッシュ(設計 §2.2)。

    展開後を使うので、fragment を変更すると参照元 Prompt にも新 version が採番される。
    """
    joined = f"{front_matter_text}\n---\n{expanded_body}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _parse_arg(raw: str | None) -> str | None:
    """フィルタ引数のクォートを外す。`join(", ")` → `, `。"""
    if raw is None:
        return None
    arg = raw.strip()
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in {'"', "'"}:
        return arg[1:-1]
    return arg


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return ", ".join(_stringify(item) for item in value)
    return str(value)


def _apply_filter(name: str, value: Any, arg: str | None) -> str:
    if name == "join":
        if not isinstance(value, list | tuple):
            raise PromptError(f"join フィルタはリストにのみ使える: {value!r}")
        return (arg if arg is not None else ", ").join(_stringify(item) for item in value)
    if name == "default":
        return _stringify(value if value not in (None, "") else (arg or ""))
    if name == "upper":
        return _stringify(value).upper()
    if name == "trim":
        return _stringify(value).strip()
    raise PromptError(f"未対応のフィルタです: {name}(使えるのは {', '.join(FILTERS)})")


def expand_includes(body: str, fragments: Mapping[str, str]) -> str:
    """`{{ include.<name> }}` を fragment 本文で置換する。

    fragment は**ネストしない**(展開結果に `{{ include.x }}` が残っていたらエラー)。
    fragment に変数を渡すこともできない(fragment 本文に `{{ }}` があればエラー)。
    """
    used: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if not name.startswith(INCLUDE_PREFIX):
            return match.group(0)
        if match.group("filter"):
            raise PromptError(f"fragment にフィルタは使えません: {match.group(0)}")
        key = name[len(INCLUDE_PREFIX) :]
        if key not in fragments:
            raise PromptError(
                f"fragment が宣言されていません: {key}"
                " (front matter の includes: に書く。宣言漏れはエラーにする)"
            )
        used.add(key)
        text = fragments[key]
        if _PLACEHOLDER_RE.search(text):
            raise PromptError(
                f"fragment に変数・include を書けません(1段のみ・変数渡し禁止): {key}"
            )
        return text

    return _PLACEHOLDER_RE.sub(replace, body)


@dataclass(frozen=True)
class RenderResult:
    text: str
    hash: str


def render(
    body: str,
    variables: Mapping[str, Any],
    *,
    fragments: Mapping[str, str] | None = None,
    var_schema: Mapping[str, Any] | None = None,
) -> RenderResult:
    """変数検証 → fragment 展開 → 変数展開 → ハッシュ算出。

    Raises:
        MissingVariable: テンプレートが要求する変数が無い(空文字で黙って通さない)。
        VariableSchemaError: `variables` スキーマに適合しない。
    """
    values = dict(variables)
    if var_schema is not None:
        values = validate_variables(values, var_schema)

    expanded = expand_includes(body, fragments or {})
    text = _substitute(expanded, values)
    return RenderResult(text=text, hash=render_hash(text))


def _substitute(body: str, values: Mapping[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name.startswith(INCLUDE_PREFIX):
            raise PromptError(f"未展開の fragment が残っています: {name}")
        filter_name = match.group("filter")
        arg = _parse_arg(match.group("arg"))

        if name not in values:
            # default フィルタだけは、変数が無いこと自体を許す
            if filter_name == "default":
                return _apply_filter("default", None, arg)
            raise MissingVariable(f"変数が渡されていません: {name}")

        value = values[name]
        if filter_name is None:
            return _stringify(value)
        if filter_name not in FILTERS:
            raise PromptError(f"未対応のフィルタです: {filter_name}")
        return _apply_filter(filter_name, value, arg)

    return _PLACEHOLDER_RE.sub(replace, body)


def variable_names(body: str) -> list[str]:
    """テンプレートが参照する変数名(include を除く)。"""
    names = [
        m.group("name")
        for m in _PLACEHOLDER_RE.finditer(body)
        if not m.group("name").startswith(INCLUDE_PREFIX)
    ]
    return sorted(set(names))


# ---------------------------------------------------------------------------
# 変数スキーマ検証(FR-022)
# ---------------------------------------------------------------------------

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": (list, tuple),
    "object": dict,
}


def apply_defaults(values: dict[str, Any], var_schema: Mapping[str, Any]) -> dict[str, Any]:
    """スキーマの `default` を、渡されなかった変数に適用する。"""
    properties = var_schema.get("properties") or {}
    if not isinstance(properties, dict):
        return values
    filled = dict(values)
    for name, spec in properties.items():
        if isinstance(spec, dict) and name not in filled and "default" in spec:
            filled[str(name)] = spec["default"]
    return filled


def validate_variables(
    values: dict[str, Any], var_schema: Mapping[str, Any]
) -> dict[str, Any]:
    """`variables`(JSON Schema)で検証する。

    `jsonschema` が入っていれば本格的に検証し、無ければ required と基本型のみの
    簡易検証にフォールバックする(必須依存を増やさないため。NFR-05)。
    """
    filled = apply_defaults(values, var_schema)
    try:
        import jsonschema  # noqa: PLC0415 - optional extra
    except ImportError:
        _validate_basic(filled, var_schema)
        return filled

    try:
        jsonschema.validate(filled, dict(var_schema))
    except jsonschema.ValidationError as exc:
        raise VariableSchemaError(f"変数がスキーマに適合しません: {exc.message}") from exc
    return filled


def _validate_basic(values: Mapping[str, Any], var_schema: Mapping[str, Any]) -> None:
    """required と基本型だけの簡易検証(jsonschema 未インストール時)。"""
    required = var_schema.get("required") or []
    if isinstance(required, list):
        missing = [str(name) for name in required if str(name) not in values]
        if missing:
            raise MissingVariable(f"必須の変数がありません: {', '.join(missing)}")

    properties = var_schema.get("properties") or {}
    if not isinstance(properties, dict):
        return
    for name, spec in properties.items():
        if not isinstance(spec, dict) or str(name) not in values:
            continue
        expected = _JSON_TYPES.get(str(spec.get("type", "")))
        if expected is None:
            continue
        value = values[str(name)]
        # bool は int のサブクラスなので、integer/number 判定から除く
        is_bool_mismatch = isinstance(value, bool) and spec.get("type") in {"integer", "number"}
        if is_bool_mismatch or not isinstance(value, expected):
            raise VariableSchemaError(
                f"変数 {name} の型が {spec.get('type')} ではありません: {type(value).__name__}"
            )
