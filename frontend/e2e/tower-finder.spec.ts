/**
 * Tower Finder frontend E2E tests.
 *
 * Covers the standalone UI this service ships:
 * - Page load and header rendering
 * - Search form validation and submission
 * - Server-side source classification (the client must not guess)
 * - Results table, summary strip, error states
 * - Map rendering
 */
import { test, expect } from "@playwright/test";
import { hosts } from "../playwright.config";

const BASE = hosts.frontend;

/** A tower row shaped like services/tower_ranking.py emits. */
function tower(overrides = {}) {
  return {
    rank: 1,
    callsign: "WGBH",
    name: "WGBH-TV",
    state: "MA",
    latitude: 42.30,
    longitude: -71.12,
    elevation_m: 60,
    altitude_m: 340,
    antenna_height_m: 280,
    frequency_mhz: 89.7,
    band: "FM",
    eirp_dbm: 78,
    distance_km: 12.3,
    bearing_deg: 145,
    bearing_cardinal: "SE",
    received_power_dbm: -62.1,
    ...overrides,
  };
}

function query(overrides = {}) {
  return {
    latitude: 42.38708028093612,
    longitude: -71.24905416622781,
    altitude_m: 43,
    radius_km: 80,
    source: "us",
    ...overrides,
  };
}

// The form pre-fills altitude from /api/elevation as coordinates are typed.
// Nothing here asserts on it, but leaving it unmocked means every spec logs a
// proxy connection error against a backend that isn't running.
test.beforeEach(async ({ page }) => {
  await page.route("**/api/elevation**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ latitude: 0, longitude: 0, elevation_m: 43 }),
    });
  });
});

test.describe("Tower Finder — page load", () => {
  test("loads and renders the app header", async ({ page }) => {
    await page.goto(BASE);
    await expect(page).toHaveTitle(/Tower Finder/i);
    await expect(page.locator("h1")).toHaveText(/Tower Finder/i);
  });

  test("search form is visible with lat/lon/altitude inputs", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.getByLabel(/latitude/i)).toBeVisible();
    await expect(page.getByLabel(/longitude/i)).toBeVisible();
    await expect(page.getByLabel(/altitude/i)).toBeVisible();
  });

  test("no JavaScript errors on load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForLoadState("networkidle");
    expect(errors).toHaveLength(0);
  });
});

test.describe("Tower Finder — search form", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE);
  });

  test("shows validation error if search submitted with empty fields", async ({ page }) => {
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();
    const latInput = page.getByLabel(/latitude/i);
    const validationMsg = await latInput.evaluate((el: HTMLInputElement) => el.validationMessage);
    expect(validationMsg).not.toBe("");
  });

  test("leaves source on auto and lets the server classify the coordinates", async ({ page }) => {
    // The client used to guess the country from lat/lon bounding boxes and pin
    // the dropdown, which sent "ca" for every US point above 42N. Detection
    // lives server-side against real border polygons now, so the form's job is
    // to stay out of the way and send "auto".
    const towersRequest = page.waitForRequest((r) => r.url().includes("/api/towers"));
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ towers: [], query: query(), count: 0 }),
      });
    });

    // Waltham, MA — 42.387N, the latitude the old bounding box misread as Canada.
    await page.getByLabel(/latitude/i).fill("42.38708028093612");
    await page.getByLabel(/longitude/i).fill("-71.24905416622781");
    await expect(page.getByLabel(/data source/i)).toHaveValue("auto");

    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();
    const url = new URL((await towersRequest).url());
    expect(url.searchParams.get("source")).toBe("auto");
  });

  test("measured frequencies are sent as one comma-separated parameter", async ({ page }) => {
    // parse_user_frequencies splits on ","; the route accepts the key repeated
    // as well. This pins the spelling the SPA sends, not the only one that
    // works.
    const towersRequest = page.waitForRequest((r) => r.url().includes("/api/towers"));
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ towers: [], query: query(), count: 0 }),
      });
    });

    await page.getByLabel(/latitude/i).fill("42.38708028093612");
    await page.getByLabel(/longitude/i).fill("-71.24905416622781");

    await page.getByRole("button", { name: /add measured frequencies/i }).click();
    await page.getByRole("button", { name: /add frequency/i }).click();
    await page.getByLabel(/^frequency 1/i).fill("95.5");
    await page.getByLabel(/^frequency 2/i).fill("101.1");

    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    const url = new URL((await towersRequest).url());
    expect(url.searchParams.getAll("frequencies")).toEqual(["95.5,101.1"]);
  });

  test("an explicitly chosen source is sent instead of auto", async ({ page }) => {
    const towersRequest = page.waitForRequest((r) => r.url().includes("/api/towers"));
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ towers: [], query: query({ source: "ca" }), count: 0 }),
      });
    });

    await page.getByLabel(/latitude/i).fill("43.6532");
    await page.getByLabel(/longitude/i).fill("-79.3832");
    await page.getByLabel(/data source/i).selectOption("ca");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    const url = new URL((await towersRequest).url());
    expect(url.searchParams.get("source")).toBe("ca");
  });
});

