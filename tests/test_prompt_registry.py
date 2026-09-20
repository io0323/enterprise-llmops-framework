"""Prompt loader / registry / 状態機械の検証(Step 1-3)。"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from llmops.db.connection import MEMORY
from llmops.db.repository import Repository
from llmops.errors import InvalidTransition, PromptError, PromptNotFound
from llmops.prompt import catalog as prompt_catalog
from llmops.prompt import loader
from llmops.prompt.registry import (
    APPROVED,
    ARCHIVED,
    DEPRECATED,
    DRAFT,
    PUBLISHED,
    REVIEW,
    PromptRegistry,
)

SECTION_MD = """---
id: cgmp.section
status: published
owner: io
description: セクション生成
tags: [cgmp, writer]
model: chat-standard
includes: [rule]
variables:
  type: object
  required: [title]
  properties:
    title: {type: string}
    max_chars: {type: integer, default: 1200}
---
# {{ title }}

{{ include.rule }}
- {{ max_chars }}字以内
"""

FRAGMENT_MD = """---
id: _fragments.rule
description: 共有ルール
---
- contextに無いことは書かない
"""


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    root = tmp_path / "prompts"
    (root / "cgmp").mkdir(parents=True)
    (root / "_fragments").mkdir(parents=True)
    (root / "cgmp" / "section.md").write_text(SECTION_MD, encoding="utf-8")
    (root / "_fragments" / "rule.md").write_text(FRAGMENT_MD, encoding="utf-8")
    return root


@pytest.fixture()
def repo() -> Repository:
    return Repository.open(MEMORY)


@pytest.fixture()
def registry(repo: Repository, prompts_dir: Path) -> PromptRegistry:
    return PromptRegistry(repo, prompts_dir, rng=random.Random(0))


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


def test_prompt_id_from_path(prompts_dir: Path) -> None:
    assert loader.prompt_id_from_path(prompts_dir / "cgmp" / "section.md", prompts_dir) == (
        "cgmp.section"
    )
    assert loader.prompt_id_from_path(prompts_dir / "_fragments" / "rule.md", prompts_dir) == (
        "_fragments.rule"
    )


def test_loader_splits_front_matter(prompts_dir: Path) -> None:
    file = loader.load_file(prompts_dir / "cgmp" / "section.md", prompts_dir)
    assert file.front_matter["model"] == "chat-standard"
    assert file.body.startswith("# {{ title }}")
    assert "---" not in file.body
    assert file.includes == ["rule"]
    assert file.tags == ["cgmp", "writer"]
    assert file.declared_status == PUBLISHED


def test_loader_rejects_id_mismatch(tmp_path: Path) -> None:
    """ファイルを動かして id を直し忘れる事故を止める。"""
    root = tmp_path / "prompts" / "dde"
    root.mkdir(parents=True)
    (root / "expand.md").write_text("---\nid: dde.wrong\n---\n本文\n", encoding="utf-8")
    with pytest.raises(PromptError, match="一致しません"):
        loader.load_file(root / "expand.md", tmp_path / "prompts")


def test_loader_requires_front_matter(tmp_path: Path) -> None:
    root = tmp_path / "prompts" / "x"
    root.mkdir(parents=True)
    (root / "y.md").write_text("本文だけ\n", encoding="utf-8")
    with pytest.raises(PromptError, match="front matter"):
        loader.load_file(root / "y.md", tmp_path / "prompts")


def test_loader_requires_closed_front_matter(tmp_path: Path) -> None:
    root = tmp_path / "prompts" / "x"
    root.mkdir(parents=True)
    (root / "y.md").write_text("---\nid: x.y\n本文\n", encoding="utf-8")
    with pytest.raises(PromptError, match="閉じられていません"):
        loader.load_file(root / "y.md", tmp_path / "prompts")


def test_loader_separates_fragments(prompts_dir: Path) -> None:
    prompt_set = loader.load_dir(prompts_dir)
    assert set(prompt_set.prompts) == {"cgmp.section"}
    assert set(prompt_set.fragments) == {"_fragments.rule"}


def test_loader_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="ディレクトリがありません"):
        loader.load_dir(tmp_path / "nope")


# ---------------------------------------------------------------------------
# sync と版採番
# ---------------------------------------------------------------------------


def test_sync_registers_prompts_and_fragments(registry: PromptRegistry) -> None:
    results = {r.prompt_id: r for r in registry.sync()}
    assert results["cgmp.section"].version == 1
    assert results["cgmp.section"].action == "created"
    assert results["_fragments.rule"].action == "created"


def test_sync_is_idempotent(registry: PromptRegistry) -> None:
    registry.sync()
    second = {r.prompt_id: r for r in registry.sync()}
    assert all(r.action == "unchanged" for r in second.values())


def test_sync_expands_fragment_into_body(registry: PromptRegistry) -> None:
    registry.sync()
    resolved = registry.resolve("cgmp.section")
    assert "contextに無いことは書かない" in resolved.body
    assert "{{ include." not in resolved.body


def test_changing_prompt_creates_new_version(registry: PromptRegistry, prompts_dir: Path) -> None:
    registry.sync()
    path = prompts_dir / "cgmp" / "section.md"
    path.write_text(SECTION_MD.replace("字以内", "字まで"), encoding="utf-8")
    results = {r.prompt_id: r for r in registry.sync()}
    assert results["cgmp.section"].version == 2
    assert results["cgmp.section"].action == "created"


def test_changing_fragment_versions_the_referrer(
    registry: PromptRegistry, prompts_dir: Path
) -> None:
    """fragment 変更が参照元の新 version を生む(設計 §3.1 の要点)。"""
    registry.sync()
    (prompts_dir / "_fragments" / "rule.md").write_text(
        FRAGMENT_MD.replace("書かない", "決して書かない"), encoding="utf-8"
    )
    results = {r.prompt_id: r for r in registry.sync()}
    assert results["_fragments.rule"].version == 2
    assert results["cgmp.section"].version == 2


def test_first_version_honours_declared_status(registry: PromptRegistry) -> None:
    """初回登録のみ front matter の status を尊重する(N-018)。"""
    registry.sync()
    assert registry.resolve("cgmp.section").status == PUBLISHED


def test_later_versions_start_as_draft(registry: PromptRegistry, prompts_dir: Path) -> None:
    """2回目以降は必ず draft から。状態機械を通らずに本番へ出せない。"""
    registry.sync()
    (prompts_dir / "cgmp" / "section.md").write_text(
        SECTION_MD.replace("字以内", "字まで"), encoding="utf-8"
    )
    registry.sync()
    assert registry.resolve("cgmp.section", 2).status == DRAFT
    # ポインタは v1 のまま(公開していないので本番は変わらない)
    assert registry.resolve("cgmp.section").version == 1


def test_sync_rejects_unknown_status(prompts_dir: Path, repo: Repository) -> None:
    (prompts_dir / "cgmp" / "section.md").write_text(
        SECTION_MD.replace("status: published", "status: live"), encoding="utf-8"
    )
    with pytest.raises(PromptError, match="未知の status"):
        PromptRegistry(repo, prompts_dir).sync()


def test_sync_rejects_nested_fragment_declaration(prompts_dir: Path, repo: Repository) -> None:
    (prompts_dir / "_fragments" / "rule.md").write_text(
        FRAGMENT_MD.replace("description: 共有ルール", "includes: [other]"), encoding="utf-8"
    )
    with pytest.raises(PromptError, match="fragment は他の fragment"):
        PromptRegistry(repo, prompts_dir).sync()


def test_sync_records_transition_and_audit(registry: PromptRegistry, repo: Repository) -> None:
    registry.sync()
    transitions = repo.list_prompt_transitions("cgmp.section")
    assert [t["to_status"] for t in transitions] == [PUBLISHED]
    assert repo.list_audit_logs(event="prompt.sync")


# ---------------------------------------------------------------------------
# resolve / render
# ---------------------------------------------------------------------------


def test_resolve_unknown_prompt(registry: PromptRegistry) -> None:
    with pytest.raises(PromptNotFound):
        registry.resolve("nope.nope")


def test_resolve_unknown_version(registry: PromptRegistry) -> None:
    registry.sync()
    with pytest.raises(PromptNotFound):
        registry.resolve("cgmp.section", 99)


def test_render_is_deterministic(registry: PromptRegistry) -> None:
    """完了条件: 2回連続で同じ render_hash が出ること。"""
    registry.sync()
    resolved = registry.resolve("cgmp.section")
    first = registry.render(resolved, {"title": "T"})
    second = registry.render(resolved, {"title": "T"})
    assert first.hash == second.hash
    assert "1200字以内" in first.text  # スキーマの default が効いている


def test_render_uses_default_model(registry: PromptRegistry) -> None:
    registry.sync()
    assert registry.resolve("cgmp.section").default_model == "chat-standard"


# ---------------------------------------------------------------------------
# 状態機械(設計 §5.2)
# ---------------------------------------------------------------------------


def _new_draft(registry: PromptRegistry, prompts_dir: Path) -> int:
    """v2(draft)を作る。"""
    (prompts_dir / "cgmp" / "section.md").write_text(
        SECTION_MD.replace("字以内", "字まで"), encoding="utf-8"
    )
    return next(r.version for r in registry.sync() if r.prompt_id == "cgmp.section")


def test_happy_path_transitions(registry: PromptRegistry, prompts_dir: Path) -> None:
    registry.sync()
    version = _new_draft(registry, prompts_dir)

    assert registry.submit("cgmp.section").status == REVIEW
    assert registry.approve("cgmp.section").status == APPROVED
    published = registry.publish("cgmp.section")
    assert published.status == PUBLISHED
    assert published.version == version
    # ポインタが切り替わる(FR-050)
    assert registry.resolve("cgmp.section").version == version


def test_reject_returns_to_draft(registry: PromptRegistry, prompts_dir: Path) -> None:
    registry.sync()
    _new_draft(registry, prompts_dir)
    registry.submit("cgmp.section")
    assert registry.reject("cgmp.section").status == DRAFT


def test_cannot_publish_a_draft(registry: PromptRegistry, prompts_dir: Path) -> None:
    registry.sync()
    _new_draft(registry, prompts_dir)
    with pytest.raises(InvalidTransition):
        registry.publish("cgmp.section", 2)


def test_cannot_approve_a_draft(registry: PromptRegistry, prompts_dir: Path) -> None:
    registry.sync()
    _new_draft(registry, prompts_dir)
    with pytest.raises(InvalidTransition):
        registry.approve("cgmp.section", 2)


def test_cannot_submit_twice(registry: PromptRegistry, prompts_dir: Path) -> None:
    registry.sync()
    _new_draft(registry, prompts_dir)
    registry.submit("cgmp.section")
    with pytest.raises(InvalidTransition):
        registry.submit("cgmp.section", 2)


def test_cannot_archive_published(registry: PromptRegistry) -> None:
    registry.sync()
    with pytest.raises(InvalidTransition):
        registry.archive("cgmp.section", 1)


def test_deprecate_then_archive(registry: PromptRegistry) -> None:
    registry.sync()
    assert registry.deprecate("cgmp.section", 1).status == DEPRECATED
    assert registry.archive("cgmp.section", 1).status == ARCHIVED


def test_transition_without_candidate_version(registry: PromptRegistry) -> None:
    registry.sync()  # v1 は published。draft が無い
    with pytest.raises(InvalidTransition, match="draft"):
        registry.submit("cgmp.section")


def test_publish_force_is_audited(
    registry: PromptRegistry, prompts_dir: Path, repo: Repository
) -> None:
    registry.sync()
    _new_draft(registry, prompts_dir)
    registry.submit("cgmp.section")
    registry.approve("cgmp.section")
    registry.publish("cgmp.section", force=True, reason="緊急")
    assert repo.list_audit_logs(event="prompt.publish.forced")


def test_publish_calls_eval_gate(repo: Repository, prompts_dir: Path) -> None:
    """Phase 2 の差し込み口が呼ばれること(Phase 1 では中身なし)。"""
    calls: list[tuple[str, int]] = []

    def gate(prompt_id: str, version: int) -> str | None:
        calls.append((prompt_id, version))
        return "run-1"

    registry = PromptRegistry(repo, prompts_dir, eval_gate=gate)
    registry.sync()
    (prompts_dir / "cgmp" / "section.md").write_text(
        SECTION_MD.replace("字以内", "字まで"), encoding="utf-8"
    )
    registry.sync()
    registry.submit("cgmp.section")
    registry.approve("cgmp.section")
    registry.publish("cgmp.section")

    assert calls == [("cgmp.section", 2)]
    transition = repo.list_prompt_transitions("cgmp.section")[-1]
    assert transition["eval_run_id"] == "run-1"


def test_force_skips_eval_gate(repo: Repository, prompts_dir: Path) -> None:
    def gate(prompt_id: str, version: int) -> str | None:
        pytest.fail("force のときゲートを呼んではいけない")

    registry = PromptRegistry(repo, prompts_dir, eval_gate=gate)
    registry.sync()
    (prompts_dir / "cgmp" / "section.md").write_text(
        SECTION_MD.replace("字以内", "字まで"), encoding="utf-8"
    )
    registry.sync()
    registry.submit("cgmp.section")
    registry.approve("cgmp.section")
    registry.publish("cgmp.section", force=True)


# ---------------------------------------------------------------------------
# rollback / canary
# ---------------------------------------------------------------------------


def _publish_v2(registry: PromptRegistry, prompts_dir: Path) -> None:
    registry.sync()
    _new_draft(registry, prompts_dir)
    registry.submit("cgmp.section")
    registry.approve("cgmp.section")
    registry.publish("cgmp.section")


def test_rollback_moves_pointer_back(registry: PromptRegistry, prompts_dir: Path) -> None:
    _publish_v2(registry, prompts_dir)
    assert registry.resolve("cgmp.section").version == 2
    assert registry.rollback("cgmp.section").version == 1
    assert registry.resolve("cgmp.section").version == 1
    # 版そのものは消えない
    assert registry.resolve("cgmp.section", 2).status == PUBLISHED


def test_rollback_to_explicit_version(registry: PromptRegistry, prompts_dir: Path) -> None:
    _publish_v2(registry, prompts_dir)
    assert registry.rollback("cgmp.section", to=1).version == 1


def test_rollback_without_previous_published(registry: PromptRegistry) -> None:
    registry.sync()
    with pytest.raises(InvalidTransition, match="戻せる"):
        registry.rollback("cgmp.section")


def test_rollback_rejects_non_published_target(
    registry: PromptRegistry, prompts_dir: Path
) -> None:
    registry.sync()
    _new_draft(registry, prompts_dir)
    with pytest.raises(InvalidTransition):
        registry.rollback("cgmp.section", to=2)


def test_canary_splits_by_percent(repo: Repository, prompts_dir: Path) -> None:
    registry = PromptRegistry(repo, prompts_dir, rng=random.Random(7))
    _publish_v2(registry, prompts_dir)
    registry.rollback("cgmp.section", to=1)
    registry.canary("cgmp.section", version=2, percent=50)

    versions = {registry.resolve("cgmp.section").version for _ in range(50)}
    assert versions == {1, 2}


def test_canary_zero_percent_never_selects(repo: Repository, prompts_dir: Path) -> None:
    registry = PromptRegistry(repo, prompts_dir, rng=random.Random(7))
    _publish_v2(registry, prompts_dir)
    registry.rollback("cgmp.section", to=1)
    registry.canary("cgmp.section", version=2, percent=0)
    assert {registry.resolve("cgmp.section").version for _ in range(20)} == {1}


def test_canary_rejects_bad_percent(registry: PromptRegistry, prompts_dir: Path) -> None:
    _publish_v2(registry, prompts_dir)
    with pytest.raises(PromptError, match="0-100"):
        registry.canary("cgmp.section", version=1, percent=101)


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


def test_catalog_lists_without_fragments(registry: PromptRegistry, repo: Repository) -> None:
    registry.sync()
    ids = [e.prompt_id for e in prompt_catalog.list_entries(repo)]
    assert ids == ["cgmp.section"]


def test_catalog_includes_fragments_on_demand(
    registry: PromptRegistry, repo: Repository
) -> None:
    registry.sync()
    ids = [e.prompt_id for e in prompt_catalog.list_entries(repo, include_fragments=True)]
    assert "_fragments.rule" in ids


def test_catalog_filters(registry: PromptRegistry, repo: Repository) -> None:
    registry.sync()
    assert prompt_catalog.list_entries(repo, system="cgmp")
    assert not prompt_catalog.list_entries(repo, system="dde")
    assert prompt_catalog.list_entries(repo, tag="writer")
    assert not prompt_catalog.list_entries(repo, tag="rag")
    assert prompt_catalog.list_entries(repo, status=PUBLISHED)
    assert not prompt_catalog.list_entries(repo, status=REVIEW)


def test_catalog_reports_deployment(registry: PromptRegistry, repo: Repository) -> None:
    registry.sync()
    entry = prompt_catalog.list_entries(repo)[0]
    assert entry.active_version == 1
    assert entry.model == "chat-standard"
    assert entry.owner == "io"
    assert entry.last_used_at is None


def test_catalog_stale_includes_never_used(registry: PromptRegistry, repo: Repository) -> None:
    registry.sync()
    assert [e.prompt_id for e in prompt_catalog.stale(repo, days=90, now="2026-09-20")] == [
        "cgmp.section"
    ]
