import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config.
 *
 * By default this builds nothing and serves the already-built `dist/` with
 * `vite preview`, so the suite needs node only — no Python, no live FCC
 * credentials. Every spec mocks /api itself. Point it at a real deployment
 * instead with E2E_BASE_URL=https://… to exercise the served-by-FastAPI path.
 */
const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:4173";
const USING_LOCAL_PREVIEW = !process.env.E2E_BASE_URL;

export const hosts = {
  frontend: BASE_URL,
};

/**
 * Browser tests of the map wrapper (src/lib/leaflet), on a harness page the
 * Vite dev server serves. Development React, so StrictMode really mounts
 * twice. `vite build` takes only index.html, so the harness never ships.
 *
 * BROWSER_TEST_PORT moves the dev server, to run another checkout beside this one.
 */
const BROWSER_TEST_PORT = Number(process.env.BROWSER_TEST_PORT ?? 4174);
const BROWSER_TEST_ORIGIN = `http://127.0.0.1:${BROWSER_TEST_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "list" : "html",
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    {
      name: "map-wrapper",
      testDir: "./src/lib/leaflet",
      testMatch: "**/*.browser.ts",
      use: { ...devices["Desktop Chrome"], baseURL: BROWSER_TEST_ORIGIN },
    },
  ],
  webServer: [
    ...(USING_LOCAL_PREVIEW
      ? [
          {
            command: "npx vite preview --port 4173 --strictPort",
            url: BASE_URL,
            reuseExistingServer: !process.env.CI,
            timeout: 60_000,
          },
        ]
      : []),
    {
      command: `npx vite --host 127.0.0.1 --port ${BROWSER_TEST_PORT} --strictPort`,
      url: `${BROWSER_TEST_ORIGIN}/src/lib/leaflet/browser/harness.html`,
      // A server already on the port may be another checkout's, so never reuse one.
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
