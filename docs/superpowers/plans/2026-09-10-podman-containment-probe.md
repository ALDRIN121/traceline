# Podman Containment Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a rootless-Podman-only, short-lived containment topology probe that shows an internal client can reach its in-network witness but cannot directly dial the external network.

**Architecture:** Extend `runtime/podman.py` with a small lifecycle service, gated by the existing `RuntimePreflight`, which runs only fixed Podman vectors through the injected runner. It creates exact resources, observes local HTTP and a failing external dial, and removes only those named resources in `finally`; it does not become the future proxy.

**Tech Stack:** Python 3.11, rootless Podman, pytest, fixed `docker.io/library/alpine:3.20` probe containers, POSIX shell.

**Spec:** `docs/superpowers/specs/2026-09-10-podman-containment-probe-design.md`

## Global Constraints

- The v3 design and `docs/design/workflow-implementation-plan.md` govern this narrow T12 increment; it is not full T12 completion.
- Require `RuntimePreflight.state == "ready_for_containment_experiment"`; never select Docker, Compose, Docker sockets, or the trusted local subprocess runner.
- Use only fixed vector subprocess calls with timeout and injected runner. Never broad-list or broadly delete resources.
- Use only `docker.io/library/alpine:3.20` for the witness and client; do not build or mount agent code.
- Do not create a CA, certificate, private key, credential, proxy, trace, cassette, provider route, source mount, or engine socket mount.
- A skipped live probe is not evidence. Do not claim DNS, TLS, IPv6/UDP, proxy-only egress, fresh-case execution, or uploaded-agent readiness.

---

### Task 1: Typed, command-safe containment probe

**Files:**
- Modify: `src/llm_agent_eval/runtime/podman.py`
- Modify: `src/llm_agent_eval/runtime/__init__.py`
- Create: `tests/test_runtime_containment_probe.py`

**Interfaces:**
- Consumes: `RuntimePreflight`, `CommandRunner`, `PODMAN_TIMEOUT_SECONDS` from `llm_agent_eval.runtime.podman`.
- Produces: `ContainmentProbeResult(state, code, message, network, in_network_http, direct_egress, cleanup, engine_version)` and `run_containment_probe(preflight_result, *, command_runner=_run_podman, probe_suffix=None) -> ContainmentProbeResult`.
- Uses fixed vectors for `network create --internal`, server/client `run`, `rm -f`, and `network rm`; names have a generated opaque suffix when one is not supplied.

- [ ] **Step 1: Write failing preflight-gate and success-observation tests**

Create a `FakePodman` that records `list[str]` vectors and returns `CompletedProcess` instances. First test the gate:

```python
def test_probe_refuses_resources_when_preflight_is_not_ready() -> None:
    runner = FakePodman({})
    result = run_containment_probe(blocked_preflight(), command_runner=runner, probe_suffix="unit")
    assert result.state == "blocked_setup"
    assert result.network is None
    assert runner.calls == []
```

Then simulate successful `network create`, detached server, alias `wget`, non-zero direct `wget`, and cleanup. Assert `containment_probe_observed`, `reachable`, `blocked`, and `complete`; assert every captured vector begins with `podman` and includes no `docker`, `--volume`, `--mount`, `ENGINE_SOCKET`, or API-key text.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_runtime_containment_probe.py
```

Expected: import/collection failure for missing `ContainmentProbeResult` and `run_containment_probe`. Record that expected failure before implementation.

- [ ] **Step 3: Implement the smallest lifecycle service**

Add the frozen result type and service to `podman.py`. Reject any preflight state other than ready before lifecycle calls. Create an internal network, start `alpine:3.20 httpd -f -p 8080` as `probe`, invoke `wget -q -T 3 -O - http://probe:8080`, then invoke `wget -q -T 3 -O - http://203.0.113.1:81`. A non-zero external command is `blocked`; zero is `egress_boundary_failed`. Track only successful exact names and remove client, server, and network in reverse order from `finally`. A cleanup error returns `cleanup_failed`, preserving observations but never raw command output. Export only the result/service through `runtime/__init__.py` if runtime APIs are exported there.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_runtime_containment_probe.py tests/test_runtime_preflight.py
```

Expected: every test passes; inspect the fake command log to confirm no fallback branch exists.

- [ ] **Step 5: Write failing security-failure and cleanup-failure tests**

Add:

```python
def test_probe_marks_reachable_direct_egress_as_security_failure() -> None:
    result = run_containment_probe(ready_preflight(), command_runner=runner_for(direct_returncode=0), probe_suffix="direct")
    assert result.code == "egress_boundary_failed"
    assert result.direct_egress == "reachable"

def test_probe_reports_cleanup_failure_without_losing_boundary_observations() -> None:
    result = run_containment_probe(ready_preflight(), command_runner=runner_for(network_remove_returncode=1), probe_suffix="cleanup")
    assert result.code == "cleanup_failed"
    assert result.in_network_http == "reachable"
    assert result.direct_egress == "blocked"
