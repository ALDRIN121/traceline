const { defineConfig } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "tests/browser",
  timeout: 30_000,
  use: {
    browserName: "chromium",
    viewport: { width: 1440, height: 960 },
    screenshot: "only-on-failure",
  },
});
