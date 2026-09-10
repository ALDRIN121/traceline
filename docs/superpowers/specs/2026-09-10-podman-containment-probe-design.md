# Podman Containment Probe Design

**Status:** proposed and approved for planning on 2026-09-10

## Purpose

Create the first executable proof of the T12 containment boundary on a rootless
Podman host. The probe must show that a disposable agent-like container can
reach only an explicitly attached in-network probe service, while an attempted
direct outbound connection fails. It is deliberately not an agent runner and
does not constitute proxy-only egress certification.

## Scope

The increment has three responsibilities:

1. A focused runtime service creates and removes a uniquely named, internal
   Podman network and runs a short-lived probe container connected only to that
   network. It invokes Podman through fixed argument vectors, requires the
   existing rootless preflight to be ready, applies no host or engine socket
   mount, and always attempts cleanup.
2. A small, local HTTP probe service runs inside a second container on the same
   network. It has no provider credentials, access to agent source, CA material,
   or outbound forwarding behavior. A successful in-network request is evidence
   only that the topology is wired as intended.
3. A live integration test invokes the probe through rootless Podman and records
   the engine version, internal-network identity, successful in-network result,
   failed direct-outbound result, and cleanup result. It is opt-in and skipped
   when the host does not pass T12a; a skipped test is not evidence.

The system will expose typed outcomes distinguishing unavailable runtime,
failed setup, successful in-network path, unexpected outbound reachability,
and cleanup failure. Operator-facing output will never include raw engine
diagnostics, socket credentials, provider credentials, or container IDs beyond
short opaque labels needed for cleanup auditing.

## Explicitly out of scope

- TLS interception, CA generation, CA storage, and CA trust injection.
- Provider credential substitution, LiteLLM outbound routing, metering,
  redaction, cassettes, fault injection, or trace ingestion.
- DNS rewriting, SNI routing, IPv6, UDP, redirect, certificate-pinning, and
  alternate-resolver proofs.
- Agent image building, source mounts, invocation protocol, resource limits,
  fresh-per-case execution, durable workers, or uploaded-agent dispatch.
- Docker, Docker Compose, and trusted local-process fallback.

No private key is created, written, or committed by this work.

## Architecture

`ContainmentProbe` is a small orchestration layer in
`llm_agent_eval.runtime.podman`. It owns only runtime lifecycle commands and a
typed result. A caller first obtains the existing `RuntimePreflight`; the probe
rejects anything other than `ready_for_containment_experiment`.

For one probe attempt it creates an internal network named from a generated
opaque suffix, starts a fixed HTTP-server image with `--network` set to that
network, and starts a fixed client image on the same network. The client first
requests the server through its service alias and then attempts a direct dial
to a documented non-routable probe address. The latter must fail within a
bounded timeout. `finally` cleanup removes client, server, and network in that
order, without broad list-and-delete operations.

The network is internal: the client receives no default route to external
networks. The server is only a topology witness, not a proxy. The exact
Podman command construction remains private to the module, with an injected
command runner for deterministic unit tests.

## Failure handling

- Preflight not ready: return a typed `blocked_setup` result and make no
  lifecycle calls.
- Any create/run failure: return `containment_probe_failed`, then attempt
  cleanup of only resources created by this invocation.
- Direct outbound connection succeeds: return `egress_boundary_failed`; this is
  a security failure, not a warning or a fallback condition.
- Cleanup failure: return `cleanup_failed` even if both network observations
  succeeded, retaining the earlier observation as evidence.
- Command timeouts and malformed output: return a classified failed result,
  with no raw subprocess output in the public result.

## Test strategy and evidence

Unit tests use an injected Podman command runner to assert exact fixed argument
vectors and the typed outcomes above. They must be written and observed failing
before runtime implementation. Unit tests also prove the module never invokes
Docker and never receives a socket mount or provider credential argument.

The integration test is marked `integration` and `live`; it is opt-in through
an explicit environment flag and only runs after rootless preflight reports
ready. It proves a real rootless Podman network permits the local probe alias,
rejects the direct outbound dial, and leaves no exact resources created by the
test. The test records versions and observations as an artifact or structured
test output. macOS is the first observed platform; Linux and WSL2 remain open
until each has its own observed result.

## Acceptance criteria

1. No code path in the setup or containment probe selects Docker, Docker
   Compose, or a local subprocess runner as an alternative runtime.
2. A ready rootless Podman preflight is required before resource creation.
3. The live probe creates an internal network and two containers with no engine
   socket, credential, CA, or agent-source mount.
4. The local alias request succeeds; the direct outbound dial fails closed;
   each outcome is explicit in the typed result.
5. Cleanup is exact, bounded, and verified; failures are classified rather
   than silently ignored.
6. Documentation says this is a limited topology probe, not proof of the full
   three-layer egress design or proxy implementation.

## Follow-on boundary

Only after this increment passes review and live evidence exists may the next
design introduce a per-install CA in private install state and a TLS-terminating
proxy. That design must prove no key is committed, that distinct installs have
distinct keys, and that the sandbox receives only dummy credentials.
