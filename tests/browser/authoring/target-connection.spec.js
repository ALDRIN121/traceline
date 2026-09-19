const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const authoringPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;

async function installTargetMock(page, mode) {
  await page.addInitScript((scenario) => {
    let polls = 0;
    window.__targetCalls = [];
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      window.__targetCalls.push({ path, method, body });
      const json = (value, status = 200) => new Response(JSON.stringify(value), {
        status,
        headers: { "content-type": "application/json" },
      });
      if (path === "/api/sessions/session-a") return json({
        session_id: "session-a", project_id: "project-a", evaluation_id: "eval-a",
        evaluation_version_id: "eval-v1", dataset_version_id: "dataset-v1", revision: 3,
      });
      if (path === "/api/projects/project-a/knowledge") return json({
        report_id: "knowledge-v1", revision: 1, source_version_id: "source-v1",
        review_needed: false, facts: [], pending_questions: [],
      });
      if (path === "/api/objects/dashboard/eval-a/versions") return json({ versions: [], active_revision: 0, active_version_id: null });
      if (path === "/api/projects/project-a/connections" && method === "POST") {
        if (scenario === "server-invalid") return json({ error: { code: "secret_ref_required", message: "Bearer and API-key auth require an encrypted secret_ref" } }, 422);
        return json({ state: "configured", target_id: "target-1", version_id: "target-configured-v1", revision: 0 }, 201);
      }
      if (path === "/api/targets/target-1/verify" && method === "POST") return json({ state: "queued", job_id: "verify-job-1" }, 202);
      if (path === "/api/jobs/verify-job-1") {
        polls += 1;
        if (scenario === "blocked") return json({
          job_id: "verify-job-1", status: "failed",
          error: { code: "target_verification_failed", message: "Target verification failed: endpoint returned an invalid response." },
        });
        return polls < 2 ? json({ job_id: "verify-job-1", status: "queued" }) : json({
          job_id: "verify-job-1", status: "completed",
          result: { state: "verified", target_id: "target-1", target_version_id: "target-verified-v2", capabilities: { final_output: "observed" } },
        });
      }
      return json({ error: { code: "not_found", message: `unhandled ${path}` } }, 404);
    };
  }, mode);
}

test("hosted target configures, verifies through a durable job, and persists the verified ref", async ({ page }) => {
  await installTargetMock(page, "success");
  await page.goto(`${authoringPage}?session_id=session-a&project_id=project-a`);
  await page.getByLabel("Target URL").fill("https://agent.example.test/respond");
  await page.getByLabel("Authentication").selectOption("bearer");
  await page.getByLabel("Secret reference").fill("secret-ref-1");
  await page.getByRole("button", { name: "Configure target" }).click();
  await expect(page.locator("#target-connection-status")).toContainText("Target configured");

  await page.getByRole("button", { name: "Verify target" }).click();
  await expect(page.locator("#target-connection-status")).toContainText("Target verified", { timeout: 5_000 });
  await expect.poll(() => page.evaluate(() => window.__tracelineAuthoringSession.target_version_id)).toBe("target-verified-v2");
  await expect.poll(() => new URL(page.url()).searchParams.get("target_version_id")).toBe("target-verified-v2");

  const calls = await page.evaluate(() => window.__targetCalls);
  const configured = calls.find((call) => call.path === "/api/projects/project-a/connections");
  const verified = calls.find((call) => call.path === "/api/targets/target-1/verify");
  expect(configured.body.auth).toEqual({ type: "bearer", secret_ref: "secret-ref-1" });
  expect(configured.body).not.toHaveProperty("token");
  expect(verified.body).toEqual({
    target_version_id: "target-configured-v1",
    smoke_input: { message: "Traceline verification request" },
  });
});

test("hosted target blocks missing secret refs and surfaces failed verification", async ({ page }) => {
  await installTargetMock(page, "blocked");
  await page.goto(`${authoringPage}?project_id=project-a`);
  await page.getByLabel("Target URL").fill("https://agent.example.test/respond");
  await page.getByLabel("Authentication").selectOption("api_key");
  await page.getByRole("button", { name: "Configure target" }).click();
  await expect(page.locator("#target-connection-errors")).toContainText("secret_ref is required");
  await expect(page.locator("#target-connection-status")).toContainText("blocked");
  await expect.poll(() => page.evaluate(() => window.__targetCalls.filter((call) => call.path.includes("/connections")).length)).toBe(0);

  await page.getByLabel("Secret reference").fill("secret-ref-1");
  await page.getByRole("button", { name: "Configure target" }).click();
  await page.getByRole("button", { name: "Verify target" }).click();
  await expect(page.locator("#target-connection-status")).toContainText("Target verification failed", { timeout: 5_000 });
  await expect(page.locator("#target-connection-status")).toContainText("invalid response");
  await expect.poll(() => page.evaluate(() => window.__tracelineAuthoringSession?.target_version_id || null)).toBe(null);
});
