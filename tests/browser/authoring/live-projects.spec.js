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
