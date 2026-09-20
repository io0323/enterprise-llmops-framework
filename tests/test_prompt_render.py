"""最小レンダラの検証(Step 1-3 / 設計 §3.2)。

**決定性**がこのモジュールの肝。同一入力で同一 render_hash が出ないと、
PEP の renderHash と突き合わせられず、回帰テストも成立しない。
"""

from __future__ import annotations

import pytest

from llmops.errors import MissingVariable, PromptError, VariableSchemaError
from llmops.prompt.render import (
    HASH_LENGTH,
    content_hash,
    expand_includes,
    render,
    render_hash,
    validate_variables,
    variable_names,
)

# ---------------------------------------------------------------------------
# 変数展開
# ---------------------------------------------------------------------------


def test_simple_substitution() -> None:
    assert render("こんにちは {{ name }}", {"name": "io"}).text == "こんにちは io"


def test_whitespace_variants() -> None:
    for template in ("{{name}}", "{{ name }}", "{{  name  }}"):
        assert render(template, {"name": "x"}).text == "x"


def test_repeated_variable() -> None:
    assert render("{{ a }}-{{ a }}", {"a": "1"}).text == "1-1"


def test_missing_variable_raises() -> None:
    """空文字で黙って通さない(設計 §3.2)。"""
    with pytest.raises(MissingVariable, match="name"):
        render("{{ name }}", {})


def test_extra_variables_are_ignored() -> None:
    assert render("{{ a }}", {"a": "1", "unused": "2"}).text == "1"


def test_no_control_flow_syntax() -> None:
    """if/for は構文として持たない。書いてもただの文字列として残る。"""
    template = "{% if x %}yes{% endif %}"
    assert render(template, {}).text == template


def test_numbers_and_booleans() -> None:
    assert render("{{ n }} {{ b }}", {"n": 1200, "b": True}).text == "1200 true"


def test_list_without_filter_is_comma_joined() -> None:
    assert render("{{ xs }}", {"xs": ["a", "b"]}).text == "a, b"


# ---------------------------------------------------------------------------
# フィルタ
# ---------------------------------------------------------------------------


def test_join_filter_with_separator() -> None:
    assert render('{{ xs | join(", ") }}', {"xs": ["a", "b"]}).text == "a, b"


def test_join_filter_default_separator() -> None:
    assert render("{{ xs | join }}", {"xs": ["a", "b"]}).text == "a, b"


def test_join_filter_custom_separator() -> None:
    assert render('{{ xs | join(" / ") }}', {"xs": ["a", "b"]}).text == "a / b"


def test_join_filter_rejects_non_list() -> None:
    with pytest.raises(PromptError, match="join"):
        render("{{ x | join }}", {"x": "not a list"})


def test_default_filter_fills_missing() -> None:
    assert render('{{ x | default("なし") }}', {}).text == "なし"


def test_default_filter_fills_empty() -> None:
    assert render('{{ x | default("なし") }}', {"x": ""}).text == "なし"


def test_default_filter_keeps_value() -> None:
    assert render('{{ x | default("なし") }}', {"x": "あり"}).text == "あり"


def test_upper_and_trim_filters() -> None:
    assert render("{{ a | upper }}|{{ b | trim }}", {"a": "ab", "b": "  c  "}).text == "AB|c"


def test_unknown_filter_raises() -> None:
    with pytest.raises(PromptError, match="未対応のフィルタ"):
        render("{{ x | reverse }}", {"x": "ab"})


def test_variable_names_excludes_includes() -> None:
    assert variable_names("{{ a }} {{ include.f }} {{ b | upper }}") == ["a", "b"]


# ---------------------------------------------------------------------------
# fragment(設計 §3.1)
# ---------------------------------------------------------------------------


def test_include_is_expanded() -> None:
    assert expand_includes("A\n{{ include.rule }}\nB", {"rule": "ルール本文"}) == "A\nルール本文\nB"


def test_unknown_include_raises() -> None:
    with pytest.raises(PromptError, match="宣言されていません"):
        expand_includes("{{ include.missing }}", {})


def test_fragment_cannot_nest() -> None:
    """ネスト禁止(1段のみ)。fragment 本文に include があればエラー。"""
    with pytest.raises(PromptError, match="1段のみ"):
        expand_includes("{{ include.a }}", {"a": "x {{ include.b }}"})


