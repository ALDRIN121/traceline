const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const authoringPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;

async function installRunPlanMock(page, mode) {
  await page.addInitScript((scenario) => {
    window.__workflowCalls = [];
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      window.__workflowCalls.push({
        path,
        method,
        body,
        idempotencyKey: new Headers(init.headers || {}).get("Idempotency-Key"),
      });
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
            blockers: ["target_version_required", "target_verification_required"],
            content: { version_refs: body.version_refs, limits: {} },
          },
        }, 201);
        return json({
          state: "validated",
          plan: {
            plan_id: "plan-1",
            content_digest: "a".repeat(64),
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
      if (path === "/api/runs" && method === "POST") return json({
        state: "queued", job_id: "job-1", plan_id: body.plan_id,
      }, 202);
      return json({ error: { code: "not_found", message: `unhandled ${path}` } }, 404);
    };
  }, mode);
}

test("Preview runs the durable plan, authorization, and enqueue sequence", async ({ page }) => {
  await installRunPlanMock(page, "success");
  await page.goto(`${authoringPage}?project_id=project-a&eval_id=eval-a&evaluation_version_id=eval-v1&dataset_version_id=dataset-v1&source_version_id=source-v1&target_version_id=target-v1&dashboard_version_id=dashboard-v1`);
  await page.getByRole("tab", { name: "Preview" }).click();
  await page.getByRole("button", { name: "Review run manifest" }).click();

  const review = page.getByRole("region", { name: "Run manifest review" });
  await expect(review).toContainText("eval-v1");
  await expect(review).toContainText("dataset-v1");
  await expect(review).toContainText("target-v1");
  await expect(review).toContainText("dashboard-v1");
  await expect(review).toContainText("validated");

  await page.getByRole("button", { name: "Authorize this manifest" }).click();
  await expect(review).toContainText("Authorization authorized");
  await page.getByRole("button", { name: "Queue authorized run" }).click();
  await expect(review).toContainText("Run queued. Worker job job-1.");

  const calls = await page.evaluate(() => window.__workflowCalls);
  const plan = calls.find((call) => call.path === "/api/run-plans" && call.method === "POST");
  const authorize = calls.find((call) => call.path === "/api/run-plans/plan-1/authorize");
  const enqueue = calls.find((call) => call.path === "/api/runs");
  expect(plan.body.version_refs).toEqual({
    project_id: "project-a",
    evaluation_version_id: "eval-v1",
    dataset_version_id: "dataset-v1",
    target_version_id: "target-v1",
    dashboard_version_id: "dashboard-v1",
  });
  expect(plan.body.version_refs).not.toHaveProperty("source_version_id");
  expect(authorize.body).toEqual({ plan_hash: "a".repeat(64) });
  expect(enqueue.body).toEqual({ plan_id: "plan-1", plan_hash: "a".repeat(64), authorization_id: "authorization-1" });
  expect(enqueue.idempotencyKey).toMatch(/^authoring-/);
  expect(calls.some((call) => call.path.startsWith("/api/evals/") && call.method === "POST")).toBe(false);
});

test("Preview shows precise target blockers and does not authorize a blocked plan", async ({ page }) => {
  await installRunPlanMock(page, "blocked");
  await page.goto(`${authoringPage}?project_id=project-a&eval_id=eval-a&evaluation_version_id=eval-v1&dataset_version_id=dataset-v1`);
  await page.getByRole("tab", { name: "Preview" }).click();
  await page.getByRole("button", { name: "Review run manifest" }).click();

  const review = page.getByRole("region", { name: "Run manifest review" });
  await expect(review).toContainText("target_version_required");
  await expect(review).toContainText("target_verification_required");
  await expect(page.getByRole("button", { name: "Authorize this manifest" })).toBeHidden();
  const calls = await page.evaluate(() => window.__workflowCalls);
  expect(calls.some((call) => call.path.includes("/authorize"))).toBe(false);
  expect(calls.some((call) => call.path === "/api/runs")).toBe(false);
});
