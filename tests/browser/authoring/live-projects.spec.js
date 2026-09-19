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
