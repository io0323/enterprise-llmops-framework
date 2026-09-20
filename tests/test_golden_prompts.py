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


@pytest.mark.parametrize("case", _cases("dde"), ids=_ids("dde"))
def test_dde_prompt_renders_byte_identical(case: dict[str, Any], registry: PromptRegistry) -> None:
    """移行前の `build_*_prompt()` の出力と1バイトも違わないこと。"""
    golden = (GOLDEN_DIR / f"{case['name']}.txt").read_text(encoding="utf-8")
    resolved = registry.resolve(case["prompt_id"])
    rendered = registry.render(resolved, case["variables"]).text

    assert rendered == golden, (
        f"{case['prompt_id']} のレンダリング結果がゴールデンと一致しません。\n"
        "移行で文言が変わっています(移行の鉄則 4: 文言を同時に改善しない)。\n"
        f"--- golden ---\n{golden!r}\n--- rendered ---\n{rendered!r}"
    )


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
