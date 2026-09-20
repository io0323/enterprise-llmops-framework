"""`prompts/**/*.md` の走査と front matter 解析(設計 §3)。

prompt_id はパスから導出する(`prompts/cgmp/section.md` → `cgmp.section`)。
front matter の `id` がパス由来と食い違う場合はエラーにする。ファイルを移動したのに
id を直し忘れる、という事故がそのまま資産の取り違えになるため。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from llmops.errors import PromptError, PromptNotFound

FRONT_MATTER_DELIMITER = "---"
FRAGMENT_DIR = "_fragments"
FRAGMENT_PREFIX = f"{FRAGMENT_DIR}."


@dataclass(frozen=True)
class PromptFile:
    """1ファイル分の Prompt 定義(DBに入る前の形)。"""

    prompt_id: str
    path: Path
    front_matter: dict[str, Any]
    front_matter_text: str
    body: str

    @property
    def is_fragment(self) -> bool:
        return self.prompt_id.startswith(FRAGMENT_PREFIX)

    @property
    def includes(self) -> list[str]:
        raw = self.front_matter.get("includes") or []
        if not isinstance(raw, list):
            raise PromptError(f"{self.path}: includes はリストで書く")
        return [str(name) for name in raw]

    @property
    def declared_status(self) -> str | None:
        status = self.front_matter.get("status")
        return None if status is None else str(status)

    @property
    def declared_version(self) -> int | None:
        version = self.front_matter.get("version")
        return None if version is None else int(version)

    @property
    def owner(self) -> str | None:
        owner = self.front_matter.get("owner")
        return None if owner is None else str(owner)

    @property
    def default_model(self) -> str | None:
        model = self.front_matter.get("model")
        return None if model is None else str(model)

    @property
    def tags(self) -> list[str]:
        raw = self.front_matter.get("tags") or []
        return [str(tag) for tag in raw] if isinstance(raw, list) else []

    @property
    def var_schema(self) -> dict[str, Any] | None:
        schema = self.front_matter.get("variables")
        return schema if isinstance(schema, dict) else None


@dataclass
class PromptSet:
    """prompts ディレクトリ1つ分。Prompt と fragment を分けて保持する。"""

    prompts: dict[str, PromptFile] = field(default_factory=dict)
    fragments: dict[str, PromptFile] = field(default_factory=dict)

    def all_files(self) -> list[PromptFile]:
        return sorted(
            [*self.fragments.values(), *self.prompts.values()], key=lambda f: f.prompt_id
        )

    def get(self, prompt_id: str) -> PromptFile:
        found = self.prompts.get(prompt_id) or self.fragments.get(prompt_id)
        if found is None:
            raise PromptNotFound(f"Prompt が見つかりません: {prompt_id}")
        return found


def prompt_id_from_path(path: Path, prompts_dir: Path) -> str:
    """`prompts/cgmp/section.md` → `cgmp.section`。"""
    relative = path.resolve().relative_to(prompts_dir.resolve()).with_suffix("")
    return ".".join(relative.parts)


def split_front_matter(text: str, path: Path) -> tuple[dict[str, Any], str, str]:
    """front matter(YAML)と本文を分離する。

    Returns:
        (front matter の dict, front matter の原文, 本文)
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        raise PromptError(f"{path}: front matter(先頭の '---')がありません")
    try:
        end = next(
            i for i, line in enumerate(lines[1:], start=1)
            if line.strip() == FRONT_MATTER_DELIMITER
        )
    except StopIteration:
        raise PromptError(f"{path}: front matter が閉じられていません('---' が1つだけ)") from None

    front_matter_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")

    loaded = yaml.safe_load(front_matter_text) if front_matter_text.strip() else {}
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise PromptError(f"{path}: front matter がマッピングではありません")
    return loaded, front_matter_text, body


def load_file(path: Path, prompts_dir: Path) -> PromptFile:
    prompt_id = prompt_id_from_path(path, prompts_dir)
    front_matter, front_matter_text, body = split_front_matter(
        path.read_text(encoding="utf-8"), path
    )

    declared = front_matter.get("id")
    if declared is not None and str(declared) != prompt_id:
        raise PromptError(
            f"{path}: front matter の id({declared})がパス由来の id({prompt_id})と一致しません"
        )

    return PromptFile(
        prompt_id=prompt_id,
        path=path,
        front_matter=front_matter,
        front_matter_text=front_matter_text,
        body=body,
    )


def load_dir(prompts_dir: Path) -> PromptSet:
    """`prompts/**/*.md` を全て読む。fragment は別扱いで保持する。"""
    if not prompts_dir.is_dir():
        raise PromptError(f"prompts ディレクトリがありません: {prompts_dir}")

    result = PromptSet()
    for path in sorted(prompts_dir.rglob("*.md")):
        prompt = load_file(path, prompts_dir)
        target = result.fragments if prompt.is_fragment else result.prompts
        if prompt.prompt_id in target:
            raise PromptError(f"prompt_id が重複しています: {prompt.prompt_id}")
        target[prompt.prompt_id] = prompt
    return result
