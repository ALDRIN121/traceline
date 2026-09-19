const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const dashboardPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;

async function installResultsMock(page, mode) {
  await page.addInitScript((scenario) => {
    window.__resultsCalls = [];
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      window.__resultsCalls.push({ path, method, body });
      const json = (value, status = 200) => new Response(JSON.stringify(value), {
        status,
        headers: { "content-type": "application/json" },
      });
      const run = {
        run_id: "run-candidate",
        status: scenario === "incomplete" ? "running" : "complete",
        terminal: scenario !== "incomplete",
        case_count: 1,
      };
      if (path === "/health") return json({ status: "ok" });
      if (path === "/api/evals") return json({ evals: [{ eval_id: "eval-1", name: "Results eval", run_id: "run-candidate" }] });
      if (path === "/api/evals/eval-1") return json({
        eval_id: "eval-1", name: "Results eval", run_id: "run-candidate",
        spec: { cases: [{ case_id: "case-1" }] }, dataset: { version_id: "dataset-v1" },
        dashboard: { version_id: "dashboard-v1", blocks: [] },
      });
      if (path.startsWith("/runs?")) return json({
        runs: [
          { run_id: "run-baseline", status: "complete" },
          { run_id: "run-candidate", status: run.status },
        ], next_cursor: null,
      });
      if (path === "/api/dashboards/default?run_id=run-candidate") return json({
        name: "Results dashboard", version: 1, registry_version: 1, run,
        metrics_complete: scenario !== "incomplete", render_final: scenario !== "incomplete",
        blocks: [],
      });
      if (path === "/runs/run-candidate") return json({
        run,
        cases: [{ case_id: "case-1", name: "Case 1", status: scenario === "incomplete" ? "running" : "completed", metrics: [] }],
        metrics: [{
          metric_id: "accuracy", value: scenario === "incomplete" ? null : 1,
          aggregation_state: scenario === "incomplete" ? "IN_PROGRESS" : "COMPLETE",
          provisional: scenario === "incomplete",
          is_authoritative: scenario !== "incomplete",
        }],
      });
      if (path === "/api/comparisons" && method === "POST") {
        if (scenario === "incomplete") return json({ state: "incomparable", reason: "runs_must_be_complete" });
        return json({
          state: "comparable", baseline_run_id: body.baseline_run_id, candidate_run_id: body.candidate_run_id,
          metric_id: body.metric_id, delta: 0.25, confidence_interval: null, no_ci: true,
        });
      }
      if (path === "/api/exports" && method === "POST") {
        if (scenario === "incomplete") return json({ error: { code: "export_not_ready", message: "Export requires a terminal run." } }, 409);
        return json({
          state: "frozen", export_id: "export-1",
          manifest: { run: { status: "complete" }, metrics: [{ metric_id: "accuracy", provisional: false }] },
          formats: { bundle: "bundle-hash" },
        }, 201);
      }
      return json({ error: { code: "not_found", message: `unhandled ${path}` } }, 404);
    };
  }, mode);
}

async function openResults(page, mode) {
  await installResultsMock(page, mode);
  await page.goto(dashboardPage);
  await page.getByRole("tab", { name: "Dashboards" }).click();
  await page.getByRole("button", { name: /Results eval/ }).click();
  await expect(page.getByRole("region", { name: "Persisted results" })).toBeVisible();
}

test("persisted results compare and export through authenticated routes", async ({ page }) => {
  await openResults(page, "complete");
  const results = page.getByRole("region", { name: "Persisted results" });
  await expect(results).toContainText("authoritative");
  await page.locator("#comparison-baseline").selectOption("run-baseline");
  await page.locator("#comparison-candidate").selectOption("run-candidate");
  await page.getByRole("button", { name: "Compare persisted runs" }).click();
  await expect(page.locator("#comparison-result")).toContainText("comparable");

  await page.getByRole("button", { name: "Create export snapshot" }).click();
  await expect(page.locator("#export-result")).toContainText("authoritative");
  await expect(page.getByRole("link", { name: "Download bundle" })).toHaveAttribute("href", "/api/exports/export-1/bundle");
  const calls = await page.evaluate(() => window.__resultsCalls);
  expect(calls.find((call) => call.path === "/api/comparisons").body).toEqual({
    baseline_run_id: "run-baseline", candidate_run_id: "run-candidate", metric_id: "accuracy",
  });
  expect(calls.find((call) => call.path === "/api/exports").body).toEqual({ run_id: "run-candidate" });
});

test("incomplete results show truthful states and block comparison/export", async ({ page }) => {
  await openResults(page, "incomplete");
  const results = page.getByRole("region", { name: "Persisted results" });
  await expect(results).toContainText("partial");
  await expect(results).toContainText("provisional");
  await page.locator("#comparison-baseline").selectOption("run-baseline");
  await page.locator("#comparison-candidate").selectOption("run-candidate");
  await page.getByRole("button", { name: "Compare persisted runs" }).click();
  await expect(page.locator("#comparison-result")).toContainText("Both runs must be complete");
  await page.getByRole("button", { name: "Create export snapshot" }).click();
  await expect(page.locator("#export-result")).toContainText("Export unavailable: Export requires a terminal run.");
});
