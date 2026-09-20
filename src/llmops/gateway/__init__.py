"""Gateway — 全LLM呼び出しの単一通過点。解決 → Guard → 実行 → 再試行/Fallback → Trace。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llmops.config import Config, load_config
from llmops.db.repository import Repository
from llmops.gateway.gateway import Gateway
from llmops.guard.quota import Guard
from llmops.observability.cost import CostTracker
from llmops.observability.tracer import Tracer
from llmops.prompt.registry import EvalGate, PromptRegistry
from llmops.registry.model_registry import ModelRegistry


@dataclass
class Runtime:
    """Gateway と、その組み立てに使った部品一式。

    CLI と SDK が同じ組み方をするための唯一の場所。ここ以外で Gateway を
    手組みしない(部品の差し替え漏れが起きるため)。
    """

    config: Config
    repo: Repository
    prompts: PromptRegistry
    models: ModelRegistry
    tracer: Tracer
    cost: CostTracker
    guard: Guard
    gateway: Gateway

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        *,
        system: str | None = None,
        eval_gate: EvalGate | None = None,
    ) -> Runtime:
        config = load_config(config_path)
        return cls.build(config, system=system, eval_gate=eval_gate)

    @classmethod
    def build(
        cls,
        config: Config,
        *,
        repo: Repository | None = None,
        system: str | None = None,
        eval_gate: EvalGate | None = None,
    ) -> Runtime:
        resolved_system = system or config.system
        repository = repo or Repository.open(config.db_path)
        prompts = PromptRegistry(repository, config.prompts_dir, eval_gate=eval_gate)
        models = ModelRegistry(repository, config.models_file)
        tracer = Tracer(repository, config, system=resolved_system)
        cost = CostTracker(repository)
        guard = Guard(repository, config, cost)
        gateway = Gateway(
            config=config,
            prompts=prompts,
            models=models,
            guard=guard,
            tracer=tracer,
            cost=cost,
            system=resolved_system,
        )
        return cls(
            config=config,
            repo=repository,
            prompts=prompts,
            models=models,
            tracer=tracer,
            cost=cost,
            guard=guard,
            gateway=gateway,
        )

    def close(self) -> None:
        self.repo.close()


__all__ = ["Gateway", "Runtime"]
