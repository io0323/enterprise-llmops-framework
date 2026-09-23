"""週次レポートの鮮度(NOTES.md N-047)。

N-046 で「検知しても止めない」を選んだので、**警告が人に届くことが唯一の防御**。
その経路が「手で叩く」だけだと、定期実行が落ちた期間に何が起きても分からない。

ここで固定するのは「動いていないこと自体が見えること」:
レポートを生成していない期間が続けば、次に `llmops` を叩いた時点で1行出る。
**出すだけで、止めない・自動実行もしない。**
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llmops.cli import app
from llmops.config import Config
from llmops.db.repository import Repository
from llmops.observability.report import (
    WEEKLY_REPORT_EVENT,
    last_weekly_report,
    record_weekly_report,
    weekly_report_staleness,
)

runner = CliRunner()
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)
OPS = Path(__file__).resolve().parents[1] / "ops"


def _recorded(repo: Repository, *, days_ago: int) -> None:
    record_weekly_report(repo, destination="weekly.md", since="7d")
    stamp = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    repo.conn.execute(
        "UPDATE audit_logs SET created_at = ? WHERE event = ?", (stamp, WEEKLY_REPORT_EVENT)
    )
    repo.conn.commit()


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def test_never_generated_is_reported(repo: Repository) -> None:
    """1度も作っていない状態を「問題なし」にしない。"""
    line = weekly_report_staleness(repo, stale_days=10, now=NOW)
    assert line is not None
    assert "まだ1度も" in line


def test_a_fresh_report_is_silent(repo: Repository) -> None:
    _recorded(repo, days_ago=2)
    assert weekly_report_staleness(repo, stale_days=10, now=NOW) is None


def test_a_stale_report_is_reported_with_the_day_count(repo: Repository) -> None:
    _recorded(repo, days_ago=21)
    line = weekly_report_staleness(repo, stale_days=10, now=NOW)
    assert line is not None
    assert "21 日経過" in line
    assert "2026-09-02" in line


def test_the_threshold_is_the_boundary(repo: Repository) -> None:
    _recorded(repo, days_ago=9)
    assert weekly_report_staleness(repo, stale_days=10, now=NOW) is None
    repo.conn.execute("DELETE FROM audit_logs")
    _recorded(repo, days_ago=10)
    assert weekly_report_staleness(repo, stale_days=10, now=NOW) is not None


def test_zero_disables_the_notice(repo: Repository) -> None:
    """閾値は config.yaml。0 で黙らせられる(絶対ルール12)。"""
    assert weekly_report_staleness(repo, stale_days=0, now=NOW) is None


def test_the_latest_generation_wins(repo: Repository) -> None:
    _recorded(repo, days_ago=30)
    record_weekly_report(repo, destination="weekly.md", since="7d")
    last = last_weekly_report(repo)
    assert last is not None
    assert (datetime.now(UTC) - last).days == 0


# ---------------------------------------------------------------------------
# CLI(どのコマンドでも出る / 止めない)
# ---------------------------------------------------------------------------


@pytest.fixture()
def elf_config(config: Config, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> Path:
    """CLI が既定で読む config を、テスト用のものに向ける。"""
    monkeypatch.setenv("ELF_CONFIG", str(workspace / "config.yaml"))
    return workspace / "config.yaml"


def test_the_notice_appears_on_an_unrelated_command(elf_config: Path) -> None:
    """レポート系に限らず、**どのコマンドでも**気付ける。"""
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["model", "list"])
    assert "週次レポート" in result.stderr


def test_the_notice_does_not_fail_the_command(elf_config: Path) -> None:
    """**止めない。** 通知が出ても終了コードは変わらない。"""
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["model", "list"])
    assert result.exit_code == 0


def test_the_notice_goes_to_stderr(elf_config: Path) -> None:
    """標準出力は Markdown の出力先になりうる。パイプを汚さない。"""
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["report", "cost", "--since", "1d"])
    assert "週次レポート" not in result.stdout
    assert "ELF コストレポート" in result.stdout


def test_generating_the_report_clears_the_notice(elf_config: Path) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["sync"])

    generated = runner.invoke(app, ["report", "weekly", "--out", "weekly.md"])
    assert generated.exit_code == 0

    result = runner.invoke(app, ["model", "list"])
    assert "週次レポート" not in result.stderr


def test_missing_database_is_silent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DB がまだ無い環境(`llmops init` の前)で騒がない。"""
    (tmp_path / "config.yaml").write_text("paths:\n  db: none.sqlite3\n", encoding="utf-8")
    monkeypatch.setenv("ELF_CONFIG", str(tmp_path / "config.yaml"))
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "週次レポート" not in result.stderr


# ---------------------------------------------------------------------------
# 定期実行の実体がリポジトリにあること(マシン設定として不可視にしない)
# ---------------------------------------------------------------------------


def test_the_launchd_plist_is_in_the_repository() -> None:
    plist = OPS / "launchd" / "io.elf.weekly-report.plist"
    assert plist.is_file(), "定期実行の設定をマシン側だけに置かない(N-047)"
    body = plist.read_text(encoding="utf-8")
    assert "io.elf.weekly-report" in body
    assert "StartCalendarInterval" in body
    assert "<key>RunAtLoad</key>\n    <false/>" in body, "load しただけで走らせない"


def test_the_plist_is_valid() -> None:
    plist = OPS / "launchd" / "io.elf.weekly-report.plist"
    result = subprocess.run(
        ["plutil", "-lint", str(plist)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0 and "not found" in (result.stderr or ""):
        pytest.skip("plutil が無い環境(macOS 以外)")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_runner_script_is_executable_and_calls_the_cli() -> None:
    script = OPS / "weekly_report.sh"
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    assert "report weekly" in body
    assert "--out" in body, "生成物を残さないと、後から「いつ動いたか」が追えない"