test.describe("Tower Finder — search results", () => {
  test("returns tower results for a known US location", async ({ page }) => {
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          towers: [
            tower(),
            tower({
              rank: 2,
              callsign: "WBZ",
              name: "WBZ-TV",
              frequency_mhz: 30.0,
              band: "VHF",
              distance_km: 8.1,
              bearing_cardinal: "E",
            }),
          ],
          query: query(),
          count: 2,
        }),
      });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("42.38708028093612");
    await page.getByLabel(/longitude/i).fill("-71.24905416622781");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    await expect(page.locator("table")).toBeVisible({ timeout: 10000 });
    await expect(page.locator("tbody tr")).toHaveCount(2);
    await expect(page.locator(".results-count")).toHaveText("2");
    await expect(page.locator(".summary-strip")).toBeVisible();
  });

  test("surfaces the resolved region so a misclassification is visible", async ({ page }) => {
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ towers: [tower()], query: query({ source: "us" }), count: 1 }),
      });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("42.38708028093612");
    await page.getByLabel(/longitude/i).fill("-71.24905416622781");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    await expect(page.locator(".summary-strip")).toContainText("US");
    await expect(page.locator(".summary-strip")).toContainText(/United States/i);
  });

  test("shows no-results message when API returns empty towers", async ({ page }) => {
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ towers: [], query: query(), count: 0 }),
      });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("42.38708028093612");
    await page.getByLabel(/longitude/i).fill("-71.24905416622781");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    await expect(page.getByText(/No suitable broadcast towers/i)).toBeVisible({ timeout: 10000 });
  });

  test("shows the server's message for a coordinate outside the supported regions", async ({ page }) => {
    // /api/towers answers 422 rather than silently serving US data.
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Location is not in a supported region (US, CA, AU)." }),
      });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("48.8566");
    await page.getByLabel(/longitude/i).fill("2.3522");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    await expect(page.locator(".error-banner")).toContainText(/not in a supported region/i);
  });

  test("shows error banner on API failure", async ({ page }) => {
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({ status: 500, body: "Internal Server Error" });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("42.38708028093612");
    await page.getByLabel(/longitude/i).fill("-71.24905416622781");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();

    await expect(page.locator(".error-banner")).toBeVisible({ timeout: 10000 });
  });
});

test.describe("Tower Finder — map rendering", () => {
  test("Leaflet map container is present", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".leaflet-container")).toBeVisible({ timeout: 8000 });
  });
});

test.describe("Tower Finder — subresources", () => {
  // Which origins the bundle reaches for, so a reintroduced CDN fails here
  // rather than in production. It does not test the policy: vite preview sends
  // no headers, so script-src, connect-src and font-src are unguarded until the
  // deploy probe asserts a real response (123zgec1bvn).
  test("loads nothing from a third party except the basemap tiles", async ({ page }) => {
    const foreign: string[] = [];
    let sawTile = false;
    const record = (raw: string) => {
      // data: and blob: have an opaque origin and an empty hostname, so test
      // the scheme before anything derived from it.
      const url = new URL(raw);
      if (url.protocol === "data:" || url.protocol === "blob:") return;
      if (url.origin === new URL(BASE).origin) return;
      // What the CSP source `https://*.basemaps.cartocdn.com` admits: https,
      // the default port, and at least one label before the dot. Stricter than
      // the policy on the label count, which is the safe direction; a scheme or
      // port that the policy would refuse lands in `foreign` instead.
      const isTile =
        url.protocol === "https:" &&
        url.port === "" &&
        /^[^.]+\.basemaps\.cartocdn\.com$/.test(url.hostname);
      if (isTile) {
        sawTile = true;
        return;
      }
      foreign.push(raw);
    };
    page.on("request", (r) => record(r.url()));

    // A non-empty result deliberately: the components that could reach for a
    // CDN are the ones that only mount when there are towers, so an empty list
    // would exercise none of them.
    await page.route("**/api/towers**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ towers: [tower()], query: query(), count: 1 }),
      });
    });

    await page.goto(BASE);
    await page.getByLabel(/latitude/i).fill("42.38708028093612");
    await page.getByLabel(/longitude/i).fill("-71.24905416622781");
    await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();
    // Proves the render actually happened, so an empty `foreign` cannot mean
    // "the page never loaded".
    await expect(page.locator("tbody tr")).toHaveCount(1);
    await expect(page.locator(".leaflet-marker-icon").first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    page.removeAllListeners("request");

    // A de-sharded tile URL is not a shape the CSP admits, so it lands in
    // `foreign` rather than passing quietly.
    expect(foreign).toEqual([]);
    expect(sawTile).toBe(true);
  });
});

