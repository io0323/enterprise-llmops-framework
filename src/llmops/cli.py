"""`llmops` コマンドのエントリポイント(設計 §7)。

CLI は薄く保つ。判断は各モジュール側に置き、ここは入出力の整形だけを担う。
"""

from __future__ import annotations

import difflib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from llmops.config import Config, load_config
from llmops.db.connection import table_names
from llmops.db.repository import Repository
from llmops.errors import LLMOpsError
from llmops.eval.closed_loop import add_case_from_span, add_manual_case
from llmops.eval.gate import PublishGate
from llmops.eval.regression import MODE_WIRING_CHECK, PASS, WIRING_CHECK
from llmops.eval.report import eval_run_report, quality_report
from llmops.eval.runner import EvalRunner
from llmops.gateway import Runtime
from llmops.logging_utils import get_logger, setup_logging
from llmops.models import CompletionRequest
from llmops.observability.report import cost_report, parse_since
from llmops.observability.tracer import new_id
from llmops.prompt import catalog as prompt_catalog
from llmops.prompt.registry import PromptRegistry
from llmops.sdk import LLMOps

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
trace_app = typer.Typer(name="trace", help="trace / span の参照", no_args_is_help=True)
report_app = typer.Typer(name="report", help="Markdown レポート", no_args_is_help=True)
budget_app = typer.Typer(name="budget", help="予算の確認・設定", no_args_is_help=True)
eval_app = typer.Typer(name="eval", help="評価スイートの実行と回帰ゲート", no_args_is_help=True)
app.add_typer(prompt_app)
app.add_typer(model_app)
app.add_typer(trace_app)
app.add_typer(report_app)
app.add_typer(budget_app)
app.add_typer(eval_app)

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
    """publish の評価ゲートを差し込んだ Registry。

    ゲートの実体は `eval/gate.py`。`gateway` は `eval` を import できない
    (依存の向きの絶対規約)ため、上位である CLI で注入する。
    """
    return PromptRegistry(repo, config.prompts_dir, eval_gate=PublishGate(repo))


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


def _sql_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


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
    """approved → published。ポインタを切り替える。

    評価ゲート(Step 2-4)を通らないと publish できない。`--force` で飛ばせるが、
    そのときは `--reason` が必須で、監査ログに `prompt.publish.forced` が残る。
    """
    if force and not reason:
        raise typer.BadParameter("--force を使うときは --reason で理由を必ず書いてください")
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


# ---------------------------------------------------------------------------
# trace / report / budget
# ---------------------------------------------------------------------------


