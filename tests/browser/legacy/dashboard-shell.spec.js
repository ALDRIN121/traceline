const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const dashboardPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;

test("legacy dashboard uses keyboard activation and reviews a run before sending it", async ({ page }) => {
  await page.addInitScript(() => {
    let runId = null;
    window.__runRequests = [];
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const json = (body, status = 200) => new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
      if (path === "/health") return json({ status: "ok" });
      if (path === "/api/evals") return json({ evals: [{ eval_id: "eval-1", name: "Support eval", run_id: runId }] });
      if (path === "/api/evals/eval-1") return json({
        eval_id: "eval-1",
        name: "Support eval",
        run_id: runId,
        spec: { spec_version: "spec-v1", cases: [{ case_id: "case-1" }] },
        dataset: { version_id: "dataset-v1" },
        dashboard: { version_id: "dashboard-v1", blocks: [] },
      });
      if (path.startsWith("/api/dashboards/default")) return json({ name: "Support dashboard", blocks: [], run: null });
      if (path.startsWith("/runs?")) return json({ runs: [], next_cursor: null });
      if (path === "/api/evals/eval-1/run") {
        window.__runRequests.push(init.method || "GET");
        runId = "run-1";
        return json({ eval_id: "eval-1", run_id: runId, status: "queued" });
      }
      return json({ error: { code: "not_found", message: `unhandled ${path}` } }, 404);
    };
  });

  await page.goto(dashboardPage);
  await page.getByRole("tab", { name: "Dashboards" }).click();
  const evalItem = page.getByRole("button", { name: /Support eval/ });
  await expect(evalItem).toBeVisible();
  await evalItem.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => page.evaluate(() => window.__dashboard.state.selectedEval?.eval_id)).toBe("eval-1");

  // The legacy preview rail is normally opened by the authoring response. Reveal it
  // here to exercise the existing run control without adding a second product flow.
  await page.getByRole("tab", { name: "Eval Builder" }).click();
  await page.locator("#view-builder").evaluate((view) => view.classList.add("is-artifact-open"));
  await page.locator("#artifact-rail").evaluate((rail) => {
    rail.hidden = false;
    rail.setAttribute("aria-hidden", "false");
  });
  await page.locator("#artifact-panel").evaluate((panel) => {
    panel.style.transform = "none";
    panel.style.opacity = "1";
    panel.style.pointerEvents = "auto";
  });
  await page.locator("#btn-run-this-eval").dispatchEvent("click");

  const review = page.getByRole("dialog", { name: "Review before sending run request" });
  await expect(review).toContainText("Support eval");
  await expect(review).toContainText("spec-v1");
  await expect(review).toContainText("dataset-v1");
  await expect(review).toContainText("dashboard-v1");
  await expect(review).toContainText("separate from backend authorization");
  await expect.poll(() => page.evaluate(() => window.__runRequests.length)).toBe(0);

  const confirm = page.getByRole("button", { name: "Confirm review & send run request" });
  await confirm.focus();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  await expect.poll(() => page.evaluate(() => window.__runRequests.length)).toBe(1);
});

test("legacy case rows activate by click, Enter, and Space", async ({ page }) => {
  await page.goto(dashboardPage);
  await expect.poll(() => page.evaluate(() => Boolean(window.__dashboard))).toBe(true);
  await page.evaluate(() => {
    const dashboard = window.__dashboard;
    dashboard.state.definition = { blocks: [] };
    dashboard.state.view = "dashboard";
    dashboard.state.selection.case = null;
    document.querySelector("#view-dashboard").hidden = false;
    const grid = document.querySelector("#grid");
    grid.hidden = false;
    grid.replaceChildren(dashboard.renderers.case_table({
      cases: [{ case_id: "case-1", name: "Refund case", selection: { value: "case-1" }, status: "completed" }],
    }));
  });

  const row = page.locator("tr.case-row");
  await row.dispatchEvent("click");
  await expect.poll(() => page.evaluate(() => window.__dashboard.state.selection.case)).toBe("case-1");

  await page.evaluate(() => { window.__dashboard.state.selection.case = null; });
  await row.dispatchEvent("keydown", { key: "Enter" });
  await expect.poll(() => page.evaluate(() => window.__dashboard.state.selection.case)).toBe("case-1");

  await page.evaluate(() => { window.__dashboard.state.selection.case = null; });
  await row.dispatchEvent("keydown", { key: " " });
  await expect.poll(() => page.evaluate(() => window.__dashboard.state.selection.case)).toBe("case-1");
});
