"""Prompt Catalog — 一覧・タグ検索・最終利用日(FR-024)。

最終利用日は `spans` から導出する。台帳(Phase 3)に持たせず導出にしておくと、
「記録はあるのに台帳が古い」というズレが構造的に起きない。
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from llmops.db.repository import Repository


@dataclass(frozen=True)
class CatalogEntry:
    prompt_id: str
    version: int
    status: str
    owner: str | None
    description: str | None
    tags: list[str]
    model: str | None
    active_version: int | None
    canary_version: int | None
    canary_percent: int
    source_path: str
    last_used_at: str | None

    @property
    def is_fragment(self) -> bool:
        return self.prompt_id.startswith("_fragments.")


def list_entries(
    repo: Repository,
    *,
    system: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    include_fragments: bool = False,
) -> list[CatalogEntry]:
    """prompt_id ごとの最新版を、条件で絞って返す。

    `system` は prompt_id の先頭要素(`cgmp.section` → `cgmp`)で判定する。
    """
    entries: list[CatalogEntry] = []
    for row in repo.list_prompts(status=status):
        front_matter = yaml.safe_load(str(row["front_matter"])) or {}
        raw_tags = front_matter.get("tags") or []
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
        prompt_id = str(row["prompt_id"])

        entry = CatalogEntry(
            prompt_id=prompt_id,
            version=int(row["version"]),
            status=str(row["status"]),
            owner=None if row["owner"] is None else str(row["owner"]),
            description=None
            if front_matter.get("description") is None
            else str(front_matter["description"]),
            tags=tags,
            model=None if front_matter.get("model") is None else str(front_matter["model"]),
            active_version=None
            if row["active_version"] is None
            else int(row["active_version"]),
            canary_version=None
            if row["canary_version"] is None
            else int(row["canary_version"]),
            canary_percent=int(row["canary_percent"] or 0),
            source_path=str(row["source_path"]),
            last_used_at=repo.prompt_last_used_at(prompt_id),
        )
        if entry.is_fragment and not include_fragments:
            continue
        if system is not None and prompt_id.split(".")[0] != system:
            continue
        if tag is not None and tag not in entry.tags:
            continue
        entries.append(entry)
    return entries


def stale(repo: Repository, *, days: int, now: str) -> list[CatalogEntry]:
    """`days` 日以上使われていない Prompt(未使用を含む)。`now` は 'YYYY-MM-DD ...' 形式。"""
    from datetime import date, timedelta  # noqa: PLC0415 - 局所利用

    threshold = (date.fromisoformat(now[:10]) - timedelta(days=days)).isoformat()
    return [
        entry
        for entry in list_entries(repo)
        if entry.last_used_at is None or entry.last_used_at[:10] < threshold
    ]
