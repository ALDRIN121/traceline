"""Opt-in live evidence for the narrow rootless-Podman containment probe."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from llm_agent_eval.runtime.podman import preflight, run_containment_probe


@pytest.mark.integration
@pytest.mark.live
def test_rootless_podman_internal_network_blocks_direct_egress() -> None:
    """Catches direct egress access or leaked exact probe-network cleanup."""
    if os.environ.get("LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE") != "1":
        pytest.skip("set LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE=1 to run the rootless Podman probe")
    setup = preflight()
    if setup.state != "ready_for_containment_experiment":
        pytest.skip(f"Podman containment preflight is not ready: {setup.code}")
    result = run_containment_probe(setup)
    assert result.state == "containment_probe_observed"
    assert result.in_network_http == "reachable"
    assert result.direct_egress == "blocked"
    assert result.cleanup == "complete"

    assert result.network is not None
    podman = ["podman"]
    if setup.connection and setup.connection.get("source") == "explicit":
        podman.extend(["--url", setup.connection["socket"]])
    network_exists = subprocess.run(
        [*podman, "network", "exists", result.network],
        capture_output=True,
        check=False,
        shell=False,
        text=True,
        timeout=10,
    )
    assert network_exists.returncode == 1


@pytest.mark.parametrize("exists_status", [0, 125, 126, 127, -9])
def test_live_evidence_rejects_unverified_network_absence(
    monkeypatch: pytest.MonkeyPatch, exists_status: int,
) -> None:
    """Catches the live gate accepting Podman errors as confirmed network absence."""
    monkeypatch.setenv("LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE", "1")
    monkeypatch.setitem(globals(), "preflight", lambda: SimpleNamespace(
        state="ready_for_containment_experiment", connection=None,
    ))
    monkeypatch.setitem(globals(), "run_containment_probe", lambda setup: SimpleNamespace(
        state="containment_probe_observed", in_network_http="reachable",
        direct_egress="blocked", cleanup="complete", network="exact-probe-network",
    ))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], exists_status, stdout="", stderr="",
    ))

    with pytest.raises(AssertionError):
        test_rootless_podman_internal_network_blocks_direct_egress()
