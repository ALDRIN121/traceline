const { test, expect } = require("@playwright/test");

test.use({ baseURL: "http://127.0.0.1:8765" });

test("live authoring creates, lists, selects, and loads a project knowledge report", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  const apiRequests = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/")) {
      apiRequests.push({
        method: request.method(),
        path: new URL(request.url()).pathname,
        body: request.postDataJSON?.() ?? null,
      });
    }
  });

  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Choose a project" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Select Seeded support agent" })).toBeVisible();
  const seededProjectId = await page.locator(".project-option", { hasText: "Seeded support agent" }).getAttribute("data-project-id");
  expect(seededProjectId).toMatch(/^[0-9a-f]{32}$/);

  const createButton = page.getByRole("button", { name: "Create project" });
  await page.getByLabel("Project name").fill("Browser-created support agent");
  await page.getByLabel("Entrypoint (optional)").fill("python agent.py");
  await createButton.focus();
  await page.keyboard.press("Enter");

  await expect(page.getByRole("button", { name: "Select Browser-created support agent" })).toBeVisible();
  await expect(page.getByText(/Browser-created support agent · /)).toBeVisible();
  await expect(createButton).toBeInViewport();

  const seededProject = page.getByRole("button", { name: "Select Seeded support agent" });
  await seededProject.focus();
  await page.keyboard.press("Enter");

  await expect(page.getByRole("heading", { name: "Knowledge report" })).toBeVisible();
  await expect(page.getByText("lookup_order_status", { exact: true })).toBeVisible();
  await expect(page.getByText("src/support/agent.py:2", { exact: true })).toBeVisible();
  await expect(page.locator("#selected-project")).not.toContainText("secret_ref");
  await expect(page.locator("#knowledge-report-card")).not.toContainText("provider-token");

  expect(apiRequests).toEqual(expect.arrayContaining([
    { method: "GET", path: "/api/projects", body: null },
    {
      method: "POST",
      path: "/api/projects",
      body: { name: "Browser-created support agent", entrypoint: ["python", "agent.py"] },
    },
    { method: "GET", path: `/api/projects/${seededProjectId}`, body: null },
    { method: "GET", path: `/api/projects/${seededProjectId}/knowledge`, body: null },
  ]));
});

