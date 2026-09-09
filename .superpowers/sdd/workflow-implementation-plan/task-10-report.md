# T10 durable onboarding UI slice report

## Scope delivered

This partial T10 browser slice extends the committed authoring workspace without
starting an evaluation. It adds project-context entry, Git/ZIP source-import
queueing, server-backed knowledge-report presentation and finding confirmation,
durable session restoration, and a session-note activity panel. It retains the
metric, dataset, and synthetic-preview authoring surfaces.

## Touched files

- `web/index.html`
- `web/authoring.js`
- `web/styles.css`
- `tests/browser/authoring/authoring.spec.js`

## API boundaries represented

- Existing project IDs are accepted through `?project_id=` or the project
  context field; the UI does not invent or create project IDs.
- ZIP uploads use `/api/uploads`; Git/ZIP imports use the existing queued
  `/api/projects/{project_id}/imports` contract. The UI names the queued state
  and does not drain a worker or claim import completion.
- Knowledge reports load through `/api/projects/{project_id}/knowledge` and
  individual confirmations use `/knowledge/confirm`. The returned report
  revision is rendered as the source of truth.
- Session restoration uses `/api/sessions/{session_id}` and notes/metric
  proposals use the existing queued session-turn endpoint.
- The current API does not offer project creation/listing, free-text knowledge
  corrections, or browser-driven worker draining. Each remains a named recovery
  state rather than a simulated flow.

## TDD evidence

RED command:

```sh
npm run test:browser -- tests/browser/authoring/authoring.spec.js --grep 'missing project context'
```

Observed result: 1 failed because the `Source and knowledge` heading and missing-project recovery surface did not exist.

GREEN command:

```sh
npm run test:browser -- tests/browser/authoring/authoring.spec.js
```

Observed result: 6 passed, 0 failed (fresh run after the final diff).

Additional focused responsive RED/GREEN evidence:

```sh
npm run test:browser -- tests/browser/authoring/authoring.spec.js --grep 'narrow viewport'
```

The RED run found the source-import button clipped by the legacy builder
container. The GREEN run passed after the authoring workspace overrode that
fixed-height/hidden-overflow container behavior.

`git diff --check` completed with exit status 0 and no output before commit.

## Browser coverage

The six authoring tests cover the preserved review-first metric flow and local
draft reload, missing project context, session/report restore and confirmation,
keyboard activation of a queued Git import, and source-control reachability at
a 390×844 viewport. Server-shaped responses are injected before page load so
the tests exercise real DOM state and browser interaction without claiming a
local file origin is an API server.

## Visual inspection

Inspected Chromium screenshots at 1440×960 and 390×844. The desktop layout
shows authoring activity and source/report work side by side with readable
import controls. The mobile layout stacks them, exposes the full source form,
and allows the import control to scroll into view. The controls retain native
keyboard activation; status changes use polite live regions. The interface
keeps the existing teal/slate Traceline visual language and has reduced-motion,
reduced-transparency, and contrast fallback rules inherited/extended from the
authoring surface.

## Remaining gaps

This is a partial T10 slice. It does not implement project creation/listing,
worker job completion polling, free-text knowledge corrections, dataset mapping
review, canonical preview revisions, or the broader UX-01–UX-08 and UX-18–UX-21
acceptance suite. It intentionally does not start an evaluation.

## Commit

Implementation commit: `a0af3c4dae0a17fcc046b2717ca3855f27228a79` (`feat(web): add durable authoring onboarding states`).
