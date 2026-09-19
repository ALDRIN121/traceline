const { test, expect } = require("@playwright/test");

test.use({ baseURL: "http://127.0.0.1:8765" });

test("live authoring creates, lists, pauses, and resumes a schedule for the exact authorized plan", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  const apiRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith("/api/")) return;
    let body = null;
    try {
      body = request.postDataJSON();
    } catch (_) {
      // Schedule lifecycle assertions below use authoritative live responses.
    }
    apiRequests.push({ method: request.method(), path: url.pathname, body });
  });

  await page.goto("/");
  await expect(page.getByRole("button", { name: "Select Seeded live run agent" })).toBeVisible();
  const projectId = await page.locator(".project-option", { hasText: "Seeded live run agent" }).getAttribute("data-project-id");
  expect(projectId).toMatch(/^[0-9a-f]{32}$/);

  const evalId = "live-browser-schedule-eval";
  const evalResponse = await page.request.get(`/api/evals/${evalId}`);
  expect(evalResponse.ok()).toBeTruthy();
  const evaluation = await evalResponse.json();
  const targetVersionId = evaluation.dashboard?.target_version_id;
  expect(targetVersionId).toMatch(/^[0-9a-f]{32}$/);

  const evaluationVersions = await (await page.request.get(`/api/objects/evaluation/${evalId}/versions`)).json();
  const evaluationVersionId = evaluationVersions.active_version_id;
  const datasetVersions = await (await page.request.get(`/api/objects/dataset/${projectId}/versions`)).json();
  const datasetVersionId = datasetVersions.active_version_id;
  const dashboardVersions = await (await page.request.get(`/api/objects/dashboard/${evalId}/versions`)).json();
  const dashboardVersionId = dashboardVersions.active_version_id;
  for (const versionId of [evaluationVersionId, datasetVersionId, dashboardVersionId]) {
    expect(versionId).toMatch(/^[0-9a-f]{32}$/);
  }

  const query = new URLSearchParams({
    project_id: projectId,
    eval_id: evalId,
    evaluation_version_id: evaluationVersionId,
    dataset_version_id: datasetVersionId,
    target_version_id: targetVersionId,
    dashboard_version_id: dashboardVersionId,
  });
  await page.goto(`/?${query.toString()}`);
  await page.getByRole("tab", { name: "Preview" }).click();
  await expect(page.locator("#schedule-list-status")).toHaveText("No schedules returned by the service yet.");

  const planResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST" && new URL(response.url()).pathname === "/api/run-plans";
  });
  await page.getByRole("button", { name: "Review run manifest" }).click();
  const plan = await (await planResponse).json();
  expect(plan.state).toBe("validated");
  expect(plan.plan.blockers).toEqual([]);
  expect(plan.plan.content.version_refs).toEqual({
    project_id: projectId,
    evaluation_version_id: evaluationVersionId,
    dataset_version_id: datasetVersionId,
    target_version_id: targetVersionId,
    dashboard_version_id: dashboardVersionId,
  });
  expect(plan.plan.content.version_refs).not.toHaveProperty("source_version_id");
  expect(plan.plan.content_digest).toMatch(/^[0-9a-f]{64}$/);

  const authorizeResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/run-plans/${plan.plan.plan_id}/authorize`;
  });
  await page.getByRole("button", { name: "Authorize this manifest" }).click();
  const authorization = await (await authorizeResponse).json();
  expect(authorization.state).toBe("authorized");
  expect(authorization.authorization.state).toBe("authorized");
  expect(authorization.authorization.plan_id).toBe(plan.plan.plan_id);
  expect(authorization.authorization.plan_hash).toBe(plan.plan.content_digest);
  await expect(page.getByText("Authorization authorized")).toBeVisible();

  await page.getByLabel("Timezone").fill("Asia/Kolkata");
  await page.getByLabel("Local time (HH:MM)").fill("09:30");
  await page.getByLabel("DST policy").selectOption("skip");
  await page.getByLabel("Daily request limit").fill("4");
  await page.getByLabel("Daily USD budget").fill("12.50");

  const createResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST" && new URL(response.url()).pathname === "/api/schedules";
  });
  await page.getByRole("button", { name: "Create schedule" }).click();
  const createdResponse = await createResponse;
  expect(createdResponse.status()).toBe(201);
  const created = await createdResponse.json();
  expect(created.state).toBe("active");
  expect(created.schedule).toMatchObject({
    project_id: projectId,
    plan_id: plan.plan.plan_id,
    plan_hash: plan.plan.content_digest,
    authorization_id: authorization.authorization.authorization_id,
    timezone: "Asia/Kolkata",
    local_time: "09:30",
    dst_policy: "skip",
    version_policy: "frozen",
    daily_request_limit: 4,
    daily_budget_usd_micros: 12_500_000,
    state: "active",
  });
  const scheduleId = created.schedule.schedule_id;
  expect(scheduleId).toMatch(/^[0-9a-f]{32}$/);

  const scheduleItem = () => page.locator(`.schedule-item[data-schedule-id="${scheduleId}"]`);
  await expect(page.locator("#schedule-list-status")).toHaveText("1 schedule returned by the service.");
  await expect(scheduleItem()).toContainText("State active");
  await expect(scheduleItem()).toContainText("0 slots");
  await expect(scheduleItem().getByRole("button", { name: `Pause schedule ${scheduleId}` })).toBeVisible();

  const listed = await (await page.request.get("/api/schedules")).json();
  expect(listed.schedules).toEqual([
    expect.objectContaining({
      schedule_id: scheduleId,
      plan_id: plan.plan.plan_id,
      plan_hash: plan.plan.content_digest,
      authorization_id: authorization.authorization.authorization_id,
      state: "active",
    }),
  ]);
  const initialDetail = await (await page.request.get(`/api/schedules/${scheduleId}`)).json();
  expect(initialDetail.schedule).toMatchObject({
    schedule_id: scheduleId,
    plan_hash: plan.plan.content_digest,
    state: "active",
  });
  expect(initialDetail.slots).toEqual([]);

  const pauseResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/schedules/${scheduleId}/pause`;
  });
  await page.getByRole("button", { name: `Pause schedule ${scheduleId}` }).click();
  const paused = await (await pauseResponse).json();
  expect(paused.state).toBe("paused");
  expect(paused.schedule.plan_hash).toBe(plan.plan.content_digest);
  await expect(scheduleItem()).toContainText("State paused");
  await expect(scheduleItem().getByRole("button", { name: `Resume schedule ${scheduleId}` })).toBeVisible();
  const pausedDetail = await (await page.request.get(`/api/schedules/${scheduleId}`)).json();
  expect(pausedDetail.schedule.state).toBe("paused");
  expect(pausedDetail.schedule.plan_hash).toBe(plan.plan.content_digest);

  const resumeResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/schedules/${scheduleId}/resume`;
  });
  await page.getByRole("button", { name: `Resume schedule ${scheduleId}` }).click();
  const resumed = await (await resumeResponse).json();
  expect(resumed.state).toBe("active");
  expect(resumed.schedule.plan_hash).toBe(plan.plan.content_digest);
  await expect(scheduleItem()).toContainText("State active");
  await expect(scheduleItem().getByRole("button", { name: `Pause schedule ${scheduleId}` })).toBeVisible();

  const resumedDetail = await (await page.request.get(`/api/schedules/${scheduleId}`)).json();
  expect(resumedDetail.schedule.state).toBe("active");
  expect(resumedDetail.schedule.plan_id).toBe(plan.plan.plan_id);
  expect(resumedDetail.schedule.plan_hash).toBe(plan.plan.content_digest);

  expect(apiRequests).toEqual(expect.arrayContaining([
    {
      method: "POST",
      path: "/api/run-plans",
      body: {
        version_refs: {
          project_id: projectId,
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
      path: "/api/schedules",
      body: {
        plan_id: plan.plan.plan_id,
        plan_hash: plan.plan.content_digest,
        authorization_id: authorization.authorization.authorization_id,
        timezone: "Asia/Kolkata",
        local_time: "09:30",
        dst_policy: "skip",
        daily_request_limit: 4,
        daily_budget_usd_micros: 12_500_000,
      },
    },
    { method: "POST", path: `/api/schedules/${scheduleId}/pause`, body: null },
    { method: "POST", path: `/api/schedules/${scheduleId}/resume`, body: null },
  ]));
});