test("live authoring uploads a ZIP, completes its import job, and refreshes knowledge", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  const apiRequests = [];
  const jobStatuses = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/")) {
      apiRequests.push({ method: request.method(), path: new URL(request.url()).pathname });
    }
  });
  page.on("response", async (response) => {
    const path = new URL(response.url()).pathname;
    if (!/^\/api\/jobs\/[0-9a-f]{32}$/.test(path)) return;
    try {
      jobStatuses.push((await response.json()).status);
    } catch (_) {
      // The request recorder remains best-effort; the UI assertions are authoritative.
    }
  });

  await page.goto("/");
  await expect(page.getByRole("button", { name: "Select Seeded support agent" })).toBeVisible();
  const seededProject = page.locator(".project-option", { hasText: "Seeded support agent" });
  const seededProjectId = await seededProject.getAttribute("data-project-id");
  expect(seededProjectId).toMatch(/^[0-9a-f]{32}$/);

  const selectSeeded = page.getByRole("button", { name: "Select Seeded support agent" });
  await selectSeeded.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("Report revision 1")).toBeVisible();

  await page.getByRole("radio", { name: "ZIP archive" }).check();
  await page.locator("#source-zip-file").setInputFiles({
    name: "support-agent.zip",
    mimeType: "application/zip",
    buffer: Buffer.from(
      "UEsDBBQAAAAAABGbM113VRwINwAAADcAAAAUAAAAc3JjL3N1cHBvcnQvYWdlbnQucHlkZWYgYnVpbGRfdG9vbHMoKToKICAgIHJldHVybiB7InNoaXBfb3JkZXIiOiBvYmplY3QoKX0KUEsBAhQDFAAAAAAAEZszXXdVHAg3AAAANwAAABQAAAAAAAAAAAAAAIABAAAAAHNyYy9zdXBwb3J0L2FnZW50LnB5UEsFBgAAAAABAAEAQgAAAGkAAAAAAA==",
      "base64",
    ),
  });

  const importResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/projects/${seededProjectId}/imports`;
  });
  await page.getByRole("button", { name: "Queue source import" }).click();
  const queued = await (await importResponse).json();
  expect(queued.state).toBe("queued");
  expect(queued.job_id).toMatch(/^[0-9a-f]{32}$/);

  await expect.poll(() => jobStatuses, { timeout: 10_000 }).toContain("completed");
  await expect(page.locator("#source-import-status")).toHaveText("Import completed. Loading the new project report…", { timeout: 10_000 });
  await expect(page.getByText("Report revision 2")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("ship_order", { exact: true })).toBeVisible();

  expect(apiRequests).toEqual(expect.arrayContaining([
    { method: "POST", path: "/api/uploads" },
    { method: "POST", path: `/api/projects/${seededProjectId}/imports` },
    { method: "GET", path: `/api/jobs/${queued.job_id}` },
    { method: "GET", path: `/api/projects/${seededProjectId}/knowledge` },
  ]));
});

test("live authoring configures a target, verifies it, and shows the observed target version", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });

  const apiRequests = [];
  const jobSnapshots = [];
  page.on("request", (request) => {
    if (!request.url().includes("/api/")) return;
    const contentType = request.headers()["content-type"] || "";
    apiRequests.push({
      method: request.method(),
      path: new URL(request.url()).pathname,
      body: contentType.includes("application/json") ? request.postDataJSON() : null,
    });
  });
  page.on("response", async (response) => {
    const path = new URL(response.url()).pathname;
    if (!/^\/api\/jobs\/[0-9a-f]{32}$/.test(path)) return;
    try {
      jobSnapshots.push(await response.json());
    } catch (_) {
      // The UI status and URL remain the user-facing assertions.
    }
  });

  await page.goto("/");
  await expect(page.getByRole("button", { name: "Select Seeded support agent" })).toBeVisible();
  const seededProject = page.locator(".project-option", { hasText: "Seeded support agent" });
  const seededProjectId = await seededProject.getAttribute("data-project-id");
  expect(seededProjectId).toMatch(/^[0-9a-f]{32}$/);

  const selectSeeded = page.getByRole("button", { name: "Select Seeded support agent" });
  await selectSeeded.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Knowledge report" })).toBeVisible();

  await page.getByLabel("Target URL").fill("http://127.0.0.1:8766/invoke");
  const configureResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/projects/${seededProjectId}/connections`;
  });
  await page.getByRole("button", { name: "Configure target" }).click();
  const configured = await (await configureResponse).json();
  expect(configured.state).toBe("configured");
  expect(configured.target_id).toMatch(/^[0-9a-f]{32}$/);
  expect(configured.version_id).toMatch(/^[0-9a-f]{32}$/);
  await expect(page.locator("#target-connection-status")).toContainText("Target configured (configured)");

  const verifyResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/targets/${configured.target_id}/verify`;
  });
  await page.getByRole("button", { name: "Verify target" }).click();
  const queued = await (await verifyResponse).json();
  expect(queued.state).toBe("queued");
  expect(queued.job_id).toMatch(/^[0-9a-f]{32}$/);

  await expect.poll(
    () => jobSnapshots.find((job) => job.job_id === queued.job_id)?.status || null,
    { timeout: 10_000 },
  ).toBe("completed");
  const completed = jobSnapshots.find((job) => job.job_id === queued.job_id);
  expect(completed.result.state).toBe("verified");
  expect(completed.result.target_version_id).toMatch(/^[0-9a-f]{32}$/);
  expect(completed.result.target_version_id).not.toBe(configured.version_id);

  await expect(page.locator("#target-connection-status")).toContainText(
    `Target verified. Version ${completed.result.target_version_id}`,
    { timeout: 10_000 },
  );
  await expect.poll(() => new URL(page.url()).searchParams.get("target_version_id")).toBe(
    completed.result.target_version_id,
  );

  expect(apiRequests).toEqual(expect.arrayContaining([
    {
      method: "POST",
      path: `/api/projects/${seededProjectId}/connections`,
      body: {
        url: "http://127.0.0.1:8766/invoke",
        framework: "generic_http",
        mode: "stateless_json",
        auth: { type: "none" },
      },
    },
    {
      method: "POST",
      path: `/api/targets/${configured.target_id}/verify`,
      body: {
        target_version_id: configured.version_id,
        smoke_input: { message: "Traceline verification request" },
      },
    },
    { method: "GET", path: `/api/jobs/${queued.job_id}`, body: null },
  ]));
});

test("live authoring uploads CSV cases, reviews provenance, and commits a durable exclusion", async ({ page }) => {
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
        // The request recorder is best-effort; response assertions remain authoritative.
      }
    }
    apiRequests.push({ method: request.method(), path: new URL(request.url()).pathname, body });
  });

  await page.goto("/");
  const seededProject = page.locator(".project-option", { hasText: "Seeded support agent" });
  await expect(page.getByRole("button", { name: "Select Seeded support agent" })).toBeVisible();
  const seededProjectId = await seededProject.getAttribute("data-project-id");
  expect(seededProjectId).toMatch(/^[0-9a-f]{32}$/);

  await page.getByRole("button", { name: "Select Seeded support agent" }).click();
  await expect(page.getByRole("heading", { name: "Knowledge report" })).toBeVisible();
  await page.getByRole("tab", { name: "Dataset" }).click();
  await expect(page.getByRole("heading", { name: "Review test data" })).toBeVisible();

  await page.locator("#dataset-file").setInputFiles({
    name: "browser-cases.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "case_id,message,answer,label_status\n"
        + "keep-case,hello,world,approved\n"
        + "exclude-case,bye,goodbye,inferred\n",
    ),
  });
  await expect(page.locator("#dataset-format")).toHaveValue("csv");
  await page.getByLabel("Case ID field / pointer").fill("case_id");
  await page.getByLabel("Input field / pointer").fill("message");
  await page.getByLabel("Expected answer field / pointer").fill("answer");
  await page.getByLabel("Label status field / pointer").fill("label_status");

  const uploadResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/uploads";
  });
  const importResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/projects/${seededProjectId}/datasets/imports`;
  });
  await page.getByRole("button", { name: "Validate mapping" }).click();
  const uploaded = await (await uploadResponse).json();
  const report = await (await importResponse).json();

  expect(uploaded.state).toBe("uploaded");
  expect(uploaded.upload_id).toMatch(/^[0-9a-f]{32}$/);
  expect(report.state).toBe("validated");
  expect(report.dataset_id).toMatch(/^[0-9a-f]{32}$/);
  expect(report.report_id).toMatch(/^[0-9a-f]{32}$/);
  expect(report.label_coverage).toEqual({ approved: 1, inferred: 1, missing: 0, total: 2 });
  await expect(page.locator("#dataset-import-status")).toHaveText(
    "Mapping validated. Nothing is committed until you choose the rows to keep.",
  );
  await expect(page.locator("#dataset-report-card")).toBeVisible();
  await expect(page.locator("#dataset-report-meta")).toHaveText("2 cases · 1 approved labels · 0 missing");
  await expect(page.locator("#dataset-coverage")).toHaveText("1/2 approved");

  const keepRow = page.locator("#dataset-report-rows tr").filter({ hasText: "keep-case" });
  const excludeRow = page.locator("#dataset-report-rows tr").filter({ hasText: "exclude-case" });
  await expect(keepRow.locator(".dataset-label")).toHaveText("approved");
  await expect(keepRow.locator(".dataset-label")).toHaveAttribute("title", "Provenance: user_stated");
  await expect(excludeRow.locator(".dataset-label")).toHaveText("inferred");
  await expect(excludeRow.locator(".dataset-label")).toHaveAttribute("title", "Provenance: inferred");

  const exclusion = excludeRow.getByRole("checkbox", { name: "Keep case exclude-case" });
  await expect(exclusion).toBeChecked();
  await exclusion.uncheck();
  await expect(exclusion).not.toBeChecked();

  const commitResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/datasets/${report.dataset_id}/commit`;
  });
  await page.getByRole("button", { name: "Commit dataset revision" }).click();
  const committed = await (await commitResponse).json();
  expect(committed.state).toBe("validated");
  expect(committed.dataset_id).toBe(report.dataset_id);
  expect(committed.version_id).toMatch(/^[0-9a-f]{32}$/);
  expect(committed.revision).toBe(1);
  await expect(page.locator("#dataset-commit-status")).toHaveText(
    "Dataset revision 1 committed. 1 explicit exclusion(s).",
  );
  await expect.poll(() => new URL(page.url()).searchParams.get("dataset_version_id")).toBe(committed.version_id);

  const versionResponse = await page.request.get(
    `/api/datasets/${report.dataset_id}/versions/${committed.version_id}`,
  );
  expect(versionResponse.ok()).toBeTruthy();
  const version = await versionResponse.json();
  expect(version.revision).toBe(1);
  expect(version.cases.map((item) => item.case_id)).toEqual(["keep-case"]);
  expect(version.exclusions).toEqual([{ case_id: "exclude-case", reason: "explicit" }]);

  await page.reload();
  await expect(page.getByRole("button", { name: "Select Seeded support agent" })).toBeVisible();
  await page.getByRole("button", { name: "Select Seeded support agent" }).click();
  await page.getByRole("tab", { name: "Dataset" }).click();
  await expect(page.locator("#dataset-revision")).toHaveText("Revision 1", { timeout: 10_000 });
  await expect(page.locator("#dataset-report-rows tr")).toHaveCount(1);
  await expect(page.getByText("keep-case", { exact: true })).toBeVisible();
  await expect(page.getByText("exclude-case", { exact: true })).toHaveCount(0);

  expect(apiRequests).toEqual(expect.arrayContaining([
    { method: "POST", path: "/api/uploads", body: null },
    {
      method: "POST",
      path: `/api/projects/${seededProjectId}/datasets/imports`,
      body: {
        upload_id: uploaded.upload_id,
        dataset_id: null,
        mapping: {
          format: "csv",
          case_id: "case_id",
          input_fields: { query: "message" },
          expected_fields: { answer: "answer" },
          label_status: "label_status",
        },
        metric_requirements: [
          { metric_id: "latency", class: "health", requires_label: false },
          { metric_id: "accuracy", class: "accuracy", requires_label: true },
        ],
      },
    },
    {
      method: "POST",
      path: `/api/datasets/${report.dataset_id}/commit`,
      body: { report_id: report.report_id, explicit_exclusions: ["exclude-case"], expected_revision: 0 },
    },
  ]));
});

test("live authoring uploads JSONL cases with pointer mappings and persists an exclusion", async ({ page }) => {
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
        // The request recorder is best-effort; response assertions remain authoritative.
      }
    }
    apiRequests.push({ method: request.method(), path: new URL(request.url()).pathname, body });
  });

  await page.goto("/");
  const seededProject = page.locator(".project-option", { hasText: "Seeded support agent" });
  await expect(page.getByRole("button", { name: "Select Seeded support agent" })).toBeVisible();
  const seededProjectId = await seededProject.getAttribute("data-project-id");
  expect(seededProjectId).toMatch(/^[0-9a-f]{32}$/);

  await page.getByRole("button", { name: "Select Seeded support agent" }).click();
  await expect(page.getByRole("heading", { name: "Knowledge report" })).toBeVisible();
  await page.getByRole("tab", { name: "Dataset" }).click();
  await expect(page.getByRole("heading", { name: "Review test data" })).toBeVisible();

  await page.locator("#dataset-file").setInputFiles({
    name: "browser-cases.jsonl",
    mimeType: "application/x-ndjson",
    buffer: Buffer.from(
      '{"case_id":"jsonl-keep","input":{"message":"hello"},"expected":{"answer":"world"},"label_status":"approved"}\n'
        + '{"case_id":"jsonl-exclude","input":{"message":"bye"},"expected":{"answer":"goodbye"}}\n',
    ),
  });
  await expect(page.locator("#dataset-format")).toHaveValue("jsonl");

  const uploadResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === "/api/uploads";
  });
  const importResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/projects/${seededProjectId}/datasets/imports`;
  });
  await page.getByRole("button", { name: "Validate mapping" }).click();
  const uploaded = await (await uploadResponse).json();
  const report = await (await importResponse).json();

  expect(uploaded.state).toBe("uploaded");
  expect(uploaded.upload_id).toMatch(/^[0-9a-f]{32}$/);
  expect(report.state).toBe("validated");
  expect(report.dataset_id).toMatch(/^[0-9a-f]{32}$/);
  expect(report.report_id).toMatch(/^[0-9a-f]{32}$/);
  expect(report.label_coverage).toEqual({ approved: 1, inferred: 1, missing: 0, total: 2 });
  await expect(page.locator("#dataset-report-meta")).toHaveText("2 cases · 1 approved labels · 0 missing");

  const keepRow = page.locator("#dataset-report-rows tr").filter({ hasText: "jsonl-keep" });
  const excludeRow = page.locator("#dataset-report-rows tr").filter({ hasText: "jsonl-exclude" });
  await expect(keepRow.locator(".dataset-label")).toHaveText("approved");
  await expect(keepRow.locator(".dataset-label")).toHaveAttribute("title", "Provenance: user_stated");
  await expect(excludeRow.locator(".dataset-label")).toHaveText("inferred");
  await expect(excludeRow.locator(".dataset-label")).toHaveAttribute("title", "Provenance: inferred");
  await excludeRow.getByRole("checkbox", { name: "Keep case jsonl-exclude" }).uncheck();

  const commitResponse = page.waitForResponse((response) => {
    return response.request().method() === "POST"
      && new URL(response.url()).pathname === `/api/datasets/${report.dataset_id}/commit`;
  });
  await page.getByRole("button", { name: "Commit dataset revision" }).click();
  const committed = await (await commitResponse).json();
  expect(committed.state).toBe("validated");
  expect(committed.dataset_id).toBe(report.dataset_id);
  expect(committed.version_id).toMatch(/^[0-9a-f]{32}$/);
  expect(committed.revision).toBe(1);
  await expect(page.locator("#dataset-commit-status")).toHaveText(
    "Dataset revision 1 committed. 1 explicit exclusion(s).",
  );

  const versionResponse = await page.request.get(
    `/api/datasets/${report.dataset_id}/versions/${committed.version_id}`,
  );
  expect(versionResponse.ok()).toBeTruthy();
  const version = await versionResponse.json();
  expect(version.revision).toBe(1);
  expect(version.cases.map((item) => item.case_id)).toEqual(["jsonl-keep"]);
  expect(version.exclusions).toEqual([{ case_id: "jsonl-exclude", reason: "explicit" }]);

  await page.reload();
  await expect(page.getByRole("button", { name: "Select Seeded support agent" })).toBeVisible();
  await page.getByRole("button", { name: "Select Seeded support agent" }).click();
  await page.getByRole("tab", { name: "Dataset" }).click();
  await expect(page.locator("#dataset-revision")).toHaveText("Revision 1", { timeout: 10_000 });
  await expect(page.locator("#dataset-report-rows tr")).toHaveCount(1);
  await expect(page.getByText("jsonl-keep", { exact: true })).toBeVisible();
  await expect(page.getByText("jsonl-exclude", { exact: true })).toHaveCount(0);

  expect(apiRequests).toEqual(expect.arrayContaining([
    { method: "POST", path: "/api/uploads", body: null },
    {
      method: "POST",
      path: `/api/projects/${seededProjectId}/datasets/imports`,
      body: {
        upload_id: uploaded.upload_id,
        dataset_id: null,
        mapping: {
          format: "jsonl",
          case_id: "/case_id",
          input: "/input",
          expected: "/expected",
          label_status: "/label_status",
        },
        metric_requirements: [
          { metric_id: "latency", class: "health", requires_label: false },
          { metric_id: "accuracy", class: "accuracy", requires_label: true },
        ],
      },
    },
    {
      method: "POST",
      path: `/api/datasets/${report.dataset_id}/commit`,
      body: { report_id: report.report_id, explicit_exclusions: ["jsonl-exclude"], expected_revision: 0 },
    },
  ]));
});
