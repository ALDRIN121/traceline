const fs = require("fs");
const { test, expect } = require("@playwright/test");

test("release-mode browser bootstrap authenticates API wrappers without changing local mode", async ({ page }) => {
  const tokenPath = process.env.TRACELINE_RELEASE_TOKEN_FILE;
  const token = fs.readFileSync(tokenPath, "utf8").trim();
  expect(token).toBeTruthy();

  const anonymousProjects = await page.request.get("/api/projects");
  expect(anonymousProjects.status()).toBe(401);
  const anonymousRuns = await page.request.get("/runs?limit=1");
  expect(anonymousRuns.status()).toBe(401);

  const authenticatedProjects = await page.request.get("/api/projects", {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(authenticatedProjects.ok()).toBeTruthy();
  expect((await authenticatedProjects.json()).projects).toEqual([
    expect.objectContaining({ name: "Release auth agent" }),
  ]);

  const apiRequests = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/runs")) {
      apiRequests.push({ path: `${url.pathname}${url.search}`, authorization: request.headers().authorization });
    }
  });

  await page.goto(`/#install_token=${encodeURIComponent(token)}`);
  await expect(page.getByRole("heading", { name: "Choose a project" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Select Release auth agent" })).toBeVisible();
  await expect(page.locator("#project-list-status")).toContainText("1 project available");
  expect(new URL(page.url()).hash).toBe("");
  expect(page.url()).not.toContain("install_token");
  expect(await page.evaluate(() => localStorage.getItem("install_token"))).toBeNull();

  expect(apiRequests).toEqual(expect.arrayContaining([
    expect.objectContaining({ path: "/api/projects", authorization: `Bearer ${token}` }),
    expect.objectContaining({ path: "/api/evals", authorization: `Bearer ${token}` }),
    expect.objectContaining({ path: "/runs?limit=500", authorization: `Bearer ${token}` }),
  ]));
});
