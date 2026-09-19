const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const authoringPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;
const planHash = "a".repeat(64);

async function installScheduleMock(page, mode = "success") {
  await page.addInitScript((scenario) => {
    const mockPlanHash = "a".repeat(64);
    const mockScheduleRecord = (state = "active") => ({
      schedule_id: "schedule-1",
      workspace_id: "workspace-a",
      project_id: "project-a",
      plan_id: "plan-1",
      plan_hash: mockPlanHash,
      authorization_id: "authorization-1",
      timezone: "Asia/Kolkata",
      local_time: "09:30",
      state,
      dst_policy: "skip",
      version_policy: "frozen",
      daily_request_limit: 4,
      daily_budget_usd_micros: 12500000,
      created_at: "2026-09-19T12:00:00+00:00",
      updated_at: "2026-09-19T12:00:00+00:00",
    });
    let currentSchedule = null;
    window.__scheduleCalls = [];
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      window.__scheduleCalls.push({ path, method, body });
      const json = (value, status = 200) => new Response(JSON.stringify(value), {
        status,
        headers: { "content-type": "application/json" },
      });
      if (path === "/api/projects/project-a/knowledge") return json({
        report_id: "knowledge-v1", revision: 1, source_version_id: "source-v1",
        review_needed: false, facts: [], pending_questions: [],
      });
      if (path === "/api/objects/dashboard/eval-a/versions") return json({
        active_revision: 1,
        active_version_id: "dashboard-v1",
        versions: [{ version_id: "dashboard-v1", content: { definition: { name: "Overview", blocks: [] } } }],
      });
      if (path === "/api/run-plans" && method === "POST") {
        if (scenario === "blocked") return json({
          state: "blocked",
          plan: {
            plan_id: "plan-blocked",
            content_digest: "b".repeat(64),
            state: "blocked",
            blockers: ["target_version_required"],
            content: { version_refs: body.version_refs, limits: {} },
          },
        }, 201);
        return json({
          state: "validated",
          plan: {
            plan_id: "plan-1",
            content_digest: mockPlanHash,
            state: "validated",
            blockers: [],
            content: { version_refs: body.version_refs, limits: {} },
          },
        }, 201);
      }
      if (path === "/api/run-plans/plan-1/authorize" && method === "POST") return json({
        state: "authorized",
        authorization: { authorization_id: "authorization-1", state: "authorized", plan_hash: body.plan_hash },
      }, 201);
      if (path === "/api/schedules" && method === "GET") return json({ schedules: currentSchedule ? [currentSchedule] : [] });
      if (path === "/api/schedules/schedule-1" && method === "GET") return json({
        schedule: currentSchedule,
        slots: currentSchedule ? [
          { schedule_id: "schedule-1", slot_key: "slot-1", slot_at: "2026-09-20T09:30:00+05:30", state: "queued", owner: "worker-a", job_id: "job-1" },
          { schedule_id: "schedule-1", slot_key: "slot-2", slot_at: "2026-09-21T09:30:00+05:30", state: "completed", owner: "worker-a", job_id: "job-2" },
        ] : [],
      });
      if (path === "/api/schedules" && method === "POST") {
        if (scenario === "create-error") return json({ error: { code: "authorization_expired", message: "schedule authorization has expired" } }, 409);
        currentSchedule = { ...mockScheduleRecord(), ...body };
        return json({ state: "active", schedule: currentSchedule }, 201);
      }
      if (path === "/api/schedules/schedule-1/pause" && method === "POST") {
        currentSchedule = { ...currentSchedule, state: "paused" };
        return json({ state: "paused", schedule: currentSchedule });
      }
      if (path === "/api/schedules/schedule-1/resume" && method === "POST") {
        currentSchedule = { ...currentSchedule, state: "active" };
        return json({ state: "active", schedule: currentSchedule });
      }
      return json({ error: { code: "not_found", message: `unhandled ${path}` } }, 404);
    };
  }, mode);
}

async function openRunPlan(page, mode = "success") {
  await installScheduleMock(page, mode);
  await page.goto(`${authoringPage}?project_id=project-a&eval_id=eval-a&evaluation_version_id=eval-v1&dataset_version_id=dataset-v1&source_version_id=source-v1&target_version_id=target-v1&dashboard_version_id=dashboard-v1`);
  await page.getByRole("tab", { name: "Preview" }).click();
  await page.getByRole("button", { name: "Review run manifest" }).click();
}