test.describe("Tower Finder — theme", () => {
  const pick = (page, name: "Light" | "System" | "Dark") =>
    page.getByRole("radio", { name, exact: true }).click();

  // `system` is the default and stamps no attribute at all, so the OS
  // preference is answered by the media query in surface.css with no
  // JavaScript — which is the whole reason the control has a third state.
  test("follows the operating system with nothing stored, stamping nothing", async ({
    browser,
  }) => {
    for (const scheme of ["light", "dark"] as const) {
      const ctx = await browser.newContext({ colorScheme: scheme });
      const page = await ctx.newPage();
      await page.goto(BASE);

      await expect(page.getByRole("radio", { name: "System" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
      await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.*/);
      // The chrome still themes, which is the part the attribute would
      // otherwise be carrying.
      await expect(page.locator("body")).toHaveCSS(
        "background-color",
        scheme === "dark" ? "rgb(13, 27, 42)" : "rgb(241, 245, 249)",
      );
      await ctx.close();
    }
  });

  test("lets an explicit light choice beat a dark OS, and survives a reload", async ({
    browser,
  }) => {
    const ctx = await browser.newContext({ colorScheme: "dark" });
    const page = await ctx.newPage();
    await page.goto(BASE);

    await pick(page, "Light");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await expect(page.locator("body")).toHaveCSS("background-color", "rgb(241, 245, 249)");

    await page.reload();
    await expect(page.getByRole("radio", { name: "Light" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

    await ctx.close();
  });

  test("goes back to following the OS when system is picked again", async ({ browser }) => {
    const ctx = await browser.newContext({ colorScheme: "dark" });
    const page = await ctx.newPage();
    await page.goto(BASE);

    await pick(page, "Light");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

    await pick(page, "System");
    await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.*/);
    await expect(page.locator("body")).toHaveCSS("background-color", "rgb(13, 27, 42)");

    await ctx.close();
  });

  // The vectors are the one part of the surface that custom properties cannot
  // reach directly, and the failure is silent: react-leaflet replays
  // `pathOptions` through setStyle, which drops `className`, so a ring styled
  // that way keeps Leaflet's own blue in both themes and nothing errors.
  test("draws the search radius in the theme's accent", async ({ browser }) => {
    const stroke = async (scheme: "light" | "dark") => {
      const ctx = await browser.newContext({ colorScheme: scheme });
      const page = await ctx.newPage();
      await page.route("**/api/elevation**", (r) =>
        r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ elevation_m: 43 }) }),
      );
      await page.route("**/api/towers**", (r) =>
        r.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ towers: [tower()], query: query(), count: 1 }),
        }),
      );
      await page.goto(BASE);
      await page.getByLabel(/latitude/i).fill("42.38708028093612");
      await page.getByLabel(/longitude/i).fill("-71.24905416622781");
      await page.locator("button[type='submit']").filter({ hasText: /Find Towers/i }).click();
      const ring = page.locator("path.search-radius");
      await expect(ring).toBeAttached();
      const value = await ring.evaluate((el) => getComputedStyle(el).stroke);
      await ctx.close();
      return value;
    };

    expect(await stroke("light")).toBe("rgb(59, 130, 246)"); // dash's blue
    expect(await stroke("dark")).toBe("rgb(56, 189, 248)"); // the map's sky
  });

  // The basemap is the one thing the tokens cannot choose: it is a tile set
  // picked in JavaScript, which is what useResolvedTheme exists for.
  test("swaps the basemap with the resolved theme", async ({ browser }) => {
    const ctx = await browser.newContext({ colorScheme: "light" });
    const page = await ctx.newPage();
    await page.goto(BASE);
    await expect(page.locator(".leaflet-container")).toBeVisible({ timeout: 8000 });

    await expect(page.locator(".leaflet-tile-pane img").first()).toHaveAttribute(
      "src",
      /\/light_all\//,
    );

    await pick(page, "Dark");
    await expect(page.locator(".leaflet-tile-pane img").first()).toHaveAttribute(
      "src",
      /\/voyager\//,
    );

    await ctx.close();
  });
});
