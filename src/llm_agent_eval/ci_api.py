"""Small CI-facing status contract shared by HTTP and command-line callers."""

from __future__ import annotations


def exit_code(run: dict) -> int:
    """Return 0 only for an observed complete run with passing active gates."""
    if run.get("status") != "complete":
        return 2
    metrics = run.get("metrics") or []
    if any(metric.get("gate_status") == "FAIL" for metric in metrics):
        return 1
    if any(metric.get("gate_status") in {None, "NOT_APPLICABLE"} or metric.get("no_ci") for metric in metrics):
        return 2
    return 0
