"""publish の評価ゲート(Step 2-4。このPhaseの成果物)。

**Prompt 変更を、評価を通さずに本番へ出せないこと**を挙動で固定する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from llmops.db.repository import Repository
from llmops.errors import EvaluationFailed, InvalidTransition
from llmops.eval import regression
from llmops.eval.gate import PublishGate
from llmops.gateway import Runtime
from llmops.prompt.registry import APPROVED, DRAFT, PUBLISHED, PromptRegistry

SMOKE_V2 = "書き写してください(v2): {{ message }}\n"


@pytest.fixture()
def gated(runtime: Runtime, repo: Repository) -> PromptRegistry:
    """評価ゲートを差し込んだ Registry(CLI と同じ組み方)。"""
    return PromptRegistry(repo, runtime.config.prompts_dir, eval_gate=PublishGate(repo))


def _new_version(workspace: Path, registry: PromptRegistry) -> int:
    path = workspace / "prompts" / "elf" / "smoke.md"
    path.write_text(
        path.read_text().replace("書き写してください", "写してください"), encoding="utf-8"
    )
    return next(r.version for r in registry.sync() if r.prompt_id == "elf.smoke")


def _approve(registry: PromptRegistry) -> None:
    registry.submit("elf.smoke")
    registry.approve("elf.smoke")


def _record_run(
    repo: Repository,
    *,
    version: int,
    verdict: str,
    mode: str = regression.MODE_EVALUATION,
    degraded: int = 0,
    run_id: str = "run-1",
) -> str:
    repo.insert_eval_run(
        run_id=run_id, suite_id="elf-smoke", prompt_id="elf.smoke", prompt_version=version,
        logical_model="chat-standard", judge_model="judge", total=1, mode=mode, trace_id="t",
    )
    repo.finish_eval_run(
        run_id, passed=1, score=0.9, verdict=verdict, baseline_run_id=None,
        cost_usd=0.0, errors=0, degraded_spans=degraded, note="test",
    )
    return run_id


# ---------------------------------------------------------------------------
# 評価を通さずに publish できないこと
# ---------------------------------------------------------------------------


def test_publish_without_any_eval_run_is_refused(
    gated: PromptRegistry, workspace: Path
) -> None:
    """**完了条件**: 評価していない版は publish できない。"""
    gated.sync()
    _new_version(workspace, gated)
    _approve(gated)

    with pytest.raises(EvaluationFailed, match="評価実行がありません"):
        gated.publish("elf.smoke")


def test_publish_with_a_failed_run_is_refused(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    gated.sync()
    version = _new_version(workspace, gated)
    _approve(gated)
    _record_run(repo, version=version, verdict=regression.FAIL)

    with pytest.raises(EvaluationFailed, match="verdict"):
        gated.publish("elf.smoke")


def test_publish_with_a_regressed_run_is_refused(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    """**完了条件**: 意図的にスコアを下げた版で publish が失敗する。"""
    gated.sync()
    version = _new_version(workspace, gated)
    _approve(gated)
    _record_run(repo, version=version, verdict=regression.REGRESSED)

    with pytest.raises(EvaluationFailed, match="regressed"):
        gated.publish("elf.smoke")


def test_publish_with_an_old_run_is_refused(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    """**完了条件**: 古い eval_run しか無い状態で publish が拒否される。"""
    gated.sync()
    _record_run(repo, version=1, verdict=regression.PASS, run_id="run-old")
    _new_version(workspace, gated)  # v2 を作る(評価はしていない)
    _approve(gated)

    with pytest.raises(EvaluationFailed, match="評価実行がありません"):
        gated.publish("elf.smoke")


def test_publish_with_a_wiring_check_run_is_refused(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    """配線確認(mock 経由)は公開の根拠にならない(N-026 の再発防止)。"""
    gated.sync()
    version = _new_version(workspace, gated)
    _approve(gated)
    _record_run(
        repo, version=version, verdict=regression.WIRING_CHECK,
        mode=regression.MODE_WIRING_CHECK,
    )

    with pytest.raises(EvaluationFailed, match="評価実行がありません"):
        gated.publish("elf.smoke")


def test_publish_with_degraded_spans_is_refused(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    """verdict が pass でも、縮退実行が混ざっていたら公開しない。"""
    gated.sync()
    version = _new_version(workspace, gated)
    _approve(gated)
    _record_run(repo, version=version, verdict=regression.PASS, degraded=2)

    with pytest.raises(EvaluationFailed, match="縮退実行"):
        gated.publish("elf.smoke")


# ---------------------------------------------------------------------------
# 通る場合
# ---------------------------------------------------------------------------


def test_publish_succeeds_with_a_passing_run(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    gated.sync()
    version = _new_version(workspace, gated)
    _approve(gated)
    _record_run(repo, version=version, verdict=regression.PASS)

    published = gated.publish("elf.smoke")

    assert published.status == PUBLISHED
    assert published.version == version
    assert gated.resolve("elf.smoke").version == version


def test_publish_records_the_eval_run_as_evidence(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    """`prompt_transitions.eval_run_id` に根拠が残る(Step 2-4)。"""
    gated.sync()
    version = _new_version(workspace, gated)
    _approve(gated)
    run_id = _record_run(repo, version=version, verdict=regression.PASS)

    gated.publish("elf.smoke")

    transition = repo.list_prompt_transitions("elf.smoke")[-1]
    assert transition["to_status"] == PUBLISHED
    assert transition["eval_run_id"] == run_id


# ---------------------------------------------------------------------------
# --force(緊急用。監査に残す)
# ---------------------------------------------------------------------------


def test_force_skips_the_gate_and_is_audited(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    """**完了条件**: `--force --reason "..."` で通り、監査ログに残る。"""
    gated.sync()
    _new_version(workspace, gated)
    _approve(gated)

    published = gated.publish("elf.smoke", force=True, reason="緊急対応: 誤字の修正")

    assert published.status == PUBLISHED
    forced = repo.list_audit_logs(event="prompt.publish.forced")
    assert forced
    assert "緊急対応" in str(forced[0]["detail_json"])


def test_force_does_not_record_an_eval_run_id(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    """飛ばした以上、根拠は残らない(残っていたら嘘になる)。"""
    gated.sync()
    _new_version(workspace, gated)
    _approve(gated)
    gated.publish("elf.smoke", force=True, reason="緊急")

    transition = repo.list_prompt_transitions("elf.smoke")[-1]
    assert transition["eval_run_id"] is None


# ---------------------------------------------------------------------------
# 状態機械との関係(ゲートは approved の後に効く)
# ---------------------------------------------------------------------------


def test_gate_runs_after_the_state_machine(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    """draft のまま publish しようとしたら、評価以前に状態機械が止める。"""
    gated.sync()
    version = _new_version(workspace, gated)
    _record_run(repo, version=version, verdict=regression.PASS)

    with pytest.raises(InvalidTransition):
        gated.publish("elf.smoke", version)
    assert gated.resolve("elf.smoke", version).status == DRAFT


def test_approved_version_is_the_gate_target(
    gated: PromptRegistry, workspace: Path, repo: Repository
) -> None:
    gated.sync()
    version = _new_version(workspace, gated)
    _approve(gated)
    assert gated.resolve("elf.smoke", version).status == APPROVED


# ---------------------------------------------------------------------------
# ゲート単体
# ---------------------------------------------------------------------------


def test_gate_returns_the_run_id(repo: Repository) -> None:
    run_id = _record_run(repo, version=3, verdict=regression.PASS)
    assert PublishGate(repo)("elf.smoke", 3) == run_id


def test_gate_ignores_runs_for_other_versions(repo: Repository) -> None:
    _record_run(repo, version=3, verdict=regression.PASS)
    with pytest.raises(EvaluationFailed, match="評価実行がありません"):
        PublishGate(repo)("elf.smoke", 4)
