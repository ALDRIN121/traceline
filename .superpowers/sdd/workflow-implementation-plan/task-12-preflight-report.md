# T12a rootless runtime preflight report

## Scope

This slice implements setup preflight only. It did not create a CA, start a
container, use Docker, test proxy behavior, or claim containment verification.

## TDD evidence

Initial contract RED (after creating the test and a deliberately empty
`preflight()` seam):

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_runtime_preflight.py
```

Result: `6 failed`; each required result assertion failed because the stub
returned `None`.

Live-diagnosis regression RED (after `podman machine list` showed a running
machine with `Running: true`):

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_runtime_preflight.py
```

Result: `3 failed, 3 passed`. The macOS-ready, rootful, and explicit-socket
fixtures used Podman's observed `Running` machine shape, which the first
implementation did not recognize. The repair uses `podman machine inspect`
(whose native output is JSON) and accepts `Running: true`.

GREEN:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_runtime_preflight.py
```

Result: `6 passed in 0.02s`.

## Current macOS observation

The following was run against the current host:

```bash
.venv/bin/python scripts/runtime_preflight.py
```

```json
{"api_version": "6.1.1", "code": "ready", "connection": {"name": "podman-machine-default", "source": "default"}, "engine_version": "6.1.1", "message": "Rootless Podman setup is ready for the later containment experiment; containment is not verified.", "next_action": "Run the T12 containment, proxy-only egress, and fresh-case conformance tests before dispatching agents.", "runtime_tier": "runc", "state": "ready_for_containment_experiment"}
```

Supporting direct observation used `podman machine list --format json`,
`podman version --format json`, and `podman system connection list --format
json`: `podman-machine-default` was `Running: true`, the client/server version
was `6.1.1`, and the default connection was the rootless `core` user endpoint.

## Files touched

- `src/llm_agent_eval/runtime/__init__.py`
- `src/llm_agent_eval/runtime/podman.py`
- `scripts/runtime_preflight.py`
- `tests/test_runtime_preflight.py`
- `docs/design/runtime-preflight.md`
- this report

## Security boundaries still unproven

Preflight does not prove proxy-only egress; direct IPv4/IPv6, DNS, UDP,
redirect, pinning, or ignored-proxy bypass resistance; credential brokering;
capture/redaction; network packet proof; read-only roots and bounded scratch;
fresh cases; or gVisor/microVM containment. It generates no per-install CA.
Those remain T12/T13 conformance and runtime work.

## Commit

`PENDING`
