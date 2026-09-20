"""`llmops` コマンドのエントリポイント(設計 §7)。

CLI は薄く保つ。判断は各モジュール側に置き、ここは入出力の整形だけを担う。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from llmops.config import Config, load_config
from llmops.db.connection import table_names
from llmops.db.repository import Repository
from llmops.logging_utils import get_logger, setup_logging

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = typer.Typer(
    name="llmops",
    help="LLMOps Control Plane for PEP / APAP / Harness / DDE / CGMP",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """ELF CLI."""


def _load(config_path: str | None = None) -> Config:
    config = load_config(config_path)
    setup_logging(level=config.logging.level, log_file=config.log_file)
    return config


def _copy_missing(src: Path, dst: Path) -> list[Path]:
    """src 配下のファイルのうち、dst に無いものだけコピーする(既存を上書きしない)。"""
    created: list[Path] = []
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        target = dst / path.relative_to(src)
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        created.append(target)
    return created


@app.command()
def init(
    config_path: str | None = typer.Option(None, "--config", help="config.yaml のパス"),
) -> None:
    """DB を作成し、prompts/ と models.yaml の雛形を用意する(冪等)。"""
    config = _load(config_path)

    for directory in (
        config.db_path.parent,
        config.prompts_dir,
        config.prompts_dir / "_fragments",
        config.evals_dir,
        config.report_dir,
        config.log_file.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    repo = Repository.open(config.db_path)
    try:
        tables = table_names(repo.conn)
    finally:
        repo.close()

    created: list[Path] = _copy_missing(TEMPLATES_DIR / "prompts", config.prompts_dir)
    if not config.models_file.exists():
        shutil.copyfile(TEMPLATES_DIR / "models.yaml", config.models_file)
        created.append(config.models_file)

    typer.echo(f"db: {config.db_path} ({len(tables)} tables)")
    for path in created:
        typer.echo(f"created: {path}")
    if not created:
        typer.echo("雛形は既に存在します(変更なし)")


if __name__ == "__main__":  # pragma: no cover
    app()