def test_fragment_cannot_take_variables() -> None:
    """変数渡し禁止。パラメータ依存のテキストは呼び出し側で組み立てる(N-008)。"""
    with pytest.raises(PromptError, match="変数・include を書けません"):
        expand_includes("{{ include.a }}", {"a": "こんにちは {{ name }}"})


def test_fragment_with_filter_is_rejected() -> None:
    with pytest.raises(PromptError, match="フィルタは使えません"):
        expand_includes("{{ include.a | upper }}", {"a": "x"})


def test_render_rejects_include_without_fragments() -> None:
    with pytest.raises(PromptError, match="宣言されていません"):
        render("{{ include.a }}", {}, fragments=None)


def test_substitute_guards_against_unexpanded_include() -> None:
    """展開を飛ばした場合の保険(通常は expand_includes が先に落ちる)。"""
    from llmops.prompt.render import _substitute

    with pytest.raises(PromptError, match="未展開"):
        _substitute("{{ include.a }}", {})


def test_render_expands_then_substitutes() -> None:
    result = render("{{ include.rule }} / {{ name }}", {"name": "io"}, fragments={"rule": "R"})
    assert result.text == "R / io"


# ---------------------------------------------------------------------------
# ハッシュ(FR-023)
# ---------------------------------------------------------------------------


def test_render_hash_is_deterministic() -> None:
    first = render("{{ a }}-{{ b }}", {"a": "1", "b": "2"})
    second = render("{{ a }}-{{ b }}", {"a": "1", "b": "2"})
    assert first.hash == second.hash
    assert len(first.hash) == HASH_LENGTH


def test_render_hash_changes_with_input() -> None:
    assert render("{{ a }}", {"a": "1"}).hash != render("{{ a }}", {"a": "2"}).hash


def test_render_hash_matches_direct_call() -> None:
    result = render("{{ a }}", {"a": "1"})
    assert result.hash == render_hash(result.text)


def test_render_hash_is_stable_across_runs() -> None:
    """値を固定しておき、実装変更で hash 定義が変わったら気付けるようにする。"""
    assert render_hash("hello") == "2cf24dba5fb0a30e"


def test_content_hash_covers_fragment_expansion() -> None:
    """fragment を変えると参照元の content_hash も変わる(設計 §3.1)。"""
    front_matter = "id: x.y"
    before = content_hash(expand_includes("{{ include.r }}", {"r": "旧"}), front_matter)
    after = content_hash(expand_includes("{{ include.r }}", {"r": "新"}), front_matter)
    assert before != after


def test_content_hash_covers_front_matter() -> None:
    assert content_hash("body", "model: a") != content_hash("body", "model: b")


# ---------------------------------------------------------------------------
# 変数スキーマ(FR-022)
# ---------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "required": ["title"],
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "max_chars": {"type": "integer", "default": 1200},
    },
}


def test_schema_default_is_applied() -> None:
    assert render("{{ max_chars }}", {"title": "t"}, var_schema=SCHEMA).text == "1200"


def test_schema_default_can_be_overridden() -> None:
    result = render("{{ max_chars }}", {"title": "t", "max_chars": 800}, var_schema=SCHEMA)
    assert result.text == "800"


def test_schema_required_is_enforced() -> None:
    with pytest.raises((MissingVariable, VariableSchemaError)):
        render("{{ title }}", {}, var_schema=SCHEMA)


def test_schema_type_is_enforced() -> None:
    with pytest.raises(VariableSchemaError):
        validate_variables({"title": 1}, SCHEMA)


def test_basic_validation_without_jsonschema(monkeypatch: pytest.MonkeyPatch) -> None:
    """`jsonschema` が無い環境では required と基本型のみの簡易検証になる(NFR-05)。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "jsonschema":
            raise ImportError("no jsonschema")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert validate_variables({"title": "t"}, SCHEMA)["max_chars"] == 1200
    with pytest.raises(MissingVariable):
        validate_variables({}, SCHEMA)
    with pytest.raises(VariableSchemaError):
        validate_variables({"title": 1}, SCHEMA)
    with pytest.raises(VariableSchemaError):
        validate_variables({"title": "t", "max_chars": True}, SCHEMA)
