"""`llmops` コマンドのエントリポイント(設計 §7)。

CLI は薄く保つ。判断は各モジュール側に置き、ここは入出力の整形だけを担う。
"""

from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path
from typing import Any

import typer

from llmops.config import Config, load_config
from llmops.db.connection import table_names
from llmops.db.repository import Repository
from llmops.errors import LLMOpsError
from llmops.gateway import Runtime
from llmops.logging_utils import get_logger, setup_logging
from llmops.models import CompletionRequest
from llmops.observability.tracer import new_id
from llmops.prompt import catalog as prompt_catalog
from llmops.prompt.registry import PromptRegistry

logger = get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

app = typer.Typer(
    name="llmops",
    help="LLMOps Control Plane for PEP / APAP / Harness / DDE / CGMP",
    no_args_is_help=True,
)
prompt_app = typer.Typer(
    name="prompt", help="Prompt 資産の一覧・表示・状態遷移", no_args_is_help=True
)
model_app = typer.Typer(name="model", help="論理モデルの一覧・疎通確認", no_args_is_help=True)
app.add_typer(prompt_app)
app.add_typer(model_app)

CONFIG_OPTION = typer.Option(None, "--config", help="config.yaml のパス")


@app.callback()
def main() -> None:
    """ELF CLI."""


def _load(config_path: str | None = None) -> Config:
    config = load_config(config_path)
    setup_logging(level=config.logging.level, log_file=config.log_file)
    return config


def _open(config: Config) -> Repository:
    return Repository.open(config.db_path)


def _registry(config: Config, repo: Repository) -> PromptRegistry:
    return PromptRegistry(repo, config.prompts_dir)


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


