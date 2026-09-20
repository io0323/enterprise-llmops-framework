"""`llmops` コマンドのエントリポイント。

Phase 0 では Typer app の器のみを置く。サブコマンド(`init` / `sync` / `prompt` / `model` /
`run` / `trace` / `report` / `budget` / `eval` / `catalog` / `audit`)は
`docs/03_詳細設計.md` §7 に従い Phase 1 以降で追加する。
"""

import typer

app = typer.Typer(
    name="llmops",
    help="LLMOps Control Plane for PEP / APAP / Harness / DDE / CGMP",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """ELF CLI."""


if __name__ == "__main__":  # pragma: no cover
    app()
