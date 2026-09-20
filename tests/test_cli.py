"""CLI の器が起動することの最小検証(docs/impl/phase0_bootstrap.md Phase 0 完了条件)。

サブコマンドの検証は Phase 1 以降。ここでは `llmops --help` が空でも動くことだけを見る。
"""

from __future__ import annotations

from typer.testing import CliRunner

from llmops.cli import app

runner = CliRunner()


def test_help_exits_zero() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "llmops" in result.output


def test_no_args_shows_help() -> None:
    """no_args_is_help=True。引数なしでヘルプを出す(Typer の慣例に合わせる)。"""
    result = runner.invoke(app, [])
    assert "Usage" in result.output
