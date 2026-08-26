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
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  ...(USING_LOCAL_PREVIEW && {
    webServer: {
      command: "npx vite preview --port 4173 --strictPort",
      url: BASE_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  }),
});
