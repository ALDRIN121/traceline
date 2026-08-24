# Sample agent — the healthy evaluation target

A small support-triage agent (stdlib-only, well-formed on purpose) that reads the §11A case
input, calls an env-swappable LLM client (offline mock / live DeepSeek / built-in standalone
fallback), calls module-level registered tools, and traces every LLM call, tool call/result, and
delegation through `llm_agent_eval.capture.TraceCapture`. Smoke: `.venv/bin/python __main__.py`
(or `entrypoint.sh`). The deliberately-broken counterpart is `fixtures/reference-agent/`.
