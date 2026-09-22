"""移行前後のバイト一致検証(移行の鉄則 2 / `docs/05_既存システム統合.md` §2.2)。

`tests/fixtures/golden/*.txt` は**移行前**の現行関数の出力そのもの。
`llmops prompt render` の結果がこれと**バイト一致**しない限り、呼び出し側を
切り替えてはいけない。文言を「ついでに改善」していないことの機械的な保証でもある。

ゴールデンの採取手順は `docs/05_既存システム統合.md` §2.2 と NOTES.md N-024 を参照。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from llmops.config import project_root
from llmops.db.connection import MEMORY
from llmops.db.repository import Repository
from llmops.prompt.registry import PUBLISHED, PromptRegistry

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"


def _cases(system: str) -> list[dict[str, Any]]:
    manifest = GOLDEN_DIR / f"{system}_manifest.json"
    if not manifest.is_file():
        return []
    loaded: list[dict[str, Any]] = json.loads(manifest.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="module")
def registry() -> PromptRegistry:
    """実物の `prompts/` を読み込んだ Registry(インメモリDB)。"""
    repo = Repository.open(MEMORY)
    registry = PromptRegistry(repo, project_root() / "prompts")
    registry.sync()
    return registry


def _ids(system: str) -> list[str]:
    return [case["name"] for case in _cases(system)]


def _assert_byte_identical(case: dict[str, Any], registry: PromptRegistry) -> None:
    golden = (GOLDEN_DIR / f"{case['name']}.txt").read_text(encoding="utf-8")
    resolved = registry.resolve(case["prompt_id"])
    rendered = registry.render(resolved, case["variables"]).text

    assert rendered == golden, (
        f"{case['prompt_id']} のレンダリング結果がゴールデンと一致しません。\n"
        "移行で文言が変わっています(移行の鉄則 4: 文言を同時に改善しない)。\n"
        f"--- golden ---\n{golden!r}\n--- rendered ---\n{rendered!r}"
    )


@pytest.mark.parametrize("case", _cases("dde"), ids=_ids("dde"))
def test_dde_prompt_renders_byte_identical(case: dict[str, Any], registry: PromptRegistry) -> None:
    """移行前の `build_*_prompt()` の出力と1バイトも違わないこと。"""
    _assert_byte_identical(case, registry)


@pytest.mark.parametrize("case", _cases("cgmp"), ids=_ids("cgmp"))
def test_cgmp_prompt_renders_byte_identical(
    case: dict[str, Any], registry: PromptRegistry
) -> None:
    """移行前の `outline_prompt` / `section_prompt` / `closing_prompt` /
    `rubric_prompt` / `sns_prompt` の出力と1バイトも違わないこと。"""
    _assert_byte_identical(case, registry)


def test_cgmp_cases_cover_every_migrated_prompt() -> None:
    """`docs/05_既存システム統合.md` §2.2 の移行対象5件 + context なし分岐。"""
    assert {case["prompt_id"] for case in _cases("cgmp")} == {
        "cgmp.outline",
        "cgmp.section",
        "cgmp.section_no_context",
        "cgmp.closing",
        "cgmp.rubric",
        "cgmp.sns_summary",
    }


def test_cgmp_section_variants_differ_only_by_context_rule(registry: PromptRegistry) -> None:
    """context の有無で Prompt を分けた(設計 §3.2: 分岐が要るなら Prompt を分ける)。

    2ファイルが「context_rule の有無」以外で食い違っていないことを見る。
    片方だけ直す事故を防ぐため。
    """
    with_context = registry.resolve("cgmp.section").body
    without = registry.resolve("cgmp.section_no_context").body
    fragment = (registry.prompts_dir / "_fragments" / "context_rule.md").read_text(
        encoding="utf-8"
    )
    rule = fragment.split("---\n", 2)[-1].rstrip("\n")

    assert rule in with_context
    assert rule not in without
    # context なし版は、その位置に空行だけが残る(移行前の
    # `{CONTEXT_RULE if contexts else ""}` と同じ見え方)。行ごと消さずに本文だけ消す
    assert with_context.replace(rule, "") == without


@pytest.mark.parametrize("case", _cases("harness"), ids=_ids("harness"))
def test_harness_prompt_renders_byte_identical(
    case: dict[str, Any], registry: PromptRegistry
) -> None:
    """移行前の `render_prompt` / `render_eval_prompt` / `render_learning_prompt`
    (Harness `app/agents/*.py`)の出力と1バイトも違わないこと。

    ゴールデンは Harness の .venv で現行関数を直接呼んで採取した(NOTES.md N-044)。
    """
    _assert_byte_identical(case, registry)


def test_harness_cases_cover_every_migrated_prompt() -> None:
    """`docs/05_既存システム統合.md` §4: gen-v1 / eval-v1 / learning-v1 の3件 + 分岐。"""
    cases = _cases("harness")
    assert {case["prompt_id"] for case in cases} == {
        "harness.generator",
        "harness.evaluator",
        "harness.learning",
    }
    # 改善指示の有無・過去投稿の有無の両方を押さえる
    assert {case["name"] for case in cases} >= {
        "harness_generator_with_hint",
        "harness_evaluator_no_recent",
    }


def test_harness_prompts_are_published_and_use_harness_models(registry: PromptRegistry) -> None:
    """本番で動いていた Prompt なので初回登録で published(N-018)。

    論理モデルは Agent ごとに分ける。Harness は Agent ごとに実モデルと
    max_tokens が違い(settings.model_* / learning だけ 2048)、それを踏襲するため。
    """
    for agent in ("generator", "evaluator", "learning"):
        resolved = registry.resolve(f"harness.{agent}")
        assert resolved.status == PUBLISHED
        assert resolved.default_model == f"harness-{agent}"


def test_dde_cases_exist() -> None:
    """ゴールデンが消えたまま「通った」ことにならないようにする。"""
    assert {case["prompt_id"] for case in _cases("dde")} == {
        "dde.expand",
        "dde.classify",
        "dde.cluster_naming",
    }


def test_dde_prompts_are_published(registry: PromptRegistry) -> None:
    """移行対象は本番で動いていた Prompt なので、初回登録時点で published(N-018)。"""
    for prompt_id in ("dde.expand", "dde.classify", "dde.cluster_naming"):
        assert registry.resolve(prompt_id).status == PUBLISHED


def test_dde_prompts_use_the_batch_model(registry: PromptRegistry) -> None:
    """DDE の既存設定は timeout_sec: 300。それを踏襲した論理モデルを使う(Step 1-7)。"""
    for prompt_id in ("dde.expand", "dde.classify", "dde.cluster_naming"):
        assert registry.resolve(prompt_id).default_model == "dde-batch"


def test_render_is_deterministic_across_calls(registry: PromptRegistry) -> None:
    case = _cases("dde")[0]
    resolved = registry.resolve(case["prompt_id"])
    first = registry.render(resolved, case["variables"])
    second = registry.render(resolved, case["variables"])
    assert first.hash == second.hash


# ---------------------------------------------------------------------------
# Prompt 内容の回帰(DDE の tests/test_prompts.py から移設)
#
# 「なぜこの文言が必要か」を残すためのテスト。ゴールデンのバイト一致だけだと、
# 差分が出たときに「直すべきか戻すべきか」が判らなくなる。
# ---------------------------------------------------------------------------


def _render(registry: PromptRegistry, prompt_id: str, variables: dict[str, Any]) -> str:
    return registry.render(registry.resolve(prompt_id), variables).text


def _classify(registry: PromptRegistry, keywords: list[str]) -> str:
    listed = "\n".join(f"- {kw}" for kw in keywords)
    return _render(registry, "dde.classify", {"n": len(keywords), "keywords": listed})


def test_classify_prompt_lists_all_keywords(registry: PromptRegistry) -> None:
    prompt = _classify(registry, ["claude code", "claude code 副業"])
    assert "(2件)" in prompt
    assert "- claude code" in prompt
    assert "- claude code 副業" in prompt


def test_classify_prompt_requires_json_only(registry: PromptRegistry) -> None:
    prompt = _classify(registry, ["a"])
    assert "JSON配列**のみ**" in prompt
    assert "説明文・コードフェンス・前置きは一切禁止" in prompt


def test_classify_prompt_states_monetization_model(registry: PromptRegistry) -> None:
    """収益化モデル(個人ブログ・note・アフィリエイトの自然流入)を明示する。"""
    assert "個人ブログ・note・アフィリエイトへの自然流入による収益化" in _classify(registry, ["a"])


def test_purchase_intent_criteria_targets_individual_readers(registry: PromptRegistry) -> None:
    prompt = _classify(registry, ["a"])
    assert "個人読者が記事経由で商品購入・有料note購入・アフィリエイト経由申込に" in prompt


def test_purchase_intent_criteria_excludes_b2b(registry: PromptRegistry) -> None:
    """法人予算執行・B2B導入検討を高評価しない旨を明記する。"""
    prompt = _classify(registry, ["a"])
    assert "法人の予算執行やB2B導入検討" in prompt
    assert "このモデルでは収益化できないため" in prompt
    assert "高評価しない" in prompt


def test_classify_prompt_keeps_schema_fields(registry: PromptRegistry) -> None:
    prompt = _classify(registry, ["a"])
    for field in (
        "keyword",
        "intents",
        "seo_intent",
        "purchase_intent",
        "problem_depth",
        "note_potential",
    ):
        assert field in prompt
    assert "情報収集|比較|購入|問題解決|学習|体験共有" in prompt


def test_expand_prompt_includes_existing_keywords_and_cap(registry: PromptRegistry) -> None:
    prompt = _render(
        registry,
        "dde.expand",
        {
            "seed": "claude code",
            "max_keywords": 50,
            "existing_count": 1,
            "existing": "- claude code 副業",
        },
    )
    assert "「claude code」" in prompt
    assert "最大50件" in prompt
    assert "- claude code 副業" in prompt
    assert "既出キーワード(1件)" in prompt


def test_cluster_naming_prompt_requires_json_only(registry: PromptRegistry) -> None:
    prompt = _render(
        registry, "dde.cluster_naming", {"n": 2, "clusters": "- c001: 副業\n- c002: 税"}
    )
    assert "(2クラスタ)" in prompt
    assert "JSON配列**のみ**" in prompt
    assert "代表キーワードに無い商品名・固有名詞を創作しない" in prompt


# ---------------------------------------------------------------------------
# CGMP Prompt 内容の回帰(CGMP の tests/ から移設)
#
# 実機の仕上げで毎回出た問題に対応する制約が入っていること。
# ゴールデンのバイト一致だけだと、差分が出たときに「直すべきか戻すべきか」が
# 判らなくなるので、**なぜその文言が要るのか**をテスト名と docstring に残す。
# ---------------------------------------------------------------------------


def _cgmp_case(name: str) -> dict[str, Any]:
    return next(case for case in _cases("cgmp") if case["name"] == name)


@pytest.fixture(scope="module")
def section_prompt(registry: PromptRegistry) -> str:
    case = _cgmp_case("cgmp_section")
    return registry.render(registry.resolve(case["prompt_id"]), case["variables"]).text


@pytest.fixture(scope="module")
def closing_prompt(registry: PromptRegistry) -> str:
    case = _cgmp_case("cgmp_closing")
    return registry.render(registry.resolve(case["prompt_id"]), case["variables"]).text


def test_section_prompt_states_tone(section_prompt: str) -> None:
    """文体制約が入ること(セクション単位の並列生成で文体がぶれるのを防ぐ)。"""
    assert "文体はだ・である調で統一する(厳守)" in section_prompt
    assert "1文でもです・ます調を混ぜない" in section_prompt


def test_section_prompt_requires_concrete_numbers(section_prompt: str) -> None:
    """時事性のある事項の2段構え: ①具体値を必ず書く ②そのうえで時点を添える。

    「断定を避ける」だけを指示すると、制度の中核となる数値まで曖昧化して逃げる
    (実機で発生)。読者が最も必要とする情報が消えるため。
    """
    assert "数値・条件・期限は必ず具体的に書く" in section_prompt
    assert "曖昧な表現で数値を置き換えて逃げてはならない" in section_prompt
    assert "◯年時点" in section_prompt
    assert "5自治体以内(2026年時点)" in section_prompt
    assert "具体値を書かずに時点や確認の促しだけを書くのは誤り" in section_prompt
    assert "記事内で初出の1回のみとする" in section_prompt


def test_section_prompt_separates_promotion_from_requirements(section_prompt: str) -> None:
    """制度上の要件(具体値を書く)と事業者の販促施策(書かない)を区別する。"""
    assert "事業者の施策や法令改正で" in section_prompt
    assert "制度上の要件は前項のとおり具体値で書く" in section_prompt
    assert "事業者が自社の判断で変えられる数値か" in section_prompt


def test_section_prompt_has_editorial_constraints(section_prompt: str) -> None:
    assert "例外や前提条件があるなら必ず併記する" in section_prompt
    assert "同じ語尾を3文以上続けない" in section_prompt
    assert "表のセルは体言止めか記号で簡潔に書く" in section_prompt
    assert "記事全体で同じ主張を3回以上くり返さない" in section_prompt


def test_section_prompt_carries_term_rule(section_prompt: str) -> None:
    """表記ゆれの統一制約(N-74)。呼び出し側が組み立てて変数で渡す。"""
    assert "×「claude code」" in section_prompt


def test_section_with_context_uses_the_context_rule(section_prompt: str) -> None:
    """context があるときは「contextを正とする」を入れる(RAGを通す意味)。"""
    assert "contextの内容と自分の知識が食い違う場合は、**contextを正とする**" in section_prompt


def test_section_without_context_omits_the_context_rule(registry: PromptRegistry) -> None:
    case = _cgmp_case("cgmp_section_no_context")
    rendered = registry.render(registry.resolve(case["prompt_id"]), case["variables"]).text
    assert "contextを正とする" not in rendered
    assert "(参照可能な事実なし)" in rendered
    # context が無くても幻覚の禁止は効く
    assert "contextに無い固有名詞・数値・年号・統計・調査結果は書かない" in rendered


def test_closing_prompt_forbids_headings(closing_prompt: str) -> None:
    assert "見出し行(#・##・###)は出力に含めない" in closing_prompt


def test_closing_prompt_forbids_summary_faq_overlap(closing_prompt: str) -> None:
    """まとめとFAQで同じ論点を繰り返さない制約。"""
    assert "まとめとFAQで同じ論点を繰り返さない" in closing_prompt
    assert "本文で扱いきれなかった疑問" in closing_prompt


def test_closing_prompt_requires_platform_neutral_cta(closing_prompt: str) -> None:
    """CTA は媒体中立にする(同じ記事を複数媒体へ出すため)。"""
    assert "CTAは媒体中立な表現にする" in closing_prompt
    assert "スキ" in closing_prompt


def test_closing_prompt_requires_three_faq(closing_prompt: str) -> None:
    assert "FAQは必ず3件出す" in closing_prompt


def test_outline_prompt_caps_sections(registry: PromptRegistry) -> None:
    """H2の本数は呼び出し枠から決まる上限で縛る(枠を使い切らせない)。"""
    case = _cgmp_case("cgmp_outline")
    rendered = registry.render(registry.resolve(case["prompt_id"]), case["variables"]).text
    assert "H2見出しは5本以内(厳守)" in rendered
    assert "骨格1項目につきH2は1本まで" in rendered


def test_rubric_prompt_states_three_axes(registry: PromptRegistry) -> None:
    case = _cgmp_case("cgmp_rubric")
    rendered = registry.render(registry.resolve(case["prompt_id"]), case["variables"]).text
    for axis in ("outline_alignment", "claim_consistency", "audience_fit"):
        assert axis in rendered
    assert "文体の好み・表記ゆれは採点対象にしない" in rendered


def test_sns_prompt_forbids_url_and_hashtags_in_body(registry: PromptRegistry) -> None:
    case = _cgmp_case("cgmp_sns_summary")
    rendered = registry.render(registry.resolve(case["prompt_id"]), case["variables"]).text
    assert "投稿文にURLとハッシュタグを含めない" in rendered
    assert "記事に書かれていない数値・効果・実績を書かない" in rendered
