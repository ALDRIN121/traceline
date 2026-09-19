const { test, expect } = require("@playwright/test");
const path = require("path");
const { pathToFileURL } = require("url");

const authoringPage = pathToFileURL(path.resolve(__dirname, "../../../web/index.html")).href;

function project(projectId, name, entrypoint = []) {
  return {
    project_id: projectId,
    name,
    entrypoint,
    smoke_state: "INGESTED",
    created_at: "2026-09-19T10:00:00Z",
    secret_ref: "vault://must-never-render",
  };
}

function knowledgeReportFixture(projectId) {
  return {
    report_id: `knowledge-${projectId}`,
    revision: 1,
    source_version_id: `source-${projectId}`,
    review_needed: false,
    facts: [{
      fact_id: `fact-${projectId}`,
      name: `${projectId} tool`,
      kind: "tool",
      summary: `${projectId} knowledge report`,
      status: "observed_static",
      evidence: [],
    }],
    pending_questions: [],
  };
}

function installProjectMock(page) {
  const projectA = project("project-a", "Support agent", ["python", "agent.py"]);
  const projectB = project("project-b", "Refund agent");
  return page.addInitScript(({ projectA, projectB, reportA, reportB }) => {
    window.__projectCalls = [];
    const json = (body, status = 200) => new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      window.__projectCalls.push({ path, method, body });
      if (path === "/api/projects" && method === "GET") {
        return json({ projects: [projectA, projectB] });
      }
      if (path === "/api/projects/project-a" && method === "GET") return json({ project: projectA });
      if (path === "/api/projects/project-b" && method === "GET") return json({ project: projectB });
      if (path === "/api/projects/project-a/knowledge") return json(reportA);
      if (path === "/api/projects/project-b/knowledge") return json(reportB);
      return json({ error: { code: "not_found", message: `unhandled ${method} ${path}` } }, 404);
    };
  }, {
    projectA,
    projectB,
    reportA: knowledgeReportFixture("project-a"),
    reportB: knowledgeReportFixture("project-b"),
  });
}

test("project chooser loads the service list without rendering secret fields", async ({ page }) => {
  await installProjectMock(page);
  await page.goto(authoringPage);

  await expect(page.getByRole("heading", { name: "Choose a project" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Select Support agent" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Select Refund agent" })).toBeVisible();
  const chooser = page.getByRole("region", { name: "Choose a project" });
  await expect(chooser).not.toContainText("must-never-render");
  await expect(chooser).not.toContainText("secret_ref");

  await page.getByRole("button", { name: "Select Support agent" }).click();
  await expect(page.getByText("project-a knowledge report")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("must-never-render");
  await expect(page.locator("body")).not.toContainText("vault://must-never-render");
});

test("project chooser creates a project with an observed id and loads its report", async ({ page }) => {
  const created = {
    project_id: "project-created",
    name: "New support agent",
    entrypoint: ["python", "main.py"],
    smoke_state: "INGESTED",
  };
  const createdReport = knowledgeReportFixture("project-created");
  await page.addInitScript(({ created, createdReport }) => {
    window.__projectCalls = [];
    const json = (body, status = 200) => new Response(JSON.stringify(body), {
      status, headers: { "content-type": "application/json" },
    });
    window.fetch = async (input, init = {}) => {
      const path = String(input);
      const method = init.method || "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : null;
      window.__projectCalls.push({ path, method, body });
      if (path === "/api/projects" && method === "GET") return json({ projects: [] });
      if (path === "/api/projects" && method === "POST") return json({ state: "created", project: created }, 201);
      if (path === "/api/projects/project-created") return json({ project: created });
      if (path === "/api/projects/project-created/knowledge") return json(createdReport);
      return json({ error: { code: "not_found", message: `unhandled ${method} ${path}` } }, 404);
    };
  }, { created, createdReport });
  await page.goto(authoringPage);

  await page.getByLabel("Project name").fill("New support agent");
  await page.getByLabel("Entrypoint (optional)").fill("python main.py");
  await page.getByRole("button", { name: "Create project" }).click();

  await expect(page.getByText("New support agent", { exact: true })).toBeVisible();
  await expect(page.locator("#selected-project-meta")).toContainText("project-created");
  await expect(page.getByRole("heading", { name: "Knowledge report" })).toBeVisible();
  await expect(page.getByText("project-created knowledge report")).toBeVisible();

  const calls = await page.evaluate(() => window.__projectCalls);
  const create = calls.find((call) => call.path === "/api/projects" && call.method === "POST");
  expect(create.body).toEqual({ name: "New support agent", entrypoint: ["python", "main.py"] });
});

test("selecting a project preserves session context and loads its server report", async ({ page }) => {
  await installProjectMock(page);
  await page.addInitScript(() => {
    const originalFetch = window.fetch;
    window.fetch = async (input, init = {}) => {
      if (String(input) === "/api/sessions/session-a") {
        return new Response(JSON.stringify({ session_id: "session-a", project_id: "project-a", revision: 2 }), {
          headers: { "content-type": "application/json" },
        });
      }
      return originalFetch(input, init);
    };
  });
  await page.goto(`${authoringPage}?session_id=session-a`);

  await page.getByRole("button", { name: "Select Support agent" }).focus();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/session_id=session-a&project_id=project-a|project_id=project-a&session_id=session-a/);
  await expect(page.getByText("project-a knowledge report")).toBeVisible();
  await expect(page.getByText("Project project-a selected")).toBeVisible();
});

