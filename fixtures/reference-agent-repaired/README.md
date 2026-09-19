# Repaired reference agent

This directory is the disclosed, versioned runnable counterpart to
`fixtures/reference-agent/`. The original fixture remains unchanged and
deliberately broken so ingestion and readiness checks continue to exercise
real failures.

The repaired copy keeps the same CrewAI manager/sub-agent shape and runtime
tool registry, but it makes the minimum repairs needed for a controlled
demonstration:

- the source parses and exposes an explicit `kickoff` entrypoint;
- mock tools use a seeded local generator instead of process-global randomness;
- the Gemini model name uses the `langchain-google-genai` form;
- the API key is supplied explicitly to the constructor rather than being
  resolved implicitly by the fixture; and
- the entrypoint requires an explicit query and calls `Crew.kickoff()`.

The fixture still uses real CrewAI and Gemini imports. It is source material
for the engine's reviewed runtime image, not a bundled dependency or provider
credential. No key is stored in this repository.
