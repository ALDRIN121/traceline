# Rootless runtime preflight (T12a)

`scripts/runtime_preflight.py` performs the small, local setup check that gates
the later containment experiment. It selects the isolation tier with the single
`AGENT_RUNTIME` knob (`runc`, `runsc`, or the future `vm` tier), queries Podman
only through fixed argument vectors with timeouts, and prints one JSON result.
It never selects Docker or the trusted subprocess runner as a fallback.

Run it from a source checkout:

```bash
python scripts/runtime_preflight.py
```

The JSON includes `state`, `code`, `message`, `runtime_tier`, `next_action`,
observed engine/API versions, and a connection identity. It does not print a
default socket path, daemon credentials, or raw Podman diagnostics. An explicit
`ENGINE_SOCKET` is accepted only when it is a validated absolute Unix-socket
endpoint with no URI authority, userinfo, or credentials; that explicit value
is returned to make an operator's configured override auditable. Root or
rootful connections are blocked with
`rootless_runtime_required`.

On macOS, install and start a rootless Podman Machine before running preflight.
The preflight detects the running machine and the selected user connection; it
does not assume a host or in-VM socket path. **Docker Desktop on macOS is not
supported**: it is rootful and cannot satisfy this project's isolation design.

`runsc` is classified as unsupported off Linux. `vm` is recognized as the
planned future tier but is not currently available. A missing binary, broken
Podman command/API result, absent default connection, stopped macOS machine,
root connection, or invalid `ENGINE_SOCKET` returns a typed `blocked_setup`
result with an operator action.

## Containment probe (T12b)

The live containment check is deliberately opt-in. Run it only against a
rootless Podman engine:

```bash
LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE=1 PYTHONPATH=src .venv/bin/python -m pytest -q -m live tests/integration/test_egress_boundary.py
```

Without `LLM_AGENT_EVAL_RUN_CONTAINMENT_PROBE=1`, collection reports one
explicit skip and provides no containment evidence. A ready run may pull the
fixed `docker.io/library/alpine:3.20` image. It creates one short-lived
`--internal` network, an HTTP witness container, and a client container using
the rootless engine identity accepted by preflight.

A pass observes the selected rootless engine/API identity, HTTP reachability
from the client to the witness on that internal network, a blocked direct dial
to the fixed external test address, and removal of the exact returned network
after both containers are removed. It is evidence only for that engine and
host at that time. It does not prove proxy-only egress, per-install CA creation
or trust, DNS or IPv6 bypass resistance, UDP behavior, redirects, TLS
interception, authoritative trace-source capture, credential brokering,
packet-level enforcement, or equivalent behavior on another platform.

Docker Desktop and trusted subprocess execution are unsupported runtime paths;
neither is a fallback for this probe.

## What a passing result does not prove

`ready_for_containment_experiment` is necessary setup readiness, not a security
certificate. It does **not** prove proxy-only egress, direct IPv4/IPv6/DNS/UDP
bypass resistance, redirect or TLS behavior, packet-level network proof,
credential brokering, or trace capture. It creates no network, containers, or
CA. The per-install TLS-interception CA must be generated later and must never
be committed. Per-case fresh containers, read-only roots, bounded scratch, and
the complete proxy/network conformance work remain T12/T13 release gates.
