"""Governance(Phase 3)— 段階展開・台帳・Policy・保持期限。

**Step 3-4 の未公開版の認可**が一番重要。published ゲートを迂回できる経路なので、
「呼び出し側の申告では通らない」ことを挙動で固定する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from llmops.config import Config
from llmops.db.repository import Repository
from llmops.errors import InvalidTransition, PolicyViolation, PromptNotPublished
from llmops.eval.closed_loop import add_manual_case
from llmops.eval.runner import EvalRunner
from llmops.gateway import Runtime
from llmops.governance import retention
from llmops.governance.catalog import AssetCatalog
from llmops.governance.deploy import Deployer
from llmops.governance.weekly import weekly_report
from llmops.guard import policy as policy_module
from llmops.models import CompletionRequest, SpanEnd
from llmops.prompt.registry import CANARY_BUCKETS, canary_bucket
from llmops.sdk import LLMOps

SINCE = "2000-01-01 00:00:00"


def _new_version(workspace: Path, runtime: Runtime, marker: str = "写して") -> int:
    path = workspace / "prompts" / "elf" / "smoke.md"
    path.write_text(path.read_text().replace("書き写して", marker), encoding="utf-8")
    return next(r.version for r in runtime.prompts.sync() if r.prompt_id == "elf.smoke")


def _publish_second_version(runtime: Runtime, workspace: Path) -> int:
    version = _new_version(workspace, runtime)
    runtime.prompts.submit("elf.smoke")
    runtime.prompts.approve("elf.smoke")
    runtime.prompts.publish("elf.smoke")
    return version


# ---------------------------------------------------------------------------
# Step 3-1: canary は trace 単位で決定的
# ---------------------------------------------------------------------------


def test_canary_bucket_is_deterministic() -> None:
    """同じ trace は必ず同じバケット。1 trace 内で版が混ざると比較不能になる。"""
    first = canary_bucket("trace-abc")
    assert first == canary_bucket("trace-abc")
    assert 0 <= first < CANARY_BUCKETS


def test_canary_buckets_are_spread() -> None:
    buckets = {canary_bucket(f"trace-{i}") for i in range(200)}
    assert len(buckets) > 50, "ハッシュが偏りすぎている"


def test_canary_splits_by_percent(runtime: Runtime, workspace: Path) -> None:
    """**完了条件**: 10% 指定で span の版が概ね9:1に分かれる。"""
    runtime.prompts.sync()
    version = _publish_second_version(runtime, workspace)
    runtime.prompts.rollback("elf.smoke", to=1)
    runtime.prompts.canary("elf.smoke", version=version, percent=10)

    chosen = [
        runtime.prompts.resolve("elf.smoke", trace_id=f"trace-{i}").version
        for i in range(1000)
    ]
    canary_share = chosen.count(version) / len(chosen)
    assert 0.05 < canary_share < 0.16, f"振り分けが偏っている: {canary_share:.2%}"


def test_same_trace_never_mixes_versions(runtime: Runtime, workspace: Path) -> None:
    runtime.prompts.sync()
    version = _publish_second_version(runtime, workspace)
    runtime.prompts.rollback("elf.smoke", to=1)
    runtime.prompts.canary("elf.smoke", version=version, percent=50)

    for i in range(50):
        trace_id = f"trace-{i}"
        versions = {
            runtime.prompts.resolve("elf.smoke", trace_id=trace_id).version for _ in range(5)
        }
        assert len(versions) == 1, "同一 trace 内で版が混ざっている"


def test_promote_clears_the_canary(runtime: Runtime, workspace: Path, repo: Repository) -> None:
    runtime.prompts.sync()
    version = _publish_second_version(runtime, workspace)
    runtime.prompts.rollback("elf.smoke", to=1)
    deployer = Deployer(repo, runtime.prompts)
    deployer.canary("elf.smoke", version=version, percent=10)

    state = deployer.promote("elf.smoke", actor="io")

    assert state.active_version == version
    assert state.canary_version is None
    assert state.canary_percent == 0
    assert repo.list_audit_logs(event="prompt.promote")


def test_promote_without_canary_is_refused(runtime: Runtime, repo: Repository) -> None:
    runtime.prompts.sync()
    with pytest.raises(InvalidTransition, match="canary が設定されていません"):
        Deployer(repo, runtime.prompts).promote("elf.smoke")


def test_rollback_stops_the_canary(runtime: Runtime, workspace: Path, repo: Repository) -> None:
    """**完了条件**: rollback が1コマンドで済む。canary も併せて止まる。"""
    runtime.prompts.sync()
    version = _publish_second_version(runtime, workspace)
    deployer = Deployer(repo, runtime.prompts)
    deployer.canary("elf.smoke", version=version, percent=50)

    resolved = deployer.rollback("elf.smoke")

    assert resolved.version == 1
    state = deployer.state("elf.smoke")
    assert state is not None
    assert state.active_version == 1
    assert not state.has_canary


def test_compare_canary_splits_stats(
    runtime: Runtime, workspace: Path, repo: Repository
) -> None:
    runtime.prompts.sync()
    version = _publish_second_version(runtime, workspace)
    runtime.prompts.rollback("elf.smoke", to=1)
    deployer = Deployer(repo, runtime.prompts)
    deployer.canary("elf.smoke", version=version, percent=50)

    for i in range(20):
        trace_id = runtime.tracer.start_trace("t", trace_id=f"trace-{i}")
        runtime.gateway.complete(
            CompletionRequest(
                trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
                variables={"message": "x"}, model="mock-echo",
            )
        )

    comparison = deployer.compare_canary("elf.smoke", since=SINCE)
    assert comparison is not None
    assert comparison.enough_samples
    assert comparison.active["calls"] + comparison.canary["calls"] == 20


# ---------------------------------------------------------------------------
# Step 3-4: 未公開版の実行は「申告」では通らない(最重要)
# ---------------------------------------------------------------------------


def test_unpublished_is_refused_without_evidence(runtime: Runtime, workspace: Path) -> None:
    runtime.prompts.sync()
    version = _new_version(workspace, runtime)
    trace_id = runtime.tracer.start_trace("t")

    with pytest.raises(PromptNotPublished):
        runtime.gateway.complete(
            CompletionRequest(
                trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
                variables={"message": "x"}, model="mock-echo", version=version,
            )
        )


def test_claiming_allow_unpublished_is_a_policy_violation(
    runtime: Runtime, workspace: Path
) -> None:
    """**申告だけでは通らない。** これが権限昇格の入口になる。"""
    runtime.prompts.sync()
    version = _new_version(workspace, runtime)
    trace_id = runtime.tracer.start_trace("t")

    with pytest.raises(PolicyViolation, match="呼び出し側の申告ではなく"):
        runtime.gateway.complete(
            CompletionRequest(
                trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
                variables={"message": "x"}, model="mock-echo", version=version,
                allow_unpublished=True,
            )
        )


def test_eval_run_authorizes_unpublished(
    runtime: Runtime, workspace: Path, config: Config, repo: Repository
) -> None:
    """評価実行の**実体が DB にある**ときだけ通る。"""
    config.evals_dir.mkdir(parents=True, exist_ok=True)
    (config.evals_dir / "elf-smoke.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "elf-smoke", "prompt_id": "elf.smoke", "model": "mock-echo",
                "deterministic": [{"type": "non_empty"}],
                "thresholds": {"min_score": 0.0},
            }
        ),
        encoding="utf-8",
    )
    runner = EvalRunner(runtime, LLMOps.from_runtime(runtime))
    runner.sync()
    runtime.prompts.sync()
    version = _new_version(workspace, runtime)
    add_manual_case(runtime, "elf-smoke", {"message": "x"}, name="c")

    outcome = runner.run("elf-smoke", version=version)

    assert outcome.errors == 0
    spans = repo.list_spans(outcome.trace_id)
    assert spans
    assert json.loads(spans[0]["meta_json"])["unpublished_execution"] == "draft"


def test_unpublished_execution_is_audited(
    runtime: Runtime, workspace: Path, config: Config, repo: Repository
) -> None:
    """どの run が・どの prompt の・どの版に対して使ったかを残す。"""
    config.evals_dir.mkdir(parents=True, exist_ok=True)
    (config.evals_dir / "elf-smoke.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "elf-smoke", "prompt_id": "elf.smoke", "model": "mock-echo",
                "deterministic": [{"type": "non_empty"}],
                "thresholds": {"min_score": 0.0},
            }
        ),
        encoding="utf-8",
    )
    runner = EvalRunner(runtime, LLMOps.from_runtime(runtime))
    runner.sync()
    runtime.prompts.sync()
    version = _new_version(workspace, runtime)
    add_manual_case(runtime, "elf-smoke", {"message": "x"}, name="c")
    outcome = runner.run("elf-smoke", version=version)

    entries = repo.list_audit_logs(event="policy.unpublished_execution")
    assert entries
    detail = json.loads(str(entries[0]["detail_json"]))
    assert detail["reason"] == "eval_run"
    assert detail["eval_run_id"] == outcome.run_id
    assert entries[0]["subject"] == f"elf.smoke@{version}"


def test_config_switch_also_audits(
    config: Config, repo: Repository, workspace: Path
) -> None:
    """開発用の設定で通した場合も記録は残す(「なぜ通ったか」を残す)。"""
    relaxed = config.model_copy(
        update={"gateway": config.gateway.model_copy(update={"allow_unpublished": True})}
    )
    runtime = Runtime.build(relaxed, repo=repo)
    runtime.prompts.sync()
    runtime.models.sync()
    version = _new_version(workspace, runtime)
    trace_id = runtime.tracer.start_trace("t")

    runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo", version=version,
        )
    )

    entries = repo.list_audit_logs(event="policy.unpublished_execution")
    assert entries
    assert json.loads(str(entries[0]["detail_json"]))["reason"] == "config.allow_unpublished"


# ---------------------------------------------------------------------------
# Step 3-4: Policy 検査
# ---------------------------------------------------------------------------


def _policies(tmp_path: Path, entries: list[dict[str, Any]]) -> list[policy_module.Policy]:
    path = tmp_path / "policies.yaml"
    path.write_text(yaml.safe_dump({"policies": entries}, allow_unicode=True), encoding="utf-8")
    return policy_module.load_policies(path)


def test_policy_load_rejects_unknown_rule(tmp_path: Path) -> None:
    with pytest.raises(policy_module.PolicyError, match="未対応の rule"):
        _policies(tmp_path, [{"id": "x", "rule": "vibes"}])


def test_policy_load_rejects_unknown_action(tmp_path: Path) -> None:
    with pytest.raises(policy_module.PolicyError, match="action は"):
        _policies(tmp_path, [{"id": "x", "rule": "no_raw_completion", "action": "maybe"}])


def test_missing_policies_file_is_empty(tmp_path: Path) -> None:
    assert policy_module.load_policies(tmp_path / "nope.yaml") == []


def test_policy_detects_model_fallback(
    runtime: Runtime, repo: Repository, tmp_path: Path
) -> None:
    """**N-026 の再発検知。** 本番モデルに fallback_to が戻ったら止める。"""
    runtime.models.sync()
    policies = _policies(tmp_path, [{"id": "no-fb", "rule": "no_model_fallback"}])

    report = policy_module.check(policies, repo, since=SINCE, models=runtime.models)

    subjects = {v.subject for v in report.violations}
    assert "always-fail" in subjects  # conftest の models.yaml にある fallback_to
    assert not report.ok


def test_policy_allows_listed_models(runtime: Runtime, repo: Repository, tmp_path: Path) -> None:
    runtime.models.sync()
    policies = _policies(
        tmp_path, [{"id": "no-fb", "rule": "no_model_fallback", "allow": ["always-fail"]}]
    )
    report = policy_module.check(policies, repo, since=SINCE, models=runtime.models)
    assert report.ok


def test_policy_detects_unpublished_execution(
    runtime: Runtime, workspace: Path, repo: Repository, tmp_path: Path
) -> None:
    """**Step 3-4 の要求**: 未公開版が実行された形跡を検出できること。"""
    runtime.prompts.sync()
    version = _new_version(workspace, runtime)
    trace_id = runtime.tracer.start_trace("t")
    result = runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo", version=1,
        )
    )
    # 公開版の span を「未公開版で実行した」形に書き換える(評価の裏付けなし)
    repo.conn.execute(
        "UPDATE spans SET prompt_version = ?, meta_json = ? WHERE id = ?",
        (version, json.dumps({"unpublished_execution": "draft"}), result.span_id),
    )
    repo.conn.commit()

    policies = _policies(tmp_path, [{"id": "no-unpub", "rule": "no_unpublished_in_production"}])
    report = policy_module.check(policies, repo, since=SINCE)

    assert not report.ok
    assert f"elf.smoke@{version}" in {v.subject for v in report.violations}


def test_policy_ignores_evaluation_path(
    runtime: Runtime, workspace: Path, config: Config, repo: Repository, tmp_path: Path
) -> None:
    """評価経路の未公開版実行は違反ではない(裏付けがあるため)。"""
    config.evals_dir.mkdir(parents=True, exist_ok=True)
    (config.evals_dir / "elf-smoke.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "elf-smoke", "prompt_id": "elf.smoke", "model": "mock-echo",
                "deterministic": [{"type": "non_empty"}],
                "thresholds": {"min_score": 0.0},
            }
        ),
        encoding="utf-8",
    )
    runner = EvalRunner(runtime, LLMOps.from_runtime(runtime))
    runner.sync()
    runtime.prompts.sync()
    version = _new_version(workspace, runtime)
    add_manual_case(runtime, "elf-smoke", {"message": "x"}, name="c")
    runner.run("elf-smoke", version=version)

    policies = _policies(tmp_path, [{"id": "no-unpub", "rule": "no_unpublished_in_production"}])
    report = policy_module.check(policies, repo, since=SINCE)
    assert report.ok


def test_policy_owner_required(runtime: Runtime, repo: Repository, tmp_path: Path) -> None:
    runtime.prompts.sync()
    catalog = AssetCatalog(runtime)
    assets = catalog.collect(since=SINCE)
    policies = _policies(tmp_path, [{"id": "owner", "rule": "owner_required"}])

    report = policy_module.check(policies, repo, since=SINCE, assets=assets)
    assert not report.ok

    for asset in assets:
        catalog.set_owner(asset.asset_type, asset.asset_id, owner="io")
    report = policy_module.check(
        policies, repo, since=SINCE, assets=catalog.collect(since=SINCE)
    )
    assert report.ok


def test_policy_warn_does_not_block(runtime: Runtime, repo: Repository, tmp_path: Path) -> None:
    runtime.models.sync()
    policies = _policies(
        tmp_path, [{"id": "no-fb", "rule": "no_model_fallback", "action": "warn"}]
    )
    report = policy_module.check(policies, repo, since=SINCE, models=runtime.models)
    assert report.violations
    assert report.ok  # warn は block しない


def test_policy_violations_are_audited(
    runtime: Runtime, repo: Repository, tmp_path: Path
) -> None:
    runtime.models.sync()
    policies = _policies(tmp_path, [{"id": "no-fb", "rule": "no_model_fallback"}])
    policy_module.check(policies, repo, since=SINCE, models=runtime.models)
    assert repo.list_audit_logs(event="policy.violation")


# ---------------------------------------------------------------------------
# Step 3-3: 資産台帳
# ---------------------------------------------------------------------------


def test_catalog_lists_prompts_models_and_suites(runtime: Runtime) -> None:
    runtime.prompts.sync()
    runtime.models.sync()
    assets = AssetCatalog(runtime).collect(since=SINCE)
    types = {a.asset_type for a in assets}
    assert "prompt" in types
    assert "model" in types


def test_catalog_set_owner(runtime: Runtime, repo: Repository) -> None:
    runtime.prompts.sync()
    catalog = AssetCatalog(runtime)
    catalog.set_owner("prompt", "elf.smoke", owner="io", risk="medium", purpose="動作確認")

    asset = next(a for a in catalog.collect(since=SINCE) if a.asset_id == "elf.smoke")
    assert asset.owner == "io"
    assert asset.risk_level == "medium"
    assert asset.purpose == "動作確認"
    assert repo.list_audit_logs(event="catalog.set_owner")


def test_catalog_rejects_unknown_type(runtime: Runtime) -> None:
    with pytest.raises(ValueError, match="未知の資産種別"):
        AssetCatalog(runtime).set_owner("agent", "x", owner="io")


def test_catalog_rejects_unknown_risk(runtime: Runtime) -> None:
    with pytest.raises(ValueError, match="risk は"):
        AssetCatalog(runtime).set_owner("prompt", "elf.smoke", owner="io", risk="extreme")


def test_catalog_without_owner(runtime: Runtime) -> None:
    runtime.prompts.sync()
    catalog = AssetCatalog(runtime)
    assert catalog.without_owner(since=SINCE)
    for asset in catalog.collect(since=SINCE):
        catalog.set_owner(asset.asset_type, asset.asset_id, owner="io")
    assert catalog.without_owner(since=SINCE) == []


def test_catalog_stale_includes_unused(runtime: Runtime) -> None:
    runtime.prompts.sync()
    stale = AssetCatalog(runtime).stale(days=90, today="2026-09-21", since=SINCE)
    assert any(a.asset_id == "elf.smoke" for a in stale)


def test_catalog_last_used_comes_from_spans(runtime: Runtime) -> None:
    runtime.prompts.sync()
    trace_id = runtime.tracer.start_trace("t")
    runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo",
        )
    )
    asset = next(
        a for a in AssetCatalog(runtime).collect(since=SINCE) if a.asset_id == "elf.smoke"
    )
    assert asset.last_used_at is not None
    assert asset.recent_calls == 1


def test_catalog_refresh_writes_back(runtime: Runtime, repo: Repository) -> None:
    runtime.prompts.sync()
    catalog = AssetCatalog(runtime)
    catalog.set_owner("prompt", "elf.smoke", owner="io")
    trace_id = runtime.tracer.start_trace("t")
    runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo",
        )
    )
    assert catalog.refresh_usage() == 1
    row = repo.get_asset("prompt", "elf.smoke")
    assert row is not None
    assert row["last_used_at"] is not None


# ---------------------------------------------------------------------------
# Step 3-5: 保持期限
# ---------------------------------------------------------------------------


def _old_span(runtime: Runtime, repo: Repository, *, days_ago: int) -> str:
    trace_id = runtime.tracer.start_trace("t")
    result = runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "本文"}, model="mock-echo",
        )
    )
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    repo.conn.execute("UPDATE spans SET created_at = ? WHERE id = ?", (stamp, result.span_id))
    repo.conn.commit()
    return result.span_id


def test_retention_dry_run_changes_nothing(
    runtime: Runtime, repo: Repository, config: Config
) -> None:
    runtime.prompts.sync()
    span_id = _old_span(runtime, repo, days_ago=200)

    plan = retention.apply(repo, config, dry_run=True)

    assert span_id in plan.strip_ids
    span = repo.get_span(span_id)
    assert span is not None
    assert span["request_text"]  # 消えていない


def test_retention_strips_text_but_keeps_metadata(
    runtime: Runtime, repo: Repository, config: Config
) -> None:
    """**メタデータは消さない。** 消すと長期の傾向分析ができなくなる。"""
    runtime.prompts.sync()
    span_id = _old_span(runtime, repo, days_ago=200)
    before = repo.get_span(span_id)
    assert before is not None
    repo.update_span(span_id, SpanEnd(success=True, response_text="本文", cost_usd=0.5,
                                      input_tokens=10, output_tokens=20, duration_ms=100))

    retention.apply(repo, config)

    after = repo.get_span(span_id)
    assert after is not None
    assert after["request_text"] == ""
    assert after["response_text"] is None
    # 残すもの
    assert after["cost_usd"] == 0.5
    assert after["input_tokens"] == 10
    assert after["render_hash"] == before["render_hash"]
    assert after["prompt_version"] == before["prompt_version"]


def test_retention_archives_before_deleting(
    runtime: Runtime, repo: Repository, config: Config
) -> None:
    runtime.prompts.sync()
    _old_span(runtime, repo, days_ago=200)

    plan = retention.apply(repo, config)

    assert plan.archive_path is not None
    assert plan.archive_path.is_file()
    archived = [json.loads(line) for line in plan.archive_path.read_text().splitlines()]
    assert archived
    assert archived[0]["request_text"]  # 本文がアーカイブに残っている


def test_retention_deletes_very_old_spans(
    runtime: Runtime, repo: Repository, config: Config
) -> None:
    runtime.prompts.sync()
    span_id = _old_span(runtime, repo, days_ago=800)

    retention.apply(repo, config)

    assert repo.get_span(span_id) is None


def test_retention_keeps_recent_spans(
    runtime: Runtime, repo: Repository, config: Config
) -> None:
    runtime.prompts.sync()
    span_id = _old_span(runtime, repo, days_ago=1)

    plan = retention.apply(repo, config)

    assert plan.empty
    span = repo.get_span(span_id)
    assert span is not None
    assert span["request_text"]


# ---------------------------------------------------------------------------
# Step 3-7: 週次レポート
# ---------------------------------------------------------------------------


def test_weekly_report_covers_the_four_things(runtime: Runtime, config: Config) -> None:
    runtime.prompts.sync()
    runtime.models.sync()
    markdown = weekly_report(runtime, since=SINCE)
    assert "必ず見る4つ" in markdown
    assert "degraded 件数" in markdown
    assert "失敗に使ったコスト" in markdown
    assert "未使用の資産" in markdown
    assert "使われ方の異常" in markdown  # N-046(ハード上限を置けないぶんの見張り)
    assert "閉ループ" in markdown


def test_weekly_report_shows_trace_level_cost(runtime: Runtime) -> None:
    """span 単価ではなく trace 単価を主指標として出す(19章 §13.2)。"""
    runtime.prompts.sync()
    trace_id = runtime.tracer.start_trace("t")
    runtime.gateway.complete(
        CompletionRequest(
            trace_id=trace_id, task="smoke", prompt_id="elf.smoke",
            variables={"message": "x"}, model="mock-echo",
        )
    )
    markdown = weekly_report(runtime, since=SINCE)
    assert "1タスクあたりのコスト(trace 単位)" in markdown


def test_weekly_report_lists_policy_violations(runtime: Runtime, workspace: Path) -> None:
    (workspace / "policies.yaml").write_text(
        yaml.safe_dump({"policies": [{"id": "no-fb", "rule": "no_model_fallback"}]}),
        encoding="utf-8",
    )
    runtime.models.sync()
    markdown = weekly_report(runtime, since=SINCE)
    assert "Policy 違反" in markdown
    assert "no-fb" in markdown
