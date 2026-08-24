#!/usr/bin/env sh
# The sample agent's shell entrypoint: run agent.py with the repo venv python
# when present, else any python3 (the agent is stdlib-only and self-bootstraps
# the src tree on import). Smoke: `./entrypoint.sh` from this directory.
set -e
DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x "$DIR/../../.venv/bin/python" ]; then
    exec "$DIR/../../.venv/bin/python" "$DIR/agent.py" "$@"
fi
exec python3 "$DIR/agent.py" "$@"
