const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const dashboardPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;

test("reviewer can submit an additive score override for the selected case", async ({ page }) => {
  await page.addInitScript(() => {
    let revisionApplied = false;
    window.__revisionCalls = [];

    const run = {
      run_id: "run-review",
      status: "complete",
      terminal: true,
      case_count: 1,
    };

    const detail = () => ({
      run: { ...run, spec: { metrics: [{ metric_id: "accuracy", name: "Accuracy" }] } },
      terminal: true,
      cases: [{
        case_id: "case-refund",
        name: "Refund eligibility",
        status: "completed",
        attempts: [],
        metrics: [{
          metric_id: "accuracy",
          metric_name: "Accuracy",
          status: revisionApplied ? "PASS" : "FAIL",
          score: revisionApplied ? 1 : 0,
          score_revision: revisionApplied ? 2 : 1,
          overridden: revisionApplied,
          is_authoritative: true,
          evidence_event_ids: [],
        }],
      }],
      metrics: [{
        metric_id: "accuracy",
        value: revisionApplied ? 1 : 0,
        aggregation_state: "COMPLETE",
        provisional: false,
        is_authoritative: true,
      }],
    });

    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      const headers = init.headers || {};
      window.__revisionCalls.push({ path, method, body, headers });

      const json = (value, status = 200) => new Response(JSON.stringify(value), {
        status,
        headers: { "content-type": "application/json" },
      });

      if (path === "/health") return json({ status: "ok" });
      if (path === "/api/evals") return json({ evals: [] });
      if (path === "/runs?limit=500") return json({ runs: [run], next_cursor: null });
      if (path === "/api/dashboards/default") {
        return json({
          name: "Review dashboard",
          version: 1,
          registry_version: 1,
          run,
          metrics_complete: true,
          render_final: true,
          blocks: [],
        });
      }
      if (path === "/runs/run-review") return json(detail());
      if (path === "/runs/run-review/traces/case-refund") {
        return json({ run_id: "run-review", case_id: "case-refund", event_count: 0, events: [] });
      }
      if (path === "/runs/run-review/metrics/accuracy/revisions" && method === "POST") {
        revisionApplied = true;
        return json({
          state: "applied",
          revision: {
            score_revision: 2,
            case_id: "case-refund",
            metric_id: "accuracy",
            override_value: 1,
            dispute_path: body.dispute_path,
            reason: body.reason,
            author_id: body.author_id,
          },
          machine_score: { score_revision: 2, status: "PASS", score: 1, overridden: true },
        }, 201);
      }
      return json({ error: { code: "not_found", message: `unhandled ${path}` } }, 404);
    };
  });

  await page.goto(dashboardPage);
  await page.getByRole("tab", { name: "Dashboards" }).click();
  await page.locator("#refresh-btn").click();

  const caseCard = page.locator('[data-case-id="case-refund"]');
  await expect(caseCard).toBeVisible();
  await caseCard.click();
  await page.getByRole("button", { name: "Dispute / Override" }).first().click();

  const dialog = page.getByRole("dialog", { name: "Dispute or Override Metric Score" });
  await expect(dialog).toBeVisible();
  await dialog.locator("#dispute-override-value").selectOption("1.0");
  await dialog.locator("#dispute-path-select").selectOption("evidence_missing");
  await dialog.locator("#dispute-author").fill("reviewer@example.test");
  await dialog.locator("#dispute-reason").fill("The trace omitted the approval event.");
  await dialog.getByRole("button", { name: "Save Override" }).click();

  await expect(dialog).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.__revisionCalls.filter((call) => (
    call.path === "/runs/run-review/metrics/accuracy/revisions" && call.method === "POST"
  )).length)).toBe(1);

  const post = await page.evaluate(() => window.__revisionCalls.find((call) => (
    call.path === "/runs/run-review/metrics/accuracy/revisions" && call.method === "POST"
  )));
  expect(post.headers).toMatchObject({
    Accept: "application/json",
    "Content-Type": "application/json",
  });
  expect(post.body).toEqual({
    case_id: "case-refund",
    score_revision: 2,
    override_value: 1,
    dispute_path: "evidence_missing",
    reason: "The trace omitted the approval event.",
    author_id: "reviewer@example.test",
  });

  await page.getByRole("tab", { name: "Metrics & Assertions" }).click();
  const assertions = page.locator("#assertions-container");
  await expect(assertions).toContainText("rev 2 · overridden");
  await expect(assertions).toContainText("Score: 1");
  await expect.poll(() => page.evaluate(() => window.__dashboard.state.runDetail.cases[0].metrics[0].overridden)).toBe(true);
});
