const { defineConfig } = require("@playwright/test");
const os = require("os");
const path = require("path");
const fs = require("fs");

const releaseServerPort = 8767;
const defaultPython = process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python";
const python = process.env.PYTHON || (fs.existsSync(defaultPython) ? defaultPython : "python");
const releaseTokenPath = process.env.TRACELINE_RELEASE_TOKEN_FILE
  || path.join(os.tmpdir(), "traceline-browser-release-owner.token");
process.env.TRACELINE_RELEASE_TOKEN_FILE = releaseTokenPath;

module.exports = defineConfig({
  testDir: "tests/browser",
  testMatch: "**/release-auth.spec.js",
  timeout: 30_000,
  webServer: {
    command: `${python} tests/browser/support/release_server.py --port ${releaseServerPort} --token-file ${releaseTokenPath}`,
    url: `http://127.0.0.1:${releaseServerPort}/health`,
    env: { PYTHONPATH: path.resolve("src") },
    reuseExistingServer: false,
    timeout: 120_000,
  },
  use: {
    baseURL: `http://127.0.0.1:${releaseServerPort}`,
    browserName: "chromium",
    viewport: { width: 1440, height: 960 },
    screenshot: "only-on-failure",
  },
});