@trace_app.command("list")
def trace_list(
    since: str = typer.Option("7d", "--since", help="7d / 24h / 2026-09-01"),
    system: str | None = typer.Option(None, "--system"),
    status: str | None = typer.Option(None, "--status", help="running/success/partial/failed"),
    limit: int = typer.Option(50, "--limit"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """trace の一覧(新しい順)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        rows = repo.list_traces(
            since=_sql_time(parse_since(since)), system=system, status=status, limit=limit
        )
        spans = {row["id"]: repo.count_spans(str(row["id"])) for row in rows}
    finally:
        repo.close()

    if not rows:
        typer.echo("該当する trace がありません")
        return
    for row in rows:
        external = "" if row["external_id"] is None else f" ext={row['external_id']}"
        typer.echo(
            f"{row['started_at']} {row['system']:8} {row['operation']:24} "
            f"{row['status']:8} spans={spans[row['id']]}{external} {row['id']}"
        )


@trace_app.command("show")
def trace_show(
    key: str,
    full: bool = typer.Option(False, "--full", help="入出力を全文表示する"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """1つの trace の span を時系列で表示する(trace_id / external_id のどちらでも)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        trace = repo.find_trace(key)
        if trace is None:
            typer.echo(f"trace が見つかりません: {key}")
            raise typer.Exit(code=1)
        spans = repo.list_spans(str(trace["id"]))
    finally:
        repo.close()

    typer.echo(f"trace: {trace['id']}")
    typer.echo(
        f"system={trace['system']} operation={trace['operation']} "
        f"status={trace['status']} external_id={trace['external_id']}"
    )
    typer.echo(f"started={trace['started_at']} finished={trace['finished_at']}")
    typer.echo("")

    for span in spans:
        prompt = (
            "(adhoc)"
            if span["prompt_id"] is None
            else f"{span['prompt_id']}@{span['prompt_version']}"
        )
        flags = []
        if not span["success"]:
            flags.append("FAILED")
        if span["degraded"]:
            flags.append("degraded")
        suffix = f" [{' '.join(flags)}]" if flags else ""
        typer.echo(
            f"#{span['seq']} {span['task']:16} {prompt:28} model={span['logical_model']} "
            f"adapter={span['adapter']} {span['duration_ms']}ms "
            f"in={span['input_tokens']} out={span['output_tokens']} "
            f"cost={span['cost_usd']} render_hash={span['render_hash']}{suffix}"
        )
        if span["error_message"]:
            typer.echo(f"    error: {span['error_type']}: {span['error_message']}")
        if full:
            typer.echo(f"    --- request ---\n{span['request_text']}")
            typer.echo(f"    --- response ---\n{span['response_text']}")


@report_app.command("cost")
def report_cost(
    since: str = typer.Option("30d", "--since", help="7d / 24h / 2026-09-01"),
    by: str = typer.Option("system", "--by", help="system / model / prompt"),
    system: str | None = typer.Option(None, "--system"),
    out: str | None = typer.Option(None, "--out", help="Markdown の出力先"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """コストレポート(Markdown)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        markdown = cost_report(repo, since=parse_since(since), by=by, system=system)
    finally:
        repo.close()

    if out is None:
        typer.echo(markdown)
        return
    path = Path(out)
    if not path.is_absolute():
        path = config.report_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    typer.echo(f"written: {path}")


@budget_app.command("show")
def budget_show(config_path: str | None = CONFIG_OPTION) -> None:
    """予算と当月の使用状況。"""
    config = _load(config_path)
    runtime = Runtime.build(config)
    try:
        states = runtime.guard.budget_states(config.system)
        rows = runtime.repo.list_budgets()
    finally:
        runtime.close()

    if not rows:
        typer.echo("budgets テーブルは空です(config.yaml の budget が使われます)")
    for row in rows:
        hard = "hard" if row["hard_limit"] else "soft"
        enabled = "enabled" if row["enabled"] else "disabled"
        typer.echo(
            f"{row['id']:10} scope={row['scope']:6} period={row['period']:8} "
            f"limit={row['limit_usd']} ({hard}, {enabled})"
        )
    typer.echo("")
    for state in states:
        typer.echo(
            f"{state.budget_id:10} 使用済み {state.used_usd:.6f} / {state.limit_usd:.6f} USD "
            f"({state.percent:.1f}%)"
        )


@budget_app.command("set")
def budget_set(
    scope: str = typer.Option(..., "--scope", help="'global' または system 名"),
    monthly: float = typer.Option(..., "--monthly", help="月額上限(USD)"),
    hard: bool = typer.Option(True, "--hard/--soft", help="超過時に拒否するか"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """予算を登録・更新する。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        repo.upsert_budget(
            budget_id=scope,
            scope="global" if scope == "global" else "system",
            period="monthly",
            limit_usd=monthly,
            hard_limit=hard,
        )
        repo.insert_audit_log(
            event="budget.set",
            actor=config.system,
            subject=scope,
            detail={"monthly_usd": monthly, "hard": hard},
        )
    finally:
        repo.close()
    typer.echo(f"budget: {scope} monthly={monthly} USD ({'hard' if hard else 'soft'})")


@app.command("audit")
def audit_tail(
    event: str | None = typer.Option(None, "--event", help="prompt.publish / quota.exceeded など"),
    limit: int = typer.Option(20, "--limit"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """監査ログの末尾(新しい順)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        rows = repo.list_audit_logs(event=event, limit=limit)
    finally:
        repo.close()

    if not rows:
        typer.echo("監査ログがありません")
        return
    for row in rows:
        typer.echo(
            f"{row['created_at']} {row['event']:22} actor={row['actor']} "
            f"subject={row['subject']} {row['detail_json'] or ''}"
        )


# ---------------------------------------------------------------------------
# eval(Phase 2)
# ---------------------------------------------------------------------------


def _runner(config: Config) -> tuple[Runtime, EvalRunner]:
    runtime = Runtime.build(config)
    ops = LLMOps.from_runtime(runtime)
    return runtime, EvalRunner(runtime, ops)


@eval_app.command("list")
def eval_list(config_path: str | None = CONFIG_OPTION) -> None:
    """評価スイートの一覧(ケース数と出自内訳つき)。"""
    config = _load(config_path)
    runtime, runner = _runner(config)
    try:
        suites = runner.load_suites()
        rows = [
            (suite, runtime.repo.count_eval_cases_by_origin(suite.id)) for suite in suites.values()
        ]
    finally:
        runtime.close()

    if not rows:
        typer.echo(f"評価スイートがありません({config.evals_dir}/*.yaml)")
        return
    for suite, origins in rows:
        total = sum(origins.values())
        breakdown = " ".join(f"{k}={v}" for k, v in sorted(origins.items())) or "ケースなし"
        judged = ", ".join(m.metric for m in suite.judge) or "(決定的評価のみ)"
        typer.echo(f"{suite.id:24} prompt={suite.prompt_id:24} cases={total} [{breakdown}]")
        typer.echo(f"{'':24} judge={judged}")


@eval_app.command("run")
def eval_run(
    suite_id: str,
    version: int | None = typer.Option(None, "--version", help="評価する Prompt 版"),
    model: str | None = typer.Option(None, "--model", help="生成に使う論理モデル"),
    judge_model: str | None = typer.Option(None, "--judge-model"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """スイートを実行する。verdict が pass 以外なら非ゼロ終了する。"""
    config = _load(config_path)
    runtime, runner = _runner(config)
    try:
        runner.sync()
        outcome = runner.run(suite_id, version=version, model=model, judge_model=judge_model)
    finally:
        runtime.close()

    typer.echo(f"run: {outcome.run_id}")
    typer.echo(f"{outcome.suite_id} / {outcome.prompt_id}@{outcome.prompt_version}")
    if outcome.mode == MODE_WIRING_CHECK:
        typer.echo(
            "*** 配線確認モード(mock Adapter 経由)。この結果は品質の判定に使えません。"
            "baseline にも publish の根拠にもなりません ***"
        )
    score = "なし(採点できず)" if outcome.score is None else f"{outcome.score:.4f}"
    typer.echo(
        f"verdict={outcome.verdict} score={score} "
        f"決定的評価 {outcome.passed}/{outcome.total} 件合格 "
        f"採点失敗 {outcome.errors} 件 degraded {outcome.degraded_spans} 件 "
        f"cost={outcome.cost_usd:.6f} USD"
    )
    typer.echo(f"理由: {outcome.reason}")
    for case in outcome.cases:
        mark = "ok  " if case.deterministic_passed else "NG  "
        detail = "" if not case.failed_rules else f" 違反={', '.join(case.failed_rules)}"
        case_score = "-" if case.score is None else f"{case.score:.3f}"
        typer.echo(f"  {mark} {case.name:28} score={case_score}{detail}")
        for error in case.errors:
            typer.echo(f"       採点失敗: {error}")
    # 配線確認は「品質判定をしていない」だけで異常ではないので 0 で返す。
    # ただし publish ゲートは verdict=='pass' しか通さないので、公開の根拠にはならない
    if outcome.verdict not in (PASS, WIRING_CHECK):
        raise typer.Exit(code=1)


@eval_app.command("report")
def eval_report(
    run_id: str,
    out: str | None = typer.Option(None, "--out", help="Markdown の出力先"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """1 run の詳細レポート(Markdown)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        markdown = eval_run_report(repo, run_id)
    finally:
        repo.close()

    if out is None:
        typer.echo(markdown)
        return
    path = Path(out)
    if not path.is_absolute():
        path = config.report_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    typer.echo(f"written: {path}")


@eval_app.command("add-case")
def eval_add_case(
    suite_id: str,
    from_span: str | None = typer.Option(None, "--from-span", help="本番の span_id"),
    vars_file: str | None = typer.Option(None, "--vars", help="変数を入れた JSON ファイル"),
    name: str | None = typer.Option(None, "--name", help="ケース名"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """評価ケースを追加する。

    `--from-span` が**閉ループの要**(Step 2-5)。本番の失敗を回帰ケースに変換する。
    これが回らないと評価スイートは数ヶ月で陳腐化する。週1件でも追加すること。
    `--vars` は初期ケースを手で置くための入口(origin=manual)。
    """
    if (from_span is None) == (vars_file is None):
        raise typer.BadParameter("--from-span か --vars のどちらか一方を指定してください")

    config = _load(config_path)
    runtime, runner = _runner(config)
    try:
        runner.sync()
        if from_span is not None:
            case = add_case_from_span(runtime, suite_id, from_span, name=name)
            origin = "production-failure"
        else:
            case = add_manual_case(
                runtime,
                suite_id,
                _load_vars(vars_file),
                name=name or Path(str(vars_file)).stem,
            )
            origin = "manual"
    finally:
        runtime.close()

    typer.echo(f"追加しました: {case.name} (id={case.case_id})")
    typer.echo(f"  suite={suite_id} origin={origin}")
    if case.missing_variables:
        typer.echo(
            "  注意: span から復元できなかった変数があります: "
            + ", ".join(case.missing_variables)
        )
        typer.echo("  ケースの vars を手で補ってください(空文字で登録しています)")


@report_app.command("quality")
def report_quality(
    since: str = typer.Option("7d", "--since", help="7d / 24h / 2026-09-01"),
    out: str | None = typer.Option(None, "--out", help="Markdown の出力先"),
    config_path: str | None = CONFIG_OPTION,
) -> None:
    """品質レポート(評価の推移と、評価基盤自体の健全性)。"""
    config = _load(config_path)
    repo = _open(config)
    try:
        markdown = quality_report(repo, since=_sql_time(parse_since(since)))
    finally:
        repo.close()

    if out is None:
        typer.echo(markdown)
        return
    path = Path(out)
    if not path.is_absolute():
        path = config.report_dir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    typer.echo(f"written: {path}")
