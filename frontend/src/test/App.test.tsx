import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";

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

function towersRequestUrl(): string | undefined {
  const mock = globalThis.fetch as unknown as { mock: { calls: [string][] } };
  return mock.mock.calls.map((c) => c[0]).find((u) => u.includes("/api/towers"));
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("App", () => {
  it("mounts and renders the header and search form", () => {
    mockApi({ status: 200, body: { towers: [], query: null, count: 0 } });
    render(<App />);
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
    render(<App />);
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

  it("shows the server's detail message when the region is unsupported", async () => {
    mockApi({
      status: 422,
      body: { detail: "Location is not in a supported region (US, CA, AU)." },
    });
    const user = userEvent.setup();
    render(<App />);
    await search(user);

    await waitFor(() =>
      expect(document.querySelector(".error-banner")).toHaveTextContent(
        /not in a supported region/i,
      ),
    );
  });
  // The seam this ticket exists to close: the form collects frequencies, App
  // forwards them, api.ts serialises them. A break anywhere in that chain is a
  // 200 with the parameter silently absent.
  it("carries entered frequencies through to the tower request", async () => {
    mockApi({ status: 200, body: { towers: [], query: null, count: 0 } });
    const user = userEvent.setup();
    render(<App />);

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
    render(<App />);
    await search(user);

    await waitFor(() => expect(document.querySelectorAll("tbody tr")).toHaveLength(2));
    expect(screen.getByRole("cell", { name: /Top 20%/ })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: /40–60%/ })).toBeInTheDocument();
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
    render(<App />);
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
    render(<App />);
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
    render(<App />);
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
