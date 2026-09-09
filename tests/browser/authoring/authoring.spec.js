const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const authoringPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;
const legacyFileOriginApiFailure = /^Fetch API cannot load file:\/\/(?:\/health|\/api\/evals|\/runs\?limit=500)\. URL scheme "file" is not supported\.$/;

function expectOnlyKnownFileOriginErrors(errors) {
  expect(errors.every((message) => legacyFileOriginApiFailure.test(message))).toBe(true);
}

test("authoring workspace exposes a review-first metric flow", async ({ page }) => {
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(authoringPage);

  await expect(page.getByRole("heading", { name: "Evaluation workspace" })).toBeVisible();
  await expect(page.getByText("This page never starts an evaluation.")).toBeVisible();

  await page.getByRole("tab", { name: "Metrics" }).click();
  await page.getByRole("checkbox", { name: /Response is valid/i }).check();
  await page.getByRole("button", { name: "Review selected metrics" }).click();

  await expect(page.getByText("Choose both outcomes before saving.")).toBeVisible();
  await page.getByLabel("When evidence is missing").selectOption("fail");
  await page.getByLabel("When the evaluator has an error").selectOption("error");
  await expect(page.getByRole("button", { name: "Save metric decisions" })).toBeEnabled();
  expectOnlyKnownFileOriginErrors(errors);
});

test("authoring workspace retains a draft locally when the page reloads", async ({ page }) => {
  const errors = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(authoringPage);
  await page.getByRole("tab", { name: "Metrics" }).click();
  await page.getByRole("checkbox", { name: /Response is valid/i }).check();
  await page.reload();
  await page.getByRole("tab", { name: "Metrics" }).click();
  await expect(page.getByRole("checkbox", { name: /Response is valid/i })).toBeChecked();
  expectOnlyKnownFileOriginErrors(errors);
});

test("authoring workspace names the missing project context before source import", async ({ page }) => {
  await page.goto(authoringPage);

  await expect(page.getByRole("heading", { name: "Source and knowledge" })).toBeVisible();
  await expect(page.getByText("Project context required")).toBeVisible();
  await expect(page.getByRole("button", { name: "Load project report" })).toBeDisabled();
  await expect(page.getByText("This page never starts an evaluation.")).toBeVisible();
});

test("authoring workspace restores a server report and confirms a finding", async ({ page }) => {
  const report = {
    report_id: "knowledge-v1",
    knowledge_version_id: "knowledge-v1",
    revision: 3,
    source_version_id: "source-v2",
    review_needed: false,
    facts: [{
      fact_id: "tool-refund",
      name: "issue_refund",
      kind: "tool",
      summary: "The source lists this tool.",
      status: "inferred",
      evidence: [{ path: "src/tools.py", line_start: 18 }],
    }],
    pending_questions: [],
  };
  await page.addInitScript((initialReport) => {
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const json = (body, status = 200) => new Response(JSON.stringify(body), {
        status, headers: { "content-type": "application/json" },
      });
      if (path === "/api/sessions/session-a") return json({
        session_id: "session-a", project_id: "project-a", revision: 7,
      });
      if (path === "/api/projects/project-a/knowledge") return json(initialReport);
      if (path === "/api/projects/project-a/knowledge/confirm") return json({
        ...initialReport,
        report_id: "knowledge-v2",
        revision: 4,
        facts: [{ ...initialReport.facts[0], status: "user_confirmed" }],
      });
      return json({ error: { code: "not_found", message: "not found" } }, 404);
    };
  }, report);

  await page.goto(`${authoringPage}?session_id=session-a`);
  await expect(page.getByText("Resumed session · revision 7")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Knowledge report" })).toBeVisible();
  await expect(page.getByText("src/tools.py:18")).toBeVisible();

  await page.getByRole("button", { name: "Confirm finding" }).click();
  await expect(page.getByText("Finding confirmed. The service returned a new knowledge report revision.")).toBeVisible();
  await expect(page.getByText("User confirmed")).toBeVisible();
});

test("authoring workspace queues an explicit Git import with keyboard activation", async ({ page }) => {
  await page.addInitScript(() => {
    window.fetch = async (input) => {
      const path = String(input);
      const json = (body, status = 200) => new Response(JSON.stringify(body), {
        status, headers: { "content-type": "application/json" },
      });
      if (path === "/api/projects/project-a/imports") return json({ state: "queued", job_id: "import-job-7" });
      return json({ error: { code: "not_found", message: "not found" } }, 404);
    };
  });
  await page.goto(`${authoringPage}?project_id=project-a`);

  await page.getByLabel("HTTPS Git URL").fill("https://example.com/support-agent.git");
  await page.getByLabel("Git ref").fill("main");
  await page.getByRole("button", { name: "Queue source import" }).focus();
  await page.keyboard.press("Enter");

  await expect(page.getByText("Import queued. Worker job import-job-7 must finish before a report can load.")).toBeVisible();
  await expect(page.getByText("Import queued — report pending")).toBeVisible();
});

test("authoring workspace keeps source controls reachable at a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(authoringPage);

  const importButton = page.getByRole("button", { name: "Queue source import" });
  await importButton.scrollIntoViewIfNeeded();
  await expect(importButton).toBeInViewport();
});

