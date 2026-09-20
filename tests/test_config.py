"""config.py の検証(Step 1-1)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from llmops.config import ENV_CONFIG, ENV_STRICT_TRACE, config_path, load_config, project_root

CONFIG_YAML = """
system: cgmp
paths:
  db: "var/test.sqlite3"
gateway:
  retry_max: 5
trace:
  strict: false
"""


def _write_config(tmp_path: Path, body: str = CONFIG_YAML) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_repository_config_loads() -> None:
    """同梱の config.yaml が、余計なキーを含まず読めること(extra='forbid')。"""
    config = load_config(project_root() / "config.yaml")
    assert config.system == "elf"
    assert config.guard.calls_per_trace_limit == 10
    assert config.eval.judge_model == "judge"


def test_relative_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    """相対パスは cwd ではなく config.yaml のあるディレクトリ基準(DDE の事故対策)。"""
    config = load_config(_write_config(tmp_path))
    assert config.db_path == tmp_path / "var/test.sqlite3"
    assert config.prompts_dir == tmp_path / "prompts"


def test_absolute_path_is_kept(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, 'paths:\n  db: "/tmp/abs.sqlite3"\n'))
    assert config.db_path == Path("/tmp/abs.sqlite3")


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    config = load_config(tmp_path / "does_not_exist.yaml")
    assert config.system == "elf"
    assert config.gateway.retry_max == 2


def test_values_override_defaults(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path))
    assert config.system == "cgmp"
    assert config.gateway.retry_max == 5
    assert config.gateway.timeout_sec == 600  # 未指定は既定値


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    """設定ミスを黙って無視しない。"""
    with pytest.raises(Exception, match="extra"):
        load_config(_write_config(tmp_path, "gateway:\n  retry_maximum: 3\n"))


def test_env_config_takes_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_config(tmp_path)
    monkeypatch.setenv(ENV_CONFIG, str(path))
    assert config_path() == path.resolve()
    assert load_config().system == "cgmp"


def test_explicit_path_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_CONFIG, str(_write_config(tmp_path)))  # system: cgmp
    sub = tmp_path / "sub"
    sub.mkdir()
    explicit = _write_config(sub, "system: dde\n")
    assert load_config(explicit).system == "dde"


def test_strict_trace_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ELF_STRICT_TRACE=1 で trace.strict を true に上書きする(絶対ルール4)。"""
    path = _write_config(tmp_path)
    assert load_config(path).trace.strict is False
    monkeypatch.setenv(ENV_STRICT_TRACE, "1")
    assert load_config(path).trace.strict is True


@pytest.mark.parametrize("value", ["0", "", "false"])
def test_strict_trace_env_falsy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(ENV_STRICT_TRACE, value)
    assert load_config(_write_config(tmp_path)).trace.strict is False
