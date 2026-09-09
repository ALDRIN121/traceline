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
