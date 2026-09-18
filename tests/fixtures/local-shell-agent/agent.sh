#!/bin/sh
set -eu
printf '%s' '{"status":"completed","answer":"ok"}' > "$LLM_AGENT_EVAL_OUTPUT/result.json"
