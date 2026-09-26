import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ThemeProvider } from "../context/ThemeContext";
import { stubBrowser } from "./browser";

// react-leaflet needs a real layout box; jsdom gives it none and Leaflet throws
// on init. The map isn't what these tests are about — the e2e suite asserts the
// real .leaflet-container renders.
vi.mock("../components/TowerMap", () => ({
  default: () => <div data-testid="tower-map" />,
}));

const WALTHAM_TOWER = {
  rank: 1,
  callsign: "WGBH",
  name: "WGBH-TV",
  state: "MA",
  latitude: 42.3,
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
  // The expected-area fields the ranking is now sorted on. All optional on the
  // wire — the "older backend" test below drops them entirely.
  expected_area_km2: 16900.4,
  best_azimuth_deg: 240,
  horizon_km: 180,
};

function mockApi(towersResponse: { status: number; body: unknown }) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      if (url.includes("/api/elevation")) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ elevation_m: 43 }) });
      }
      return Promise.resolve({
        ok: towersResponse.status < 400,
        status: towersResponse.status,
        json: () => Promise.resolve(towersResponse.body),
      });
    }),
  );
}

async function search(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/latitude/i), "42.38708028093612");
  await user.type(screen.getByLabelText(/longitude/i), "-71.24905416622781");
  await user.click(screen.getByRole("button", { name: /find towers/i }));
}

function fetchedUrls(): string[] {
  const mock = globalThis.fetch as unknown as { mock: { calls: [string][] } };
  return mock.mock.calls.map((c) => c[0]);
}

function towersRequestUrls(): string[] {
  return fetchedUrls().filter((u) => u.includes("/api/towers"));
}

function towersRequestUrl(): string | undefined {
  return towersRequestUrls()[0];
}

// The header carries the appearance switch, which reads the theme through a
// provider that throws without one — there is no sensible default for "change
// the theme". Rendering App means rendering that.
function renderApp() {
  return render(
    <ThemeProvider>
      <App />
    </ThemeProvider>,
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
  stubBrowser();
  // jsdom keeps one location for the whole file, and a search now writes to it.
  window.history.replaceState(null, "", "/");
});

