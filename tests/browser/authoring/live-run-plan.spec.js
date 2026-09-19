const { test, expect } = require("@playwright/test");

test.use({ baseURL: "http://127.0.0.1:8765" });

test("live authoring plans, authorizes, enqueues, and discovers a completed hosted run", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  const apiRequests = [];
  page.on("request", (request) => {
    if (!request.url().includes("/api/")) return;
    const contentType = request.headers()["content-type"] || "";
    let body = null;
    if (contentType.includes("application/json")) {
      try {
        body = request.postDataJSON();
      } catch (_) {
        // Response assertions below remain authoritative for the live contract.
      }
    }
    apiRequests.push({ method: request.method(), path: new URL(request.url()).pathname, body });
  });

  await page.goto("/");
  await expect(page.getByRole("button", { name: "Select Seeded live run agent" })).toBeVisible();
  const seededProjectId = await page.locator(".project-option", { hasText: "Seeded live run agent" }).getAttribute("data-project-id");
  expect(seededProjectId).toMatch(/^[0-9a-f]{32}$/);

  const evalId = "live-browser-eval";
  const evalResponse = await page.request.get(`/api/evals/${evalId}`);
  expect(evalResponse.ok()).toBeTruthy();
  const evalRecord = await evalResponse.json();
  const targetVersionId = evalRecord.dashboard?.target_version_id;
  expect(targetVersionId).toMatch(/^[0-9a-f]{32}$/);

  const evaluationVersionsResponse = await page.request.get(`/api/objects/evaluation/${evalId}/versions`);
  const evaluationVersions = await evaluationVersionsResponse.json();
  const evaluationVersionId = evaluationVersions.active_version_id;
  expect(evaluationVersionId).toMatch(/^[0-9a-f]{32}$/);

  const datasetVersionsResponse = await page.request.get(`/api/objects/dataset/${seededProjectId}/versions`);
  const datasetVersions = await datasetVersionsResponse.json();
  const datasetVersionId = datasetVersions.active_version_id;
  expect(datasetVersionId).toMatch(/^[0-9a-f]{32}$/);

  const dashboardVersionsResponse = await page.request.get(`/api/objects/dashboard/${evalId}/versions`);
  const dashboardVersions = await dashboardVersionsResponse.json();
  const dashboardVersionId = dashboardVersions.active_version_id;
  expect(dashboardVersionId).toMatch(/^[0-9a-f]{32}$/);

  const query = new URLSearchParams({
    project_id: seededProjectId,
    eval_id: evalId,
    evaluation_version_id: evaluationVersionId,
    dataset_version_id: datasetVersionId,
    target_version_id: targetVersionId,
    dashboard_version_id: dashboardVersionId,
  });
  await page.goto(`/?${query.toString()}`);
  await page.getByRole("tab", { name: "Preview" }).click();
  const planResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST" && new URL(response.url()).pathname === "/api/run-plans";
  });
  await page.getByRole("button", { name: "Review run manifest" }).click();

  const review = page.getByRole("region", { name: "Run manifest review" });
  const plan = await (await planResponse).json();
  expect(plan.state, JSON.stringify(plan)).toBe("validated");
  await expect(review).toContainText(evaluationVersionId);
  await expect(review).toContainText(datasetVersionId);
  await expect(review).toContainText(targetVersionId);
  await expect(review).toContainText(dashboardVersionId);

  await expect(review).toContainText("validated");
  expect(plan.plan.plan_id).toMatch(/^[0-9a-f]{32}$/);
  expect(plan.plan.blockers).toEqual([]);
  expect(plan.plan.content.version_refs).toEqual({
    project_id: seededProjectId,
    evaluation_version_id: evaluationVersionId,
    dataset_version_id: datasetVersionId,
    target_version_id: targetVersionId,
    dashboard_version_id: dashboardVersionId,
  });
  expect(plan.plan.content.version_refs).not.toHaveProperty("source_version_id");

  const authorizeResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/run-plans/${plan.plan.plan_id}/authorize`;
  });
  await page.getByRole("button", { name: "Authorize this manifest" }).click();
  const authorization = await (await authorizeResponse).json();
  expect(authorization.state).toBe("authorized");
  expect(authorization.authorization.state).toBe("authorized");
  expect(authorization.authorization.plan_hash).toBe(plan.plan.content_digest);
  await expect(review).toContainText("Authorization authorized");

  const enqueueResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST" && new URL(response.url()).pathname === "/api/runs";
  });
  await page.getByRole("button", { name: "Queue authorized run" }).click();
  const queued = await (await enqueueResponse).json();
  expect(queued.state).toBe("queued");
  expect(queued.job_id).toMatch(/^[0-9a-f]{32}$/);
  await expect(review).toContainText(`Run queued. Worker job ${queued.job_id}.`);

  await expect.poll(async () => {
    const response = await page.request.get(`/api/jobs/${queued.job_id}`);
    return (await response.json()).status;
  }, { timeout: 15_000, intervals: [100, 250, 500] }).toBe("completed");
  const completedResponse = await page.request.get(`/api/jobs/${queued.job_id}`);
  const completed = await completedResponse.json();
  expect(completed.status).toBe("completed");
  expect(completed.result.state).toBe("complete");
  expect(completed.result.plan_id).toBe(plan.plan.plan_id);
  expect(completed.result.run_id).toMatch(/^[0-9a-f]{32}$/);

  const runId = completed.result.run_id;
  const observedEvalResponse = await page.request.get(`/api/evals/${evalId}`);
  const observedEval = await observedEvalResponse.json();
  expect(observedEval.run_id).toBe(runId);
  expect(observedEval.pipeline.find((step) => step.id === "run").status).toBe("complete");
  expect(observedEval.pipeline.find((step) => step.id === "score").status).toBe("complete");

  const resultsResponse = await page.request.get(`/runs/${runId}`);
  expect(resultsResponse.ok()).toBeTruthy();
  const results = await resultsResponse.json();
  expect(results.run.run_id).toBe(runId);
  expect(results.run.status).toBe("complete");
  expect(results.terminal).toBe(true);
  expect(results.cases).toHaveLength(1);
  expect(results.cases[0].status).toBe("completed");
  expect(results.metrics).toEqual(expect.arrayContaining([
    expect.objectContaining({
      metric_id: "answer",
      aggregation_state: "COMPLETE",
      value: 1.0,
      gate_status: "PASS",
    }),
  ]));

  await page.getByRole("tab", { name: "Dashboards" }).click();
  const seededEvaluation = page.getByRole("button", { name: "Live hosted acceptance" });
  await expect(seededEvaluation).toBeVisible();
  await seededEvaluation.click();

  const persistedResults = page.getByRole("region", { name: "Persisted results" });
  await expect(persistedResults).toBeVisible();
  await expect(persistedResults).toContainText("authoritative");
  await expect(page.locator("#kpi-gate-val")).toHaveText("GATE PASS");

  const liveCase = page.locator(".case-card").filter({ hasText: "live-case" });
  await expect(liveCase).toBeVisible();
  await expect(liveCase).toContainText("PASS");
  await expect(page.locator("#inspector-content")).toContainText("live-case");
  await expect(page.locator("#inspector-content")).toContainText("PASS");

  const browserExportResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST" && new URL(response.url()).pathname === "/api/exports";
  });
  await page.getByRole("button", { name: "Create export snapshot" }).click();
  const browserExport = await (await browserExportResponse).json();
  expect(browserExport.state).toBe("ready");
  expect(browserExport.export_id).toMatch(/^[0-9a-f]{32}$/);
  const exportResult = page.locator("#export-result");
  await expect(exportResult).toContainText("Export snapshot returned · authoritative");
  await expect(exportResult.getByRole("link", { name: "Download JSON" }))
    .toHaveAttribute("href", `/api/exports/${browserExport.export_id}/json`);

  // A truthful comparison needs a second completed immutable run; mocked
  // compare coverage remains in tests/browser/legacy/results-actions.spec.js.

  const exportResponse = await page.request.post("/api/exports", { data: { run_id: runId } });
  expect(exportResponse.status()).toBe(201);
  const createdExport = await exportResponse.json();
  expect(createdExport.export_id).toMatch(/^[0-9a-f]{32}$/);
  const manifestResponse = await page.request.get(`/api/exports/${createdExport.export_id}/json`);
  expect(manifestResponse.ok()).toBeTruthy();
  const manifest = await manifestResponse.json();
  expect(manifest.run.run_id).toBe(runId);
  expect(manifest.run.status).toBe("complete");
  expect(manifest.case_metrics).toEqual(expect.arrayContaining([
    expect.objectContaining({ metric_id: "answer", score: 1.0 }),
  ]));
  expect((await page.request.get(`/api/exports/${createdExport.export_id}/csv`)).ok()).toBeTruthy();

  expect(apiRequests).toEqual(expect.arrayContaining([
    {
      method: "POST",
      path: "/api/run-plans",
      body: {
        version_refs: {
          project_id: seededProjectId,
          evaluation_version_id: evaluationVersionId,
          dataset_version_id: datasetVersionId,
          target_version_id: targetVersionId,
          dashboard_version_id: dashboardVersionId,
        },
        limits: {},
      },
    },
    {
      method: "POST",
      path: `/api/run-plans/${plan.plan.plan_id}/authorize`,
      body: { plan_hash: plan.plan.content_digest },
    },
    {
      method: "POST",
      path: "/api/runs",
      body: {
        plan_id: plan.plan.plan_id,
        plan_hash: plan.plan.content_digest,
        authorization_id: authorization.authorization.authorization_id,
      },
    },
  ]));
});
