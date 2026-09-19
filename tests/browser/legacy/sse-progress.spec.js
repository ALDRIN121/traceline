const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const dashboardPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;

async function installSseMock(page) {
  await page.addInitScript(() => {
    window.__sseControl = { status: "running", terminal: false, detailVersion: 1 };
    window.__sseSources = [];
    window.__fetchCalls = [];

    class MockEventSource {
      constructor(url) {
        this.url = url;
        this.closed = false;
        this.listeners = {};
        window.__sseSources.push(this);
      }

      addEventListener(name, listener) {
        (this.listeners[name] ||= []).push(listener);
      }

      close() {
        this.closed = true;
      }

      dispatch(name, data = {}) {
        for (const listener of this.listeners[name] || []) {
          listener({ data: JSON.stringify(data) });
        }
      }

      triggerError() {
        if (this.onerror) this.onerror(new Event("error"));
      }

      triggerOpen() {
        if (this.onopen) this.onopen(new Event("open"));
      }
    }

    window.EventSource = MockEventSource;
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      window.__fetchCalls.push({ path, method });

      const json = (body, status = 200) => new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
      const liveRun = {
        run_id: "run-live",
        status: window.__sseControl.status,
        terminal: window.__sseControl.terminal,
        case_count: 1,
      };

      if (path === "/health") return json({ status: "ok" });
      if (path === "/api/evals") return json({ evals: [] });
      if (path.startsWith("/runs?")) return json({ runs: [liveRun], next_cursor: null });
      if (path === "/api/dashboards/default") {
        return json({
          name: "Live dashboard",
          version: 1,
          registry_version: 1,
          run: liveRun,
          metrics_complete: window.__sseControl.terminal,
          render_final: window.__sseControl.terminal,
          blocks: [],
        });
      }
      if (path === "/runs/run-live") {
        return json({
          run: liveRun,
          detail_version: window.__sseControl.detailVersion,
          cases: [{ case_id: "case-1", name: "Case 1", status: "running", metrics: [] }],
          metrics: [{
            metric_id: "accuracy",
            value: null,
            aggregation_state: "IN_PROGRESS",
            provisional: true,
            is_authoritative: false,
          }],
        });
      }
      return json({ error: { code: "not_found", message: `unhandled ${path}` } }, 404);
    };
  });
}

async function openLiveDashboard(page) {
  await installSseMock(page);
  await page.goto(dashboardPage);
  await expect.poll(() => page.evaluate(() => Boolean(window.__dashboard))).toBe(true);
  await page.evaluate(() => {
    window.__dashboard.state.view = "dashboard";
    return window.__dashboard.refreshAll();
  });
  await expect.poll(() => page.evaluate(() => window.__sseSources.length)).toBe(1);
  await expect.poll(() => page.evaluate(() => window.__dashboard.state.sseRunId)).toBe("run-live");
}

test("legacy dashboard handles the backend's dotted progress event names", async ({ page }) => {
  await openLiveDashboard(page);
  const eventNames = [
    "case.completed",
    "metric.updated",
    "run.state",
    "run.failed",
    "run.cancelled",
    "run.resumed",
  ];

  const registered = await page.evaluate(() => Object.keys(window.__sseSources[0].listeners));
  expect(registered).toEqual(expect.arrayContaining(eventNames));

  await page.evaluate((names) => {
    window.__sseControl.detailVersion = 2;
    for (const name of names) window.__sseSources[0].dispatch(name, { run_id: "run-live" });
  }, eventNames);

  await expect.poll(() => page.evaluate(() => window.__dashboard.state.runDetail.detail_version)).toBe(2);
  await expect.poll(() => page.evaluate(() => window.__fetchCalls.filter((call) => call.method === "POST").length)).toBe(0);
});

test("an SSE error reconciles authoritative state and leaves polling as the fallback", async ({ page }) => {
  await openLiveDashboard(page);

  await page.evaluate(() => {
    window.__sseControl.detailVersion = 2;
    window.__sseSources[0].triggerError();
  });

  await expect.poll(() => page.evaluate(() => window.__dashboard.state.runDetail.detail_version)).toBe(2);
  const afterError = await page.evaluate(() => ({
    sourceCount: window.__sseSources.length,
    sourceClosed: window.__sseSources[0].closed,
    polling: window.__dashboard.state.polling,
    liveText: document.querySelector("#live-text").textContent,
    postRequests: window.__fetchCalls.filter((call) => call.method === "POST").length,
  }));
  expect(afterError).toEqual({
    sourceCount: 1,
    sourceClosed: false,
    polling: true,
    liveText: "Reconnecting…",
    postRequests: 0,
  });

  await page.evaluate(() => window.__sseSources[0].triggerOpen());
  await expect(page.locator("#live-text")).toHaveText("Live Stream");
});
