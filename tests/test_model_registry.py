"""Model Registry の検証(Step 1-4 / 設計 §2.3 / §4)。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llmops.config import Config
from llmops.db.repository import Repository
from llmops.errors import ModelNotFound
from llmops.registry.model_registry import ModelRegistry, config_hash


@pytest.fixture()
def registry(config: Config, repo: Repository) -> ModelRegistry:
    return ModelRegistry(repo, config.models_file)


def _write_models(path: Path, models: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump({"models": models}, allow_unicode=True), encoding="utf-8")


def test_sync_registers_every_model(registry: ModelRegistry) -> None:
    names = {r.logical_name for r in registry.sync()}
    assert {"mock-echo", "mock-json", "always-fail", "priced", "blocked-model"} <= names


def test_sync_is_idempotent(registry: ModelRegistry) -> None:
    registry.sync()
    assert all(r.action == "unchanged" for r in registry.sync())


def test_config_change_creates_new_version(registry: ModelRegistry, workspace: Path) -> None:
    """Provider 側の silent update を含む設定変更が必ず版として残る(FR-011)。"""
    registry.sync()
    _write_models(
        workspace / "models.yaml",
        {"mock-echo": {"adapter": "mock", "params": {"mode": "echo", "timeout_sec": 30}}},
    )
    results = {r.logical_name: r for r in registry.sync()}
    assert results["mock-echo"].version == 2
    assert results["mock-echo"].action == "created"


def test_config_hash_ignores_key_order() -> None:
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})


def test_resolve_returns_adapter_and_params(registry: ModelRegistry) -> None:
    registry.sync()
    model = registry.resolve("mock-echo")
    assert model.adapter == "mock"
    assert model.params == {"mode": "echo"}
    assert model.version == 1


def test_resolve_reads_price_and_fallback(registry: ModelRegistry) -> None:
    registry.sync()
    priced = registry.resolve("priced")
    assert priced.input_price_per_1k == 1.0
    assert priced.output_price_per_1k == 2.0
    assert registry.resolve("always-fail").fallback_to == "mock-echo"


def test_resolve_defaults_price_to_zero(registry: ModelRegistry) -> None:
    registry.sync()
    model = registry.resolve("mock-echo")
    assert model.input_price_per_1k == 0.0
    assert model.output_price_per_1k == 0.0


def test_blocked_model_is_rejected(registry: ModelRegistry) -> None:
    registry.sync()
    with pytest.raises(ModelNotFound, match="blocked"):
        registry.resolve("blocked-model")


def test_unknown_model_is_rejected(registry: ModelRegistry) -> None:
    registry.sync()
    with pytest.raises(ModelNotFound, match="見つかりません"):
        registry.resolve("nope")


def test_resolve_specific_version(registry: ModelRegistry, workspace: Path) -> None:
    registry.sync()
    _write_models(
        workspace / "models.yaml", {"mock-echo": {"adapter": "mock", "params": {"mode": "json"}}}
    )
    registry.sync()
    assert registry.resolve("mock-echo", 1).params == {"mode": "echo"}
    assert registry.resolve("mock-echo", 2).params == {"mode": "json"}


def test_missing_models_file(repo: Repository, tmp_path: Path) -> None:
    with pytest.raises(ModelNotFound, match="models.yaml がありません"):
        ModelRegistry(repo, tmp_path / "nope.yaml").load_file()


def test_models_file_without_section(repo: Repository, tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("other: {}\n", encoding="utf-8")
    with pytest.raises(ModelNotFound, match="models:"):
        ModelRegistry(repo, path).load_file()


def test_list_all_includes_blocked(registry: ModelRegistry) -> None:
    registry.sync()
    statuses = {m.logical_name: m.status for m in registry.list_all()}
    assert statuses["blocked-model"] == "blocked"
    assert statuses["mock-echo"] == "active"


def test_sync_writes_audit_log(registry: ModelRegistry, repo: Repository) -> None:
    registry.sync()
    assert repo.list_audit_logs(event="model.sync")


def test_repository_models_yaml_is_loadable(repo: Repository) -> None:
    """同梱の models.yaml がそのまま読めること(壊れた状態でコミットしない)。"""
    from llmops.config import project_root

    models = ModelRegistry(repo, project_root() / "models.yaml").load_file()
    assert "chat-standard" in models
    assert models["chat-standard"]["adapter"] == "claude_cli"
