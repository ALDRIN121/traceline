"""Environment-driven configuration.

Every value here has a safe default so the whole offline stack (engine, evaluators,
API, dashboard, tests) runs with no secrets present. The DeepSeek key is read only
by live paths (harness authoring against a real provider, live demo runs) — the
offline test suite never touches it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class ModelConfig:
    """Outbound model access for the harness and judges (ModelGateway)."""

    provider: str = "deepseek"
    base_url: str = field(default_factory=lambda: _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    api_key: str = field(default_factory=lambda: _env("DEEPSEEK_API_KEY", ""))
    model: str = field(default_factory=lambda: _env("DEEPSEEK_MODEL", "deepseek-v4-flash"))

    @property
    def has_key(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("sk-your-")


@dataclass(frozen=True)
class HarnessConfig:
    """Design constants for the harness (named config, not TODOs)."""

    analysis_token_budget: int = 120_000  # LLM analysis of a project is capped at this
    pause_restart_cap: int = 3  # max restarts after a paused-turn truncation
    confirmation_ttl_seconds: int = 600  # a confirmation decision expires after 10 min
    spec_repair_attempts: int = 2  # validation-repair loop attempts for LLM-emitted specs


@dataclass(frozen=True)
class RunTiers:
    """Run tiers (plan 4I). Quick ~2 min, Standard 100-200, Full up to the hard cap."""

    quick_max_cases: int = 20
    standard_min_cases: int = 100
    standard_max_cases: int = 200
    full_max_cases: int = 5_000  # hard cap
    warning_threshold_cases: int = 1_000


@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: _env("LLM_AGENT_EVAL_DB", "./eval.db"))
    #: The price-version stamped on cost-bearing events at ingestion
    #: (§12A.5/§12B.2: historical cost totals survive provider price drift only
    #: with the version recorded). Stamped by the engine — the proxy owns this
    #: in the full design (engine §12B.2, egress-proxy cost ledger).
    price_version: str = field(default_factory=lambda: _env("LLM_AGENT_EVAL_PRICE_VERSION", "2026-08"))
    model: ModelConfig = field(default_factory=ModelConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)
    run_tiers: RunTiers = field(default_factory=RunTiers)


settings = Settings()
