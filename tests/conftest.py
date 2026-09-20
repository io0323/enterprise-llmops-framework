"""テスト共通のフィクスチャ。

`mock` Adapter とインメモリDBだけで完結させる(絶対ルール14: 実APIを叩かない)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llmops.adapters.base import AdapterRequest, AdapterResponse, ProviderAdapter
from llmops.config import Config, load_config
from llmops.db.connection import MEMORY
from llmops.db.repository import Repository
from llmops.errors import AdapterError
from llmops.gateway import Runtime

SMOKE_MD = """---
id: elf.smoke
status: published
owner: io
description: テスト用
tags: [elf, smoke]
model: mock-echo
variables:
  type: object
  required: [message]
  properties:
    message: {type: string}
---
書き写してください: {{ message }}
"""

MODELS_YAML: dict[str, object] = {
    "models": {
        "mock-echo": {"adapter": "mock", "params": {"mode": "echo"}},
        "mock-json": {
            "adapter": "mock",
            "params": {"mode": "json", "payload": {"sections": ["a", "b"]}},
        },
        "always-fail": {
            "adapter": "mock",
            "params": {"mode": "fail"},
            "fallback_to": "mock-echo",
        },
        "fail-no-fallback": {"adapter": "mock", "params": {"mode": "fail"}},
        "priced": {
            "adapter": "mock",
            "params": {"mode": "echo"},
            "price": {"input_per_1k": 1.0, "output_per_1k": 2.0},
        },
        "blocked-model": {"adapter": "mock", "params": {"mode": "echo"}, "status": "blocked"},
    }
}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """config.yaml / prompts / models.yaml が揃った作業ディレクトリ。"""
    (tmp_path / "prompts" / "elf").mkdir(parents=True)
    (tmp_path / "prompts" / "elf" / "smoke.md").write_text(SMOKE_MD, encoding="utf-8")
    (tmp_path / "models.yaml").write_text(
        yaml.safe_dump(MODELS_YAML, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "system": "elf",
                "paths": {"db": "data/llmops.sqlite3"},
                "gateway": {"retry_max": 2},
                "budget": {"global_monthly_usd": 0.0},
                "logging": {"file": "logs/llmops.log"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def config(workspace: Path) -> Config:
    return load_config(workspace / "config.yaml")


@pytest.fixture()
def repo() -> Repository:
    return Repository.open(MEMORY)


@pytest.fixture()
def runtime(config: Config, repo: Repository) -> Runtime:
    """インメモリDBで組んだ Runtime。sync 済み。"""
    runtime = Runtime.build(config, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    return runtime


class CountingAdapter(ProviderAdapter):
    """N 回失敗してから成功する Adapter(retry / fallback の検証用)。

    テストモジュール間で共有するため conftest に置く。テスト同士を import し合うと、
    `python -m pytest` では通るのに `pytest` では ModuleNotFoundError になる
    (前者だけが CWD を sys.path に入れる)。
    """

    name = "counting"

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self.fail_times = fail_times

    def invoke(self, req: AdapterRequest) -> AdapterResponse:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise AdapterError(f"失敗 {self.calls} 回目")
        return AdapterResponse(text=req.text, raw={"result": req.text}, cost_usd=0.0)


@pytest.fixture()
def counting_adapter() -> type[CountingAdapter]:
    """`CountingAdapter` クラスそのものを渡す(失敗回数はテスト側で決める)。"""
    return CountingAdapter