describe("App", () => {
  it("mounts and renders the header and search form", () => {
    mockApi({ status: 200, body: { towers: [], query: null, count: 0 } });
    renderApp();
    expect(screen.getByRole("heading", { name: /tower finder/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/latitude/i)).toBeInTheDocument();
    expect(screen.getByTestId("tower-map")).toBeInTheDocument();
  });

  it("renders results and reports the region the server resolved", async () => {
    mockApi({
      status: 200,
      body: {
        towers: [WALTHAM_TOWER],
        query: {
          latitude: 42.38708028093612,
          longitude: -71.24905416622781,
          altitude_m: 43,
          radius_km: 80,
          source: "us",
        },
        count: 1,
      },
    });
    const user = userEvent.setup();
    renderApp();
    await search(user);

    // "WGBH" appears in both the summary strip and the table, so scope to the row.
    await waitFor(() => expect(document.querySelector("tbody tr")).toBeInTheDocument());
    expect(screen.getByRole("cell", { name: "WGBH" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: /WGBH-TV, MA/ })).toBeInTheDocument();
    // The resolved region is surfaced: a Waltham search reporting CA is exactly
    // the bug this UI used to cause, so it must be visible rather than implied.
    const summary = document.querySelector(".summary-strip");
    expect(summary).toHaveTextContent("US");
    expect(summary).toHaveTextContent(/United States/i);
  });

  it("shows the server's detail message when no region is within the radius", async () => {
    mockApi({
      status: 422,
      body: {
        detail:
          "No tower data within 80 km of this location (coverage: US, CA, AU). Try a larger search radius.",
      },
    });
    const user = userEvent.setup();
    renderApp();
    await search(user);

    await waitFor(() =>
      expect(document.querySelector(".error-banner")).toHaveTextContent(
        /No tower data within 80 km.*Try a larger search radius/i,
      ),
    );
  });
  // The seam this ticket exists to close: the form collects frequencies, App
  // forwards them, api.ts serialises them. A break anywhere in that chain is a
  // 200 with the parameter silently absent.
  it("carries entered frequencies through to the tower request", async () => {
    mockApi({ status: 200, body: { towers: [], query: null, count: 0 } });
    const user = userEvent.setup();
    renderApp();

    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.click(screen.getByRole("button", { name: /add frequency/i }));
    await user.type(screen.getByLabelText(/^frequency 1/i), "95.5");
    await user.type(screen.getByLabelText(/^frequency 2/i), "101.1");
    await search(user);

    await waitFor(() => expect(towersRequestUrl()).toBeDefined());
    const params = new URL(towersRequestUrl()!, "https://towers.invalid").searchParams;
    expect(params.get("frequencies")).toBe("95.5,101.1");
  });

  it("colours ranks by tier and lists channel-sharing partners", async () => {
    const secondTower = {
      ...WALTHAM_TOWER,
      rank: 2,
      callsign: "WBZ",
      name: "WBZ-TV",
      frequency_mhz: 96.1,
      received_power_dbm: -70,
      // The server always sends the key, empty when the tower stands alone. A
      // `length && ...` guard would render that empty array as a literal "0".
      shared_callsigns: [] as string[],
    };
    mockApi({
      status: 200,
      body: {
        towers: [{ ...WALTHAM_TOWER, shared_callsigns: ["WGBH-DT2"] }, secondTower],
        query: {
          latitude: 42.38708028093612,
          longitude: -71.24905416622781,
          altitude_m: 43,
          radius_km: 80,
          source: "us",
        },
        count: 2,
      },
    });
    const user = userEvent.setup();
    renderApp();
    await search(user);

    await waitFor(() => expect(document.querySelectorAll("tbody tr")).toHaveLength(2));
    expect(screen.getByRole("cell", { name: /^Best$/ })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: /^Middle$/ })).toBeInTheDocument();
    expect(screen.getByText(/\+ WGBH-DT2/)).toBeInTheDocument();
    // Exact name: a stray "0" from the empty list would make this "WBZ0".
    expect(screen.getByRole("cell", { name: "WBZ" })).toBeInTheDocument();
  });

  it("shows the detect area the rank is built on, and where to point", async () => {
    mockApi({
      status: 200,
      body: {
        towers: [WALTHAM_TOWER],
        query: {
          latitude: 42.38708028093612,
          longitude: -71.24905416622781,
          altitude_m: 43,
          radius_km: 80,
          source: "us",
        },
        count: 1,
      },
    });
    const user = userEvent.setup();
    renderApp();
    await search(user);

    await waitFor(() => expect(document.querySelector("tbody tr")).toBeInTheDocument());
    // Thousands separated, no decimals: 16900.4 km² reads as "16,900".
    expect(screen.getByRole("cell", { name: "16,900" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "240° WSW" })).toBeInTheDocument();
    // The top pick card says why it is top.
    expect(document.querySelector(".summary-strip")).toHaveTextContent("16,900 km²");
  });

  it("leaves the new columns blank when an older backend omits the fields", async () => {
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { expected_area_km2, best_azimuth_deg, horizon_km, ...legacyTower } = WALTHAM_TOWER;
    mockApi({
      status: 200,
      body: {
        towers: [legacyTower],
        query: {
          latitude: 42.38708028093612,
          longitude: -71.24905416622781,
          altitude_m: 43,
          radius_km: 80,
          source: "us",
        },
        count: 1,
      },
    });
    const user = userEvent.setup();
    renderApp();
    await search(user);

    await waitFor(() => expect(document.querySelector("tbody tr")).toBeInTheDocument());
    // Blank, not "NaN" and not a placeholder that would read as a real zero.
    expect(document.querySelector("tbody .detect-area")?.textContent).toBe("");
    expect(document.querySelector("tbody .point")?.textContent).toBe("");
    // The top pick card keeps exactly its old text.
    const summary = document.querySelector(".summary-strip");
    expect(summary).toHaveTextContent("Top Pick — 12.3 km");
    expect(summary).not.toHaveTextContent("km²");
  });

  it("mutes towers past the radio horizon rather than hiding them", async () => {
    const farTower = {
      ...WALTHAM_TOWER,
      rank: 2,
      callsign: "WFAR",
      name: "WFAR-TV",
      frequency_mhz: 98.5,
      distance_km: 220,
      horizon_km: 180,
      expected_area_km2: 120,
    };
    mockApi({
      status: 200,
      body: {
        towers: [WALTHAM_TOWER, farTower],
        query: {
          latitude: 42.38708028093612,
          longitude: -71.24905416622781,
          altitude_m: 43,
          radius_km: 80,
          source: "us",
        },
        count: 2,
      },
    });
    const user = userEvent.setup();
    renderApp();
    await search(user);

    await waitFor(() => expect(document.querySelectorAll("tbody tr")).toHaveLength(2));
    const rows = document.querySelectorAll("tbody tr");
    // Still listed — an operator needs to see why a familiar transmitter ranks low.
    expect(rows[1]).toHaveTextContent("WFAR");
    expect(rows[1]).toHaveClass("beyond-horizon");
    expect(rows[1]).toHaveAttribute("title", "Beyond radio horizon");
    // The in-horizon tower is untouched.
    expect(rows[0]).not.toHaveClass("beyond-horizon");
    expect(rows[0]).not.toHaveAttribute("title");
  });
});

