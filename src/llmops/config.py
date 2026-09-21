"""config.yaml ローダ(pydantic)。

閾値・上限・モデル名はすべてここ経由で読む(CLAUDE.md 絶対ルール12)。

パス解決は cwd に依存させない。DDE で「別プロジェクトの DB を読む」事故が起きた経緯があるため、
相対パスは常に config.yaml のあるディレクトリ基準で解決する。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_FILENAME = "config.yaml"

#: config.yaml のパスを直接指定する環境変数(相対パス依存を断ち切るための逃げ道)
ENV_CONFIG = "ELF_CONFIG"
#: trace.strict を上書きする環境変数(テスト用)
ENV_STRICT_TRACE = "ELF_STRICT_TRACE"


def project_root() -> Path:
    """リポジトリルート(config.yaml を含むディレクトリ)。

    `src/llmops/config.py` → `src/llmops` → `src` → root の順に遡る。cwd は見ない。
    """
    return Path(__file__).resolve().parents[2]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Base):
    db: str = "data/llmops.sqlite3"
    prompts_dir: str = "prompts"
    models_file: str = "models.yaml"
    evals_dir: str = "evals"
    report_dir: str = "output"


class GatewayConfig(_Base):
    retry_max: int = 2
    #: Prompt の front matter にも呼び出しにも model が無いときの既定(論理名。絶対ルール12/13)
    default_model: str = "chat-standard"
    timeout_sec: int = 600
    allow_unpublished: bool = False
    allow_raw_completion: bool = False
    fallback_enabled: bool = True


class GuardConfig(_Base):
    calls_per_trace_limit: int = 10
    #: system 別の上書き。0 で無制限(NOTES.md N-023)
    calls_per_trace_limit_by_system: dict[str, int] = Field(default_factory=dict)
    hard_quota: bool = True

    def calls_limit_for(self, system: str) -> int:
        return self.calls_per_trace_limit_by_system.get(system, self.calls_per_trace_limit)


class BudgetConfig(_Base):
    global_monthly_usd: float = 20.0
    warn_percents: list[int] = Field(default_factory=lambda: [50, 80, 100])


class TraceConfig(_Base):
    record_request_text: bool = True
    record_response_text: bool = True
    max_text_chars: int = 200000
    strict: bool = False


class EvalConfig(_Base):
    judge_model: str = "judge"
    regression_tolerance: float = 0.02
    deterministic_first: bool = True
    min_score: float = 0.75

    # --- 評価系の安全装置(NOTES.md N-026 の再発防止)------------------------
    #: judge 論理モデルに fallback_to があれば評価の実行を拒否する。
    #: 代替モデルで穴埋めされたスコアは「誰が採点したか」が分からず使えない
    forbid_judge_fallback: bool = True
    #: degraded=1(Fallback 経由)の span が混ざった run は無条件で fail にする。
    #: 縮退実行の結果で品質を判定しない
    fail_on_degraded: bool = True
    #: mock Adapter を通った run は baseline にしない(配線確認モードとして扱う)
    mock_is_wiring_check: bool = True


class LoggingConfig(_Base):
    level: str = "INFO"
    file: str = "logs/llmops.log"


class Config(_Base):
    """config.yaml 全体。`root` は YAML には無く、ロード時に注入する。"""

    system: str = "elf"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    trace: TraceConfig = Field(default_factory=TraceConfig)
    eval: EvalConfig = Field(default_factory=EvalConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    root: Path = Field(default_factory=project_root)

    def resolve(self, relative: str) -> Path:
        """設定中の相対パスを root 基準の絶対パスへ。絶対パスはそのまま返す。"""
        path = Path(relative)
        return path if path.is_absolute() else (self.root / path)

    @property
    def db_path(self) -> Path:
        return self.resolve(self.paths.db)

    @property
    def prompts_dir(self) -> Path:
        return self.resolve(self.paths.prompts_dir)

    @property
    def models_file(self) -> Path:
        return self.resolve(self.paths.models_file)

    @property
    def evals_dir(self) -> Path:
        return self.resolve(self.paths.evals_dir)

    @property
    def report_dir(self) -> Path:
        return self.resolve(self.paths.report_dir)

    @property
    def log_file(self) -> Path:
        return self.resolve(self.logging.file)


def config_path(explicit: str | Path | None = None) -> Path:
    """使用する config.yaml のパスを決める。

    優先順位: 引数 → 環境変数 `ELF_CONFIG` → リポジトリルートの config.yaml。
    `docs/05_既存システム統合.md` §6 のとおり、相対パス依存は環境変数で上書きできる。
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    from_env = os.environ.get(ENV_CONFIG)
    if from_env:
        return Path(from_env).expanduser().resolve()
    return project_root() / DEFAULT_CONFIG_FILENAME


def load_config(path: str | Path | None = None) -> Config:
    """config.yaml を読み込む。ファイルが無い場合は既定値のみで構成する。"""
    resolved = config_path(path)
    raw: dict[str, Any] = {}
    if resolved.is_file():
        loaded = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    config = Config(**raw, root=resolved.parent)
    return _apply_env_overrides(config)


def _apply_env_overrides(config: Config) -> Config:
    """環境変数による上書き。現状は `ELF_STRICT_TRACE` のみ(絶対ルール4)。"""
    strict = os.environ.get(ENV_STRICT_TRACE)
    if strict and strict not in {"0", "", "false", "False"}:
        strict_trace = config.trace.model_copy(update={"strict": True})
        config = config.model_copy(update={"trace": strict_trace})
    return config