test("stale project selection responses cannot replace the current report", async ({ page }) => {
  const projects = [project("project-a", "Alpha agent"), project("project-b", "Beta agent")];
  await page.addInitScript(({ projects, reportA, reportB }) => {
    const json = (body, status = 200) => new Response(JSON.stringify(body), {
      status, headers: { "content-type": "application/json" },
    });
    window.__finishAlpha = null;
    window.fetch = async (input) => {
      const path = String(input);
      if (path === "/api/projects") return json({ projects });
      if (path === "/api/projects/project-a") {
        return new Promise((resolve) => { window.__finishAlpha = () => resolve(json({ project: projects[0] })); });
      }
      if (path === "/api/projects/project-b") return json({ project: projects[1] });
      if (path === "/api/projects/project-b/knowledge") return json(reportB);
      if (path === "/api/projects/project-a/knowledge") return json(reportA);
      return json({ error: { code: "not_found", message: "not found" } }, 404);
    };
  }, { projects, reportA: knowledgeReportFixture("project-a"), reportB: knowledgeReportFixture("project-b") });
  await page.goto(authoringPage);

  await page.getByRole("button", { name: "Select Alpha agent" }).click();
  await page.getByRole("button", { name: "Select Beta agent" }).click();
  await expect(page.getByText("project-b knowledge report")).toBeVisible();
  await page.evaluate(() => window.__finishAlpha?.());
  await expect(page.getByText("project-b knowledge report")).toBeVisible();
  await expect(page.getByText("project-a knowledge report")).not.toBeVisible();
});

test("project chooser reports list failures without exposing response internals", async ({ page }) => {
  await page.addInitScript(() => {
    window.fetch = async (input) => {
      if (String(input) === "/api/projects") {
        return new Response(JSON.stringify({ error: { code: "projects_unavailable", message: "workspace secret detail" } }), {
          status: 503, headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ error: { code: "not_found", message: "not found" } }), {
        status: 404, headers: { "content-type": "application/json" },
      });
    };
  });
  await page.goto(authoringPage);

  await expect(page.getByText("Projects could not be loaded")).toBeVisible();
  await expect(page.locator("body")).not.toContainText("workspace secret detail");
  await expect(page.getByRole("region", { name: "Choose a project" })).not.toContainText("secret_ref");
});
