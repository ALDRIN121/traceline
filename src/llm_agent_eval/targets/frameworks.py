"""Explicit R2 framework-adapter capability declarations.

The matrix is intentionally small. An adapter is listed only after its
conformance tests exist; unknown frameworks are returned as unsupported rather
than being treated as generic success.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterCapability:
    framework: str
    supported: bool
    versions: tuple[str, ...]
    capabilities: dict[str, str]
    conformance_tests: tuple[str, ...]
    reason: str | None = None


_GENERIC_HTTP = AdapterCapability(
    framework="generic_http",
    supported=True,
    versions=("stateless_json", "streaming", "async", "session"),
    capabilities={
        "final_output": "observed",
        "retrieval": "declared",
        "tool_execution": "unavailable",
        "provider_cost": "unavailable",
    },
    conformance_tests=(
        "tests/workflow/test_http_target.py",
        "tests/workflow/test_http_outcomes.py",
        "tests/workflow/test_stream_target.py",
        "tests/workflow/test_stateful_target.py",
    ),
)

_OPENAI_COMPATIBLE = AdapterCapability(
    framework="openai_compatible",
    supported=True,
    versions=("stateless_json",),
    capabilities={
        "final_output": "observed",
        "retrieval": "unavailable",
        "tool_execution": "unavailable",
        "provider_cost": "unavailable",
    },
    conformance_tests=("tests/workflow/test_openai_compatible_adapter.py",),
)


def supported_adapters() -> tuple[AdapterCapability, ...]:
    """Return the immutable list of adapters with checked-in conformance tests."""
    return (_GENERIC_HTTP, _OPENAI_COMPATIBLE)


def adapter_capability(
    framework: str,
    *,
    version: str | None = None,
    require_supported: bool = False,
) -> AdapterCapability:
    """Resolve an adapter declaration, failing closed for unknown versions."""
    normalized = framework.strip().lower() if isinstance(framework, str) else ""
    candidate = next((item for item in supported_adapters() if item.framework == normalized), None)
    if candidate is not None and (version is None or version in candidate.versions):
        return candidate
    unsupported = AdapterCapability(
        framework=normalized or "unknown",
        supported=False,
        versions=(),
        capabilities={},
        conformance_tests=(),
        reason="unsupported_framework",
    )
    if require_supported:
        raise ValueError("unsupported_framework")
    return unsupported
