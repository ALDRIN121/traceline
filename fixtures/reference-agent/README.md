# Reference agent — the design's reality check

`multi_agent_system.py` is a real CrewAI + LangChain-Gemini manager/sub-agent system. It is
vendored here **exactly as received and deliberately unrepaired.**

Its value is that it is broken in the ways real uploads are broken. Every design decision in
`docs/design/v2-review-and-v3-plan.md` is walked against this file, and any design that cannot
ingest it is not finished.

## What is wrong with it, and why each one matters

| Defect | Why the design cares |
|---|---|
| **It does not parse.** Truncated mid-method inside `_create_tasks` — a `SyntaxError`, not merely an incomplete program. | Ingestion must gate on parse *before* dependency install, and "uploaded code does not parse" needs its own guided recovery state. |
| **No runnable entrypoint** — nothing calls `Crew(...).kickoff()`. | Entrypoint declaration must be a first-class supported path, not a failure. This is argued to be the *modal* first run for CrewAI, not the exception. |
| **Zero instrumentation** — no callbacks, tracers, or handlers anywhere. | Trace capture cannot depend on the agent cooperating. |
| **Tools registered in a runtime dict** (`_initialize_tools`) rather than statically. | Static analysis sees a method returning a dict, not three tools. Discovery must score this low and confirm from execution instead. |
| **Tools hand-mocked** with `time.sleep(random.uniform(...))` and unseeded `random`. | Users already need a fixture/mock world; and unseeded randomness is a live source of flakiness. |
| **Model chosen via an implicit `GOOGLE_API_KEY` lookup.** | A source hash does not identify what actually ran — reproducibility must capture resolved runtime config. |
| **Its model string is invalid for its own SDK.** It passes `model="gemini/gemini-2.0-flash"` — a *LiteLLM* convention — to `langchain_google_genai`, which strips no `gemini/` prefix. The first call is expected to fail with a provider 4xx. | The smoke gate must classify this as **runtime misconfiguration**, not infrastructure failure, or the error gets blamed on the sandbox. |

## Do not fix this file

Repairing it in place would destroy its purpose. The product's hosted "try the example" fixture
will be a **separate, repaired, versioned variant** with the repair disclosed — this copy stays as
the adversarial case the design is tested against.

## Provenance and licensing

Supplied by the project owner as their own work. Third-party dependencies referenced by the file
(`crewai`, `langchain-google-genai`, `pydantic`) carry their own licenses and are not vendored here.