async function authorizeRunPlan(page) {
  await page.getByRole("button", { name: "Authorize this manifest" }).click();
  await expect(page.getByText("Authorization authorized")).toBeVisible();
}

test("schedule creation is blocked before a validated plan is explicitly authorized", async ({ page }) => {
  await openRunPlan(page, "blocked");

  const schedule = page.getByRole("region", { name: "Schedule controls" });
  await expect(schedule).toContainText("Authorize a validated run plan");
  await expect(page.getByRole("button", { name: "Create schedule" })).toBeDisabled();
  await expect(page.getByLabel("Timezone")).toBeDisabled();

  const calls = await page.evaluate(() => window.__scheduleCalls);
  expect(calls.some((call) => call.path === "/api/schedules" && call.method === "POST")).toBe(false);
});

test("authorized Preview creates and lists a schedule with the exact run identity", async ({ page }) => {
  await openRunPlan(page);
  await authorizeRunPlan(page);

  const schedule = page.getByRole("region", { name: "Schedule controls" });
  await expect(page.getByRole("button", { name: "Create schedule" })).toBeEnabled();
  await page.getByLabel("Timezone").fill("Asia/Kolkata");
  await page.getByLabel(/Local time/).fill("09:30");
  await page.getByLabel("DST policy").selectOption("skip");
  await page.getByLabel("Daily request limit").fill("4");
  await page.getByLabel(/Daily USD budget/).fill("12.50");
  await page.getByRole("button", { name: "Create schedule" }).click();

  await expect(schedule).toContainText("Schedule active");
  await expect(schedule).toContainText("schedule-1");
  await expect(schedule).toContainText("2 slots");
  await expect(schedule).toContainText("2026-09-20T09:30:00+05:30");

  const calls = await page.evaluate(() => window.__scheduleCalls);
  const create = calls.find((call) => call.path === "/api/schedules" && call.method === "POST");
  expect(create.body).toEqual({
    plan_id: "plan-1",
    plan_hash: planHash,
    authorization_id: "authorization-1",
    timezone: "Asia/Kolkata",
    local_time: "09:30",
    dst_policy: "skip",
    daily_request_limit: 4,
    daily_budget_usd_micros: 12500000,
  });
  expect(calls.some((call) => call.path === "/api/schedules" && call.method === "GET")).toBe(true);
  expect(calls.some((call) => call.path === "/api/schedules/schedule-1" && call.method === "GET")).toBe(true);
});

test("existing schedule controls truthfully pause and resume the observed schedule", async ({ page }) => {
  await openRunPlan(page);
  await authorizeRunPlan(page);
  await page.getByLabel("Daily USD budget").fill("12.50");
  await page.getByRole("button", { name: "Create schedule" }).click();

  const schedule = page.getByRole("region", { name: "Schedule controls" });
  await page.getByRole("button", { name: "Pause schedule schedule-1" }).click();
  await expect(schedule).toContainText("State paused");
  await page.getByRole("button", { name: "Resume schedule schedule-1" }).click();
  await expect(schedule).toContainText("State active");

  const calls = await page.evaluate(() => window.__scheduleCalls);
  expect(calls.some((call) => call.path === "/api/schedules/schedule-1/pause" && call.method === "POST")).toBe(true);
  expect(calls.some((call) => call.path === "/api/schedules/schedule-1/resume" && call.method === "POST")).toBe(true);
});

test("schedule errors are rendered and schedule controls never expose secret values", async ({ page }) => {
    await openRunPlan(page, "create-error");
    await authorizeRunPlan(page);

    const schedule = page.getByRole("region", { name: "Schedule controls" });
    await expect(schedule).not.toContainText(/secret|token|api[_ -]?key/i);
    await page.getByLabel("Daily USD budget").fill("12.50");
    await page.getByRole("button", { name: "Create schedule" }).click();

  await expect(schedule).toContainText("Schedule was not created: schedule authorization has expired");
  await expect(schedule).not.toContainText("Schedule active");
  const calls = await page.evaluate(() => window.__scheduleCalls);
  const create = calls.find((call) => call.path === "/api/schedules" && call.method === "POST");
  expect(create.body).not.toHaveProperty("secret_ref");
  expect(create.body).not.toHaveProperty("token");
  expect(create.body).not.toHaveProperty("api_key");
});
