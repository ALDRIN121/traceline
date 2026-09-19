const { defineConfig } = require("@playwright/test");
const fs = require("fs");
const path = require("path");

const liveServerPort = 8765;
const defaultPython = process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python";
const python = process.env.PYTHON || (fs.existsSync(defaultPython) ? defaultPython : "python");

module.exports = defineConfig({
  testDir: "tests/browser",
  timeout: 30_000,
  webServer: {
    command: `${python} tests/browser/support/live_server.py --port ${liveServerPort}`,
    url: `http://127.0.0.1:${liveServerPort}/health`,
    env: { PYTHONPATH: path.resolve("src") },
    reuseExistingServer: false,
    timeout: 120_000,
  },
  use: {
    browserName: "chromium",
    viewport: { width: 1440, height: 960 },
    screenshot: "only-on-failure",
  },
});