const UBC_QUERY = {
  latitude: 49.2648,
  longitude: -123.2502,
  altitude_m: 95,
  radius_km: 80,
  source: "ca",
};

const UBC_TOWER = { ...WALTHAM_TOWER, callsign: "CBU-FM", name: "CBU-FM", state: "BC" };

/** Put a share link in the address bar, as if the page had been opened on it. */
function openAt(search: string) {
  window.history.replaceState(null, "", `/${search}`);
}

/** A clipboard whose writeText does what `impl` says. Installed after
 *  userEvent.setup(), which attaches a stub of its own. */
function stubClipboard(impl: (text: string) => Promise<void>) {
  const writeText = vi.fn(impl);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  return writeText;
}

/** Long enough for anything a mount would have fired to have gone out. */
const settle = (ms = 50) => new Promise((r) => setTimeout(r, ms));

describe("App share links", () => {
  it("runs the search a link describes on load, once, with every parameter", async () => {
    mockApi({ status: 200, body: { towers: [UBC_TOWER], query: UBC_QUERY, count: 1 } });
    openAt("?lat=49.2648&lon=-123.2502&alt=95&source=CA&f=99.9,102.1");
    // StrictMode runs mount effects twice in development; the link must still
    // cost one search, not two.
    render(
      <StrictMode>
        <ThemeProvider>
          <App />
        </ThemeProvider>
      </StrictMode>,
    );

    await waitFor(() => expect(document.querySelector("tbody tr")).toBeInTheDocument());
    expect(towersRequestUrls()).toHaveLength(1);
    const params = new URL(towersRequestUrl()!, "https://towers.invalid").searchParams;
    expect(params.get("lat")).toBe("49.2648");
    expect(params.get("lon")).toBe("-123.2502");
    expect(params.get("altitude")).toBe("95");
    expect(params.get("source")).toBe("ca");
    expect(params.get("frequencies")).toBe("99.9,102.1");

    // The form shows what ran, so the recipient can adjust it and search again.
    expect(screen.getByLabelText(/latitude/i)).toHaveValue(49.2648);
    expect(screen.getByLabelText(/longitude/i)).toHaveValue(-123.2502);
    expect(screen.getByLabelText(/altitude/i)).toHaveValue(95);
    expect(screen.getByLabelText(/data source/i)).toHaveValue("ca");
    expect(screen.getByLabelText(/^frequency 1/i)).toHaveValue(99.9);
    expect(screen.getByLabelText(/^frequency 2/i)).toHaveValue(102.1);
    expect(document.querySelector(".summary-strip")).toHaveTextContent("CBU-FM");
    // Rewritten in the normalised spelling the form would have produced.
    expect(window.location.search).toBe(
      "?lat=49.2648&lon=-123.2502&alt=95&source=ca&f=99.9,102.1",
    );
  });

  it("sends altitude 0 without waiting for the elevation lookup when the link has none", async () => {
    mockApi({ status: 200, body: { towers: [UBC_TOWER], query: UBC_QUERY, count: 1 } });
    openAt("?lat=49.2648&lon=-123.2502");
    renderApp();

    await waitFor(() => expect(towersRequestUrl()).toBeDefined());
    // First out of the door: the elevation lookup is debounced behind it.
    expect(fetchedUrls()[0]).toContain("/api/towers");
    const params = new URL(towersRequestUrl()!, "https://towers.invalid").searchParams;
    // 0 is how /api/towers is asked to resolve the ground elevation itself.
    expect(params.get("altitude")).toBe("0");
    expect(params.get("source")).toBe("auto");
    expect(params.has("frequencies")).toBe(false);

    // The form still fills the field for display, as it does for typed coordinates.
    await waitFor(() => expect(screen.getByLabelText(/altitude/i)).toHaveValue(43));
    // The prefill is not the operator's, so the link does not grow an alt.
    expect(window.location.search).toBe("?lat=49.2648&lon=-123.2502");
  });

  it("writes a submitted search into the address bar", async () => {
    mockApi({ status: 200, body: { towers: [WALTHAM_TOWER], query: UBC_QUERY, count: 1 } });
    const user = userEvent.setup();
    renderApp();

    // Altitude first, so the elevation prefill (which fires once both
    // coordinates are in) finds it already set and stays out of the way.
    await user.type(screen.getByLabelText(/altitude/i), "120");
    await user.selectOptions(screen.getByLabelText(/data source/i), "us");
    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.type(screen.getByLabelText(/^frequency 1/i), "95.5");
    await search(user);

    await waitFor(() => expect(towersRequestUrl()).toBeDefined());
    expect(window.location.pathname).toBe("/");
    expect(window.location.search).toBe(
      "?lat=42.38708028093612&lon=-71.24905416622781&alt=120&source=us&f=95.5",
    );
  });

  it("leaves altitude, auto and an empty frequency list out of the link", async () => {
    mockApi({ status: 200, body: { towers: [WALTHAM_TOWER], query: UBC_QUERY, count: 1 } });
    const user = userEvent.setup();
    renderApp();

    await search(user);

    await waitFor(() => expect(towersRequestUrl()).toBeDefined());
    expect(window.location.search).toBe("?lat=42.38708028093612&lon=-71.24905416622781");
  });

  it("makes a failing search linkable too", async () => {
    mockApi({ status: 422, body: { detail: "No tower data within 80 km of this location." } });
    const user = userEvent.setup();
    renderApp();

    await search(user);

    const banner = await waitFor(() => {
      const el = document.querySelector(".error-banner");
      expect(el).toHaveTextContent(/No tower data/);
      return el as HTMLElement;
    });
    expect(window.location.search).toBe("?lat=42.38708028093612&lon=-71.24905416622781");
    expect(banner.querySelector("button")).toHaveTextContent("Copy link");
  });

  it.each([
    ["an out-of-range latitude", "?lat=91&lon=-123.2502"],
    ["an unreadable longitude", "?lat=49.2648&lon=west"],
    ["a coordinate that is only half there", "?lat=49.2648"],
    ["an unreadable altitude", "?lat=49.2648&lon=-123.2502&alt=high"],
    ["a negative altitude", "?lat=49.2648&lon=-123.2502&alt=-5"],
  ])("does not search on %s, and says nothing about it", async (_, search) => {
    mockApi({ status: 200, body: { towers: [UBC_TOWER], query: UBC_QUERY, count: 1 } });
    openAt(search);
    renderApp();

    await settle();
    expect(towersRequestUrls()).toHaveLength(0);
    expect(document.querySelector(".error-banner")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /copy link/i })).not.toBeInTheDocument();
    // Not rewritten either: nothing ran, so there is nothing new to share.
    expect(window.location.search).toBe(search);
  });

  it("prefills whatever part of a broken link is valid", async () => {
    mockApi({ status: 200, body: { towers: [UBC_TOWER], query: UBC_QUERY, count: 1 } });
    openAt("?lat=49.2648&lon=west&alt=95&source=xx&f=99.9,0,abc");
    renderApp();

    await settle();
    expect(towersRequestUrls()).toHaveLength(0);
    expect(screen.getByLabelText(/latitude/i)).toHaveValue(49.2648);
    expect(screen.getByLabelText(/longitude/i)).toHaveValue(null);
    expect(screen.getByLabelText(/altitude/i)).toHaveValue(95);
    expect(screen.getByLabelText(/data source/i)).toHaveValue("auto");
    expect(screen.getByLabelText(/^frequency 1/i)).toHaveValue(99.9);
    expect(screen.queryByLabelText(/^frequency 2/i)).not.toBeInTheDocument();
  });

  it("offers no copy link before anything has been searched", () => {
    mockApi({ status: 200, body: { towers: [], query: null, count: 0 } });
    renderApp();
    expect(screen.queryByRole("button", { name: /copy link/i })).not.toBeInTheDocument();
  });

  it("copies the address bar and says so", async () => {
    mockApi({ status: 200, body: { towers: [UBC_TOWER], query: UBC_QUERY, count: 1 } });
    openAt("?lat=49.2648&lon=-123.2502&alt=95");
    const user = userEvent.setup();
    const writeText = stubClipboard(() => Promise.resolve());
    renderApp();

    const button = await screen.findByRole("button", { name: /copy link/i });
    expect(button.closest(".summary-strip")).not.toBeNull();
    await user.click(button);

    expect(writeText).toHaveBeenCalledWith(window.location.href);
    expect(window.location.href).toBe(
      `${window.location.origin}/?lat=49.2648&lon=-123.2502&alt=95`,
    );
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/link to this search/i)).not.toBeInTheDocument();
  });

  it("shows the link selected for a manual copy when the clipboard refuses", async () => {
    mockApi({ status: 200, body: { towers: [UBC_TOWER], query: UBC_QUERY, count: 1 } });
    openAt("?lat=49.2648&lon=-123.2502");
    const user = userEvent.setup();
    stubClipboard(() => Promise.reject(new DOMException("Denied", "NotAllowedError")));
    renderApp();

    await user.click(await screen.findByRole("button", { name: /copy link/i }));

    const field = (await screen.findByLabelText(/link to this search/i)) as HTMLInputElement;
    expect(field).toHaveValue(window.location.href);
    expect(field).toHaveAttribute("readonly");
    expect(field).toHaveFocus();
    expect(field.selectionStart).toBe(0);
    expect(field.selectionEnd).toBe(window.location.href.length);
    expect(screen.queryByRole("button", { name: "Copied" })).not.toBeInTheDocument();
  });

  it("falls back the same way when there is no clipboard API at all", async () => {
    mockApi({ status: 200, body: { towers: [UBC_TOWER], query: UBC_QUERY, count: 1 } });
    openAt("?lat=49.2648&lon=-123.2502");
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    renderApp();

    await user.click(await screen.findByRole("button", { name: /copy link/i }));

    expect(await screen.findByLabelText(/link to this search/i)).toHaveValue(
      window.location.href,
    );
  });
});
