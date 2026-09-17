"""Environment-driven configuration.

Every value here has a safe default so the whole offline stack (engine, evaluators,
API, dashboard, tests) runs with no secrets present. The DeepSeek key is read only
by live paths (harness authoring against a real provider, live demo runs) — the
offline test suite never touches it.

``.env`` handling (no third-party deps): if a ``.env`` file exists in the working
directory or the repository root it is parsed into ``os.environ`` with
``setdefault`` — a real exported variable always wins, and the placeholder value
from ``.env.example`` never satisfies ``has_key``. Missing file, malformed lines,
and absent variables are all no-ops: the app never fails because ``.env`` is
missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _load_dotenv() -> None:
    """Parse ``.env`` into ``os.environ`` (setdefault — real env wins).

    A deliberately small, safe subset: ``KEY=VALUE`` lines, ``#`` comments and
    blanks skipped, surrounding quotes stripped, nothing interpolated, nothing
    exported, never raises. Only files under the repo root are ever read — the
    repo never reads ``.env`` from anywhere else.
    """
    candidates = (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env")
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
        except OSError:
            continue  # unreadable .env is a no-op, never a crash


_load_dotenv()


@dataclass(frozen=True)
class ModelConfig:
    """Outbound model access for the harness and judges (ModelGateway)."""

    # Generic profile variables are the release interface. The DEEPSEEK names
    # remain aliases so existing self-hosted installs do not silently change.
    provider: str = field(default_factory=lambda: _env("LLM_AGENT_EVAL_MODEL_PROVIDER", "deepseek"))
    base_url: str = field(default_factory=lambda: _env("LLM_AGENT_EVAL_MODEL_BASE_URL", _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")))
    api_key: str = field(default_factory=lambda: _env("LLM_AGENT_EVAL_MODEL_API_KEY", _env("DEEPSEEK_API_KEY", "")))
    model: str = field(default_factory=lambda: _env("LLM_AGENT_EVAL_MODEL", _env("DEEPSEEK_MODEL", "deepseek-v4-flash")))
    #: LiteLLM uses this for Azure deployments and compatible gateways.  It is
    #: deliberately optional because most providers do not accept it.
    api_version: str | None = field(default_factory=lambda: os.environ.get("LLM_AGENT_EVAL_MODEL_API_VERSION"))
    #: Local endpoints (for example Ollama) must opt in to keyless operation.
    allow_keyless: bool = field(default_factory=lambda: _env("LLM_AGENT_EVAL_MODEL_ALLOW_KEYLESS", "false").lower() == "true")

    @property
    def has_key(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("sk-your-")

    @property
    def can_authenticate(self) -> bool:
        return self.has_key or self.allow_keyless


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
    db_path: str = field(default_factory=lambda: _env("DATABASE_URL", _env("LLM_AGENT_EVAL_DB", "./eval.db")))
    artifact_root: str = field(default_factory=lambda: _env("LLM_AGENT_EVAL_ARTIFACT_ROOT", "./.artifacts"))
    artifact_max_bytes: int = 16 * 1024 * 1024
    #: The price-version stamped on cost-bearing events at ingestion
    #: (§12A.5/§12B.2: historical cost totals survive provider price drift only
    #: with the version recorded). Stamped by the engine — the proxy owns this
    #: in the full design (engine §12B.2, egress-proxy cost ledger).
    price_version: str = field(default_factory=lambda: _env("LLM_AGENT_EVAL_PRICE_VERSION", "2026-08"))
    model: ModelConfig = field(default_factory=ModelConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)
    run_tiers: RunTiers = field(default_factory=RunTiers)


settings = Settings()