```

- [ ] **Step 6: Verify RED, implement classifications, and verify GREEN**

Run the same focused command, observe missing classification failures, implement only those outcomes, then rerun to a pass. Do not add discovery/sweeping behavior.

- [ ] **Step 7: Commit Task 1**

Run `git diff --check`, inspect the runtime/test diff, then run:

```bash
git add src/llm_agent_eval/runtime/podman.py src/llm_agent_eval/runtime/__init__.py tests/test_runtime_containment_probe.py
git commit -m "feat: add Podman containment probe"
```

### Task 2: Opt-in live evidence and platform documentation

**Files:**
- Create: `tests/integration/test_egress_boundary.py`
- Modify: `docs/design/runtime-preflight.md`

**Interfaces:**
- Consumes: `preflight()` and `run_containment_probe()`.
- Produces: a real rootless-Podman check only when `LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE=1`; default collection remains safe and explicit.

- [ ] **Step 1: Write the integration test before the runtime API exists**

```python
@pytest.mark.integration
@pytest.mark.live
def test_rootless_podman_internal_network_blocks_direct_egress() -> None:
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
```

Require the result to preserve its exact network name for a post-cleanup `podman network exists <network>` assertion (a non-zero result confirms deletion). Never use a prefix scan.

- [ ] **Step 2: Verify default guard, then expected RED**

Run first without opt-in:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q -m live tests/integration/test_egress_boundary.py
```

Expected: one explicit opt-in skip, not evidence. Before Task 1 implementation, rerun with `LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE=1`; expected result is missing runtime API/import failure.

- [ ] **Step 3: Add honest runtime documentation**

Add `Containment probe (T12b)` to `docs/design/runtime-preflight.md`: opt-in command, rootless requirement, fixed image pull, observations proven by a pass, and unproven proxy/CA/DNS/IPv6/UDP/redirect/TLS/source/credential/packet/platform items. State Docker Desktop and trusted subprocess execution are unsupported.

- [ ] **Step 4: Run live evidence after Task 1 is green**

Run:

```bash
LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE=1 PYTHONPATH=src .venv/bin/python -m pytest -q -m live tests/integration/test_egress_boundary.py
```

Expected on the current macOS Podman Machine: one passing test with rootless engine/API identity, internal HTTP reachability, blocked direct dial, and exact cleanup. If an egress or cleanup assertion fails, stop and write an incompatibility report; do not weaken isolation.

- [ ] **Step 5: Commit Task 2**

Run `git diff --check`, then:

```bash
git add tests/integration/test_egress_boundary.py docs/design/runtime-preflight.md
git commit -m "test: document Podman containment evidence"
```

### Task 3: Remove the contradicted startup fallback and record status

**Files:**
- Modify: `scripts/spin_up.sh`
- Modify: `tests/test_runtime_containment_probe.py`
- Modify: `docs/design/workflow-implementation-status.md`

**Interfaces:**
- Consumes: the runtime policy that Podman preflight is the only containment setup check.
- Produces: a startup helper with no Docker/local execution branch and a status record based only on observed Task 1/2 evidence.

- [ ] **Step 1: Write a failing source-policy test**

```python
def test_startup_script_never_selects_docker_or_local_execution_fallback() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts/spin_up.sh").read_text(encoding="utf-8")
    assert "docker compose" not in script
    assert "docker info" not in script
    assert "Falling back to local live execution" not in script
    assert "runtime_preflight.py" in script
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_runtime_containment_probe.py::test_startup_script_never_selects_docker_or_local_execution_fallback
```

Expected: failure because the current script starts Docker or a trusted local CLI.

- [ ] **Step 3: Replace fallback with honest setup guidance**

Rewrite `spin_up.sh` with `set -eu` to run `python scripts/runtime_preflight.py`, clearly state that it does not start an agent runtime, and exit non-zero unless its structured result reports ready. It must not invoke Docker, Compose, `podman run`, `eval-engine serve`, or `python -m llm_agent_eval.cli serve`. Point to documented prototype API development commands separately.

- [ ] **Step 4: Verify policy/runtime checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_runtime_preflight.py tests/test_runtime_containment_probe.py
zsh -n scripts/spin_up.sh
scripts/spin_up.sh
```

Expected: Python checks pass, shell syntax passes, and script emits preflight-only output. Its output does not prove containment.

- [ ] **Step 5: Record observed status, verify, and commit**

After the live result, add a `T12b — Internal-network containment probe` status entry containing exact test counts, observed host/Podman version, and pass/skip/failure truth. Retain every unproven capability. Then run:

```bash
git diff --check
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_runtime_preflight.py tests/test_runtime_containment_probe.py
LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE=1 PYTHONPATH=src .venv/bin/python -m pytest -q -m live tests/integration/test_egress_boundary.py
zsh -n scripts/spin_up.sh
git status --short
```

Inspect for certificates, private keys, credentials, Docker containment references, and reference-fixture edits. Commit only the policy/status slice:

```bash
git add scripts/spin_up.sh docs/design/workflow-implementation-status.md tests/test_runtime_containment_probe.py
git commit -m "fix: remove unsafe runtime startup fallback"
```

## Plan self-review

- **Spec coverage:** Task 1 creates typed, fail-closed lifecycle behavior; Task 2 supplies opt-in real evidence and accurate docs; Task 3 removes the contradicting Docker/local fallback and records only evidence actually obtained.
- **Intentional gaps:** CA/proxy/DNS/TLS/credentials/agent images/uploads are explicitly deferred to a later approved design.
- **Type consistency:** `RuntimePreflight` gates `run_containment_probe`; `ContainmentProbeResult` is consumed by the integration test and includes cleanup observations.

## Execution handoff

Plan saved to `docs/superpowers/plans/2026-09-10-podman-containment-probe.md`.

1. **Subagent-Driven (recommended)** — fresh implementer/reviewer for each task.
2. **Inline Execution** — execute in this session with review checkpoints.

