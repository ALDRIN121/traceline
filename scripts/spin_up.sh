#!/usr/bin/env bash
set -eu

echo "LLM Agent Evaluation runtime setup check"
echo "This helper does not start an agent runtime, API, or containment probe."

if python scripts/runtime_preflight.py; then
    echo "Rootless Podman setup is ready for the separately opt-in containment experiment."
    echo "This result is setup readiness only; it does not prove containment."
    echo "For prototype API development, use the documented commands in README.md."
else
    echo "Rootless Podman setup is not ready; no runtime was started." >&2
    exit 1
fi