test("authoring workspace preserves selected ZIP bytes during upload", async ({ page }) => {
  await page.addInitScript(() => {
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const json = (body) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
      if (path === "/api/uploads") {
        window.__zipUpload = {
          contentType: new Headers(init.headers).get("Content-Type"),
          bytes: init.body instanceof Blob ? [...new Uint8Array(await init.body.arrayBuffer())] : null,
        };
        return json({ state: "uploaded", upload_id: "upload-zip-1" });
      }
      if (path === "/api/projects/project-a/imports") return json({ state: "queued", job_id: "import-zip-1" });
      return json({ error: { code: "not_found", message: "not found" } });
    };
  });
  await page.goto(`${authoringPage}?project_id=project-a`);
  await page.getByRole("radio", { name: "ZIP archive" }).check();
  await page.locator("#source-zip-file").setInputFiles({
    name: "agent.zip", mimeType: "application/zip", buffer: Buffer.from([80, 75, 3, 4]),
  });
  await page.getByRole("button", { name: "Queue source import" }).click();

  await expect(page.getByText("Import queued. Worker job import-zip-1 must finish before a report can load.")).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.__zipUpload)).toEqual({
    contentType: "application/zip", bytes: [80, 75, 3, 4],
  });
});

test("authoring workspace detaches session mutations after switching projects", async ({ page }) => {
  await page.addInitScript(() => {
    window.__turns = [];
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const json = (body) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
      if (path === "/api/sessions/session-a") return json({ session_id: "session-a", project_id: "project-a", revision: 1 });
      if (path === "/api/projects/project-a/knowledge") return json({ report_id: "knowledge-a", revision: 1, source_version_id: "source-a", facts: [], pending_questions: [] });
      if (path.includes("/turns")) { window.__turns.push(path); return json({ state: "queued", job_id: "turn-1" }); }
      return json({ error: { code: "not_found", message: "not found" } });
    };
  });
  await page.goto(`${authoringPage}?session_id=session-a`);
  await page.getByLabel("Existing project ID").fill("project-b");
  await expect(page.getByText("Session detached — project changed to project-b")).toBeVisible();

  await page.getByLabel("Authoring note").fill("Update the current project metric");
  await page.getByRole("button", { name: "Send note" }).click();
  await page.getByRole("tab", { name: "Metrics" }).click();
  await page.getByRole("checkbox", { name: /Response is valid/i }).check();
  await page.getByRole("button", { name: "Review selected metrics" }).click();
  await page.getByLabel("When evidence is missing").selectOption("fail");
  await page.getByLabel("When the evaluator has an error").selectOption("error");
  await page.getByRole("button", { name: "Save metric decisions" }).click();

  await expect.poll(() => page.evaluate(() => window.__turns)).toEqual([]);
});

test("authoring workspace clears old report facts after a newer project report fails", async ({ page }) => {
  await page.addInitScript(() => {
    window.fetch = async (input) => {
      const path = String(input);
      const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
      if (path === "/api/projects/project-a/knowledge") return json({
        report_id: "knowledge-a", revision: 1, source_version_id: "source-a",
        facts: [{ fact_id: "a-tool", name: "Agent A tool", kind: "tool", status: "inferred", evidence: [] }], pending_questions: [],
      });
      if (path === "/api/projects/project-b/knowledge") return json({ error: { code: "not_found", message: "report is not ready" } }, 404);
      return json({ error: { code: "not_found", message: "not found" } }, 404);
    };
  });
  await page.goto(`${authoringPage}?project_id=project-a`);
  await expect(page.getByText("Agent A tool")).toBeVisible();
  await page.getByLabel("Existing project ID").fill("project-b");
  await page.getByRole("button", { name: "Load project report" }).click();

  await expect(page.getByText("Knowledge report unavailable — import source and wait for the worker")).toBeVisible();
  await expect(page.getByText("Agent A tool")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Knowledge report" })).toHaveCount(0);
});

test("authoring workspace presents pending entrypoint questions from a knowledge report", async ({ page }) => {
  await page.addInitScript(() => {
    window.fetch = async (input) => {
      const json = (body) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
      if (String(input) === "/api/projects/project-a/knowledge") return json({
        report_id: "knowledge-a", revision: 1, source_version_id: "source-a", facts: [],
        needs_entrypoint_declaration: true,
        pending_questions: [{ question_id: "entrypoint_declaration", text: "Which command should invoke this agent?" }],
      });
      return json({ error: { code: "not_found", message: "not found" } });
    };
  });
  await page.goto(`${authoringPage}?project_id=project-a`);

  await expect(page.getByText("Entrypoint declaration required")).toBeVisible();
  await expect(page.getByText("Which command should invoke this agent?")).toBeVisible();
  await expect(page.getByText("Answering these questions is unavailable in the current API.")).toBeVisible();
});

test("authoring workspace restores focus to the report after keyboard confirmation", async ({ page }) => {
  const report = {
    report_id: "knowledge-a", revision: 1, source_version_id: "source-a", pending_questions: [],
    facts: [{ fact_id: "tool-a", name: "Agent A tool", kind: "tool", status: "inferred", evidence: [] }],
  };
  await page.addInitScript((initialReport) => {
    window.fetch = async (input) => {
      const path = String(input);
      const json = (body) => new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
      if (path === "/api/projects/project-a/knowledge") return json(initialReport);
      if (path === "/api/projects/project-a/knowledge/confirm") return json({
        ...initialReport, revision: 2, facts: [{ ...initialReport.facts[0], status: "user_confirmed" }],
      });
      return json({ error: { code: "not_found", message: "not found" } });
    };
  }, report);
  await page.goto(`${authoringPage}?project_id=project-a`);
  const confirm = page.getByRole("button", { name: "Confirm finding" });
  await confirm.focus();
  await page.keyboard.press("Enter");

  await expect(page.getByText("User confirmed")).toBeVisible();
  await expect(page.locator("#knowledge-report-heading")).toBeFocused();
});
