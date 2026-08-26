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
  distance_class: "Ideal",
  bearing_deg: 145,
  bearing_cardinal: "SE",
  received_power_dbm: -62.1,
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
});