def _load_vars(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise typer.BadParameter("--vars のJSONはオブジェクトである必要があります")
    return loaded


@app.command()
def init(config_path: str | None = CONFIG_OPTION) -> None:
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

    repo = _open(config)
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


@app.command()
def sync(config_path: str | None = CONFIG_OPTION) -> None:
    """prompts/**/*.md と models.yaml を DB へ取り込む(ハッシュ差分で新 version を採番)。"""
    config = _load(config_path)
    runtime = Runtime.build(config)
    try:
        prompts = runtime.prompts.sync(actor=config.system)
        models = runtime.models.sync()
    finally:
        runtime.close()

    for result in prompts:
        typer.echo(f"{result.action:9} {result.prompt_id}@{result.version} ({result.status})")
    for model in models:
        typer.echo(f"{model.action:9} model {model.logical_name}@{model.version}")
    typer.echo(
        f"prompts: {len(prompts)} 件(新 version {sum(r.action == 'created' for r in prompts)})"
        f" / models: {len(models)} 件(新 version {sum(m.action == 'created' for m in models)})"
    )


# ---------------------------------------------------------------------------
# prompt サブコマンド
# ---------------------------------------------------------------------------


@prompt_app.command("list")
def prompt_list(
    system: str | None = typer.Option(None, "--system", help="prompt_id の先頭要素で絞る"),
    status: str | None = typer.Option(None, "--status"),
    tag: str | None = typer.Option(None, "--tag"),
    fragments: bool = typer.Option(False, "--fragments", help="fragment も表示する"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """Prompt の一覧(prompt_id ごとの最新版)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        entries = prompt_catalog.list_entries(
            repo, system=system, status=status, tag=tag, include_fragments=fragments
        )
    finally:
        repo.close()

    if not entries:
        typer.echo("該当する Prompt がありません")
        return
    for entry in entries:
        active = "" if entry.active_version is None else f" active=v{entry.active_version}"
        canary = (
            ""
            if entry.canary_version is None
            else f" canary=v{entry.canary_version}:{entry.canary_percent}%"
        )
        tags = f" [{', '.join(entry.tags)}]" if entry.tags else ""
        typer.echo(f"{entry.prompt_id}@{entry.version} ({entry.status}){active}{canary}{tags}")


@prompt_app.command("show")
def prompt_show(
    prompt_id: str,
    version: int | None = typer.Option(None, "--version"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """1版の front matter と本文(fragment 展開後)を表示する。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        resolved = _registry(config, repo).resolve(prompt_id, version)
    finally:
        repo.close()

    typer.echo(f"# {resolved.prompt_id}@{resolved.version} ({resolved.status})")
    typer.echo(f"# source: {resolved.source_path}")
    typer.echo(f"# content_hash: {resolved.content_hash}")
    typer.echo("---")
    typer.echo(resolved.body)


@prompt_app.command("diff")
def prompt_diff(
    prompt_id: str,
    v1: int,
    v2: int,
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """2つの版の本文差分(unified diff)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        registry = _registry(config, repo)
        left = registry.resolve(prompt_id, v1)
        right = registry.resolve(prompt_id, v2)
    finally:
        repo.close()

    diff = difflib.unified_diff(
        left.body.splitlines(keepends=True),
        right.body.splitlines(keepends=True),
        fromfile=f"{prompt_id}@{v1}",
        tofile=f"{prompt_id}@{v2}",
    )
    output = "".join(diff)
    typer.echo(output if output else "(差分なし)")


@prompt_app.command("render")
def prompt_render(
    prompt_id: str,
    vars_file: str | None = typer.Option(None, "--vars", help="変数を入れた JSON ファイル"),
    version: int | None = typer.Option(None, "--version"),
    show_hash: bool = typer.Option(True, "--hash/--no-hash", help="render_hash を表示する"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """実行せずにレンダリング結果と render_hash を表示する(コストゼロ)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        registry = _registry(config, repo)
        resolved = registry.resolve(prompt_id, version)
        result = registry.render(resolved, _load_vars(vars_file))
    finally:
        repo.close()

    typer.echo(result.text)
    if show_hash:
        typer.echo(f"\n--- render_hash: {result.hash} ({resolved.prompt_id}@{resolved.version})")


def _transition_command(
    action: str,
    prompt_id: str,
    version: int | None,
    reason: str | None,
    config_path: str | None,
    **kwargs: Any,
) -> None:
    config = _load(config_path)
    repo = _open(config)
    try:
        registry = _registry(config, repo)
        method = getattr(registry, action)
        resolved = method(prompt_id, version, actor=config.system, reason=reason, **kwargs)
    finally:
        repo.close()
    typer.echo(f"{resolved.prompt_id}@{resolved.version} → {resolved.status}")


@prompt_app.command("submit")
def prompt_submit(
    prompt_id: str,
    version: int | None = typer.Option(None, "--version"),
    reason: str | None = typer.Option(None, "--reason"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """draft → review。"""
    _transition_command("submit", prompt_id, version, reason, config_path)


@prompt_app.command("approve")
def prompt_approve(
    prompt_id: str,
    version: int | None = typer.Option(None, "--version"),
    reason: str | None = typer.Option(None, "--reason"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """review → approved。"""
    _transition_command("approve", prompt_id, version, reason, config_path)


@prompt_app.command("publish")
def prompt_publish(
    prompt_id: str,
    version: int | None = typer.Option(None, "--version"),
    reason: str | None = typer.Option(None, "--reason"),
    force: bool = typer.Option(False, "--force", help="評価ゲートを飛ばす(監査ログに残る)"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """approved → published。ポインタを切り替える(評価ゲートは Phase 2)。"""
    _transition_command("publish", prompt_id, version, reason, config_path, force=force)


@prompt_app.command("deprecate")
def prompt_deprecate(
    prompt_id: str,
    version: int | None = typer.Option(None, "--version"),
    reason: str | None = typer.Option(None, "--reason"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """published → deprecated。"""
    _transition_command("deprecate", prompt_id, version, reason, config_path)


@prompt_app.command("rollback")
def prompt_rollback(
    prompt_id: str,
    to: int | None = typer.Option(None, "--to", help="戻す先の version(既定は直前の published)"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """直前の published 版へポインタを戻す(FR-050)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        resolved = _registry(config, repo).rollback(prompt_id, to, actor=config.system)
    finally:
        repo.close()
    typer.echo(f"active → {resolved.prompt_id}@{resolved.version}")


@prompt_app.command("canary")
def prompt_canary(
    prompt_id: str,
    version: int = typer.Option(..., "--version"),
    percent: int = typer.Option(..., "--percent"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """新版を割合指定で段階適用する(FR-051)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        _registry(config, repo).canary(
            prompt_id, version=version, percent=percent, actor=config.system
        )
    finally:
        repo.close()
    typer.echo(f"canary: {prompt_id}@{version} {percent}%")


if __name__ == "__main__":  # pragma: no cover
    app()


# ---------------------------------------------------------------------------
# model サブコマンド
# ---------------------------------------------------------------------------


@model_app.command("list")
def model_list(config_path: str | None = CONFIG_OPTION) -> None:
    """論理モデル名の一覧(名前ごとの最新版)。"""
    config = _load(config_path)
    runtime = Runtime.build(config)
    try:
        models = runtime.models.list_all()
    finally:
        runtime.close()

    if not models:
        typer.echo("モデルが登録されていません(`llmops sync` を実行してください)")
        return
    for model in models:
        fallback = "" if model.fallback_to is None else f" → fallback={model.fallback_to}"
        typer.echo(
            f"{model.logical_name}@{model.version} adapter={model.adapter} "
            f"({model.status}){fallback}"
        )


@model_app.command("show")
def model_show(logical_name: str, config_path: str | None = CONFIG_OPTION) -> None:
    """1つの論理モデルの解決内容。"""
    config = _load(config_path)
    runtime = Runtime.build(config)
    try:
        model = runtime.models.resolve(logical_name)
    finally:
        runtime.close()

    typer.echo(f"{model.logical_name}@{model.version} ({model.status})")
    typer.echo(f"adapter: {model.adapter}")
    typer.echo(f"params: {json.dumps(model.params, ensure_ascii=False)}")
    typer.echo(f"price: {json.dumps(model.price, ensure_ascii=False)}")
    typer.echo(f"fallback_to: {model.fallback_to}")


@model_app.command("health")
def model_health(config_path: str | None = CONFIG_OPTION) -> None:
    """全 Adapter の疎通確認。LLM は呼ばない(コストゼロ)。"""
    config = _load(config_path)
    runtime = Runtime.build(config)
    failed = 0
    try:
        for model in runtime.models.list_all():
            try:
                healthy = runtime.gateway.adapter(model.adapter).health()
            except LLMOpsError as exc:
                healthy = False
                typer.echo(f"NG   {model.logical_name} ({model.adapter}): {exc}")
                failed += 1
                continue
            mark = "OK  " if healthy else "NG  "
            failed += 0 if healthy else 1
            typer.echo(f"{mark} {model.logical_name} ({model.adapter})")
    finally:
        runtime.close()
    if failed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# run(動作確認・アドホック実行)
# ---------------------------------------------------------------------------


@app.command()
def run(
    prompt_id: str,
    vars_file: str | None = typer.Option(None, "--vars", help="変数を入れた JSON ファイル"),
    model: str | None = typer.Option(None, "--model", help="論理モデル名(既定は front matter)"),
    version: int | None = typer.Option(None, "--version"),
    task: str | None = typer.Option(None, "--task"),
    as_json: bool = typer.Option(False, "--json", help="応答を JSON として解釈する"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """Prompt を1回実行する(trace 1件 + span N件が記録される)。"""
    config = _load(config_path)
    runtime = Runtime.build(config)
    trace_id = new_id()
    try:
        runtime.tracer.start_trace("cli.run", external_id=prompt_id, trace_id=trace_id)
        try:
            result = runtime.gateway.complete(
                CompletionRequest(
                    trace_id=trace_id,
                    task=task or prompt_id.split(".")[-1],
                    prompt_id=prompt_id,
                    variables=_load_vars(vars_file),
                    model=model,
                    version=version,
                    as_json=as_json,
                )
            )
        except Exception:
            runtime.tracer.end_trace(trace_id, status="failed")
            raise
        runtime.tracer.end_trace(trace_id, status="success")
    finally:
        runtime.close()

    typer.echo(result.text)
    typer.echo(
        f"\n--- {result.prompt_id}@{result.prompt_version} model={result.logical_model} "
        f"span={result.span_id} cost={result.cost_usd} "
        f"in={result.input_tokens} out={result.output_tokens} "
        f"{result.duration_ms}ms degraded={result.degraded}"
    )
    typer.echo(f"--- trace: {trace_id}")
