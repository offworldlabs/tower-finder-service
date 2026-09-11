import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SearchForm from "./SearchForm";

// Waltham, MA. The old client-side bounding boxes checked Canada before the US
// along a flat 42°N line, so this coordinate — 0.387° above it — was pinned to
// "ca" and searched against Canadian ISED data.
const WALTHAM = { lat: "42.38708028093612", lon: "-71.24905416622781" };

const MATCH = {
  query: "1600 Pennsylvania Ave NW, Washington, DC",
  latitude: 38.897675,
  longitude: -77.036547,
  matched_address: "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
  provider: "census",
  precision: "street",
};

/** One canned Response, shaped the way api.ts reads one. */
function reply(status: number, body: unknown) {
  return () =>
    Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
}

// Reassigned per test, before the click that triggers the lookup.
let geocodeReply = reply(200, MATCH);

beforeEach(() => {
  vi.restoreAllMocks();
  geocodeReply = reply(200, MATCH);
  // Routed by URL rather than answered with one canned body: a successful
  // address lookup writes lat/lon, which fires the debounced elevation request
  // in turn, so both endpoints have to answer within the same test. Elevation
  // is stubbed here mainly so it can't reject noisily.
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) =>
      String(url).includes("/geocode")
        ? geocodeReply()
        : Promise.resolve({ ok: true, json: () => Promise.resolve({ elevation_m: 42 }) }),
    ),
  );
});

async function fillCoords(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/latitude/i), WALTHAM.lat);
  await user.type(screen.getByLabelText(/longitude/i), WALTHAM.lon);
}

describe("SearchForm source handling", () => {
  it("defaults to auto and leaves classification to the server", () => {
    render(<SearchForm onSearch={vi.fn()} loading={false} />);
    expect(screen.getByLabelText(/data source/i)).toHaveValue("auto");
  });

  it("does not change the source when US coordinates above 42N are entered", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await fillCoords(user);

    // The regression: this used to flip itself to "ca".
    expect(screen.getByLabelText(/data source/i)).toHaveValue("auto");
  });

  it("submits source=auto rather than a guessed country", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    render(<SearchForm onSearch={onSearch} loading={false} />);

    await fillCoords(user);
    await user.click(screen.getByRole("button", { name: /find towers/i }));

    await waitFor(() => expect(onSearch).toHaveBeenCalledTimes(1));
    expect(onSearch.mock.calls[0][0]).toMatchObject({
      lat: 42.38708028093612,
      lon: -71.24905416622781,
      source: "auto",
    });
  });

  it("still honours an explicit source the user picks", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    render(<SearchForm onSearch={onSearch} loading={false} />);

    await fillCoords(user);
    await user.selectOptions(screen.getByLabelText(/data source/i), "ca");
    await user.click(screen.getByRole("button", { name: /find towers/i }));

    await waitFor(() => expect(onSearch).toHaveBeenCalledTimes(1));
    expect(onSearch.mock.calls[0][0].source).toBe("ca");
  });

  it("offers auto alongside the three supported regions", () => {
    render(<SearchForm onSearch={vi.fn()} loading={false} />);
    const values = Array.from(
      screen.getByLabelText(/data source/i).querySelectorAll("option"),
    ).map((o) => (o as HTMLOptionElement).value);
    expect(values).toEqual(["auto", "us", "ca", "au"]);
  });
});

describe("SearchForm measured frequencies", () => {
  it("keeps the frequency inputs out of the way until they are asked for", () => {
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    expect(
      screen.getByRole("button", { name: /add measured frequencies/i }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/^frequency 1/i)).not.toBeInTheDocument();
  });

  it("passes an entered frequency to the search", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    render(<SearchForm onSearch={onSearch} loading={false} />);

    await fillCoords(user);
    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.type(screen.getByLabelText(/^frequency 1/i), "95.5");
    await user.click(screen.getByRole("button", { name: /find towers/i }));

    await waitFor(() => expect(onSearch).toHaveBeenCalledTimes(1));
    expect(onSearch.mock.calls[0][0].frequencies).toEqual([95.5]);
  });
  it("adds another frequency row on request", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.click(screen.getByRole("button", { name: /add frequency/i }));

    expect(screen.getByLabelText(/^frequency 2/i)).toBeInTheDocument();
  });

  it("offers no way to remove the only row, and one per row after that", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    expect(screen.queryByRole("button", { name: /remove frequency/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /add frequency/i }));
    expect(screen.getAllByRole("button", { name: /remove frequency/i })).toHaveLength(2);
  });

  it("removes the row the operator asked to remove, keeping the others' values", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.click(screen.getByRole("button", { name: /add frequency/i }));
    await user.type(screen.getByLabelText(/^frequency 1/i), "95.5");
    await user.type(screen.getByLabelText(/^frequency 2/i), "101.1");

    await user.click(screen.getByRole("button", { name: /remove frequency 1/i }));

    expect(screen.getByLabelText(/^frequency 1/i)).toHaveValue(101.1);
    expect(screen.queryByLabelText(/^frequency 2/i)).not.toBeInTheDocument();
  });

  it("stops offering new rows at ten", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    for (let i = 0; i < 9; i++) {
      await user.click(screen.getByRole("button", { name: /add frequency/i }));
    }

    expect(screen.getByLabelText(/^frequency 10/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add frequency/i })).not.toBeInTheDocument();
  });
  it("sends no frequencies when the section was never opened", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    render(<SearchForm onSearch={onSearch} loading={false} />);

    await fillCoords(user);
    await user.click(screen.getByRole("button", { name: /find towers/i }));

    await waitFor(() => expect(onSearch).toHaveBeenCalledTimes(1));
    expect(onSearch.mock.calls[0][0].frequencies).toEqual([]);
  });

  it("leaves a blank row out of the search rather than sending it", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    render(<SearchForm onSearch={onSearch} loading={false} />);

    await fillCoords(user);
    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.click(screen.getByRole("button", { name: /add frequency/i }));
    await user.type(screen.getByLabelText(/^frequency 1/i), "95.5");
    // Frequency 2 is left blank.
    await user.click(screen.getByRole("button", { name: /find towers/i }));

    await waitFor(() => expect(onSearch).toHaveBeenCalledTimes(1));
    expect(onSearch.mock.calls[0][0].frequencies).toEqual([95.5]);
  });

  it("drops a zero, which parse_user_frequencies would reject anyway", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    render(<SearchForm onSearch={onSearch} loading={false} />);

    await fillCoords(user);
    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.type(screen.getByLabelText(/^frequency 1/i), "0");
    await user.click(screen.getByRole("button", { name: /find towers/i }));

    await waitFor(() => expect(onSearch).toHaveBeenCalledTimes(1));
    expect(onSearch.mock.calls[0][0].frequencies).toEqual([]);
  });
  it("says how many frequencies are set once the section is collapsed", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.click(screen.getByRole("button", { name: /add frequency/i }));
    await user.type(screen.getByLabelText(/^frequency 1/i), "95.5");
    await user.type(screen.getByLabelText(/^frequency 2/i), "101.1");
    await user.click(screen.getByRole("button", { name: /hide measured frequencies/i }));

    // Collapsed values are still submitted, so the count has to stay on screen:
    // otherwise the ranking shifts with nothing visible to explain it.
    expect(screen.queryByLabelText(/^frequency 1/i)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /measured frequencies \(2\)/i }),
    ).toBeInTheDocument();
  });

  it("bounds each input to the range the server will accept", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));

    // parse_user_frequencies keeps 0 < val < 10000 and drops the rest in silence.
    expect(screen.getByLabelText(/^frequency 1/i)).toHaveAttribute("max", "10000");
  });

  it("refuses to search on a frequency the server would discard", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    render(<SearchForm onSearch={onSearch} loading={false} />);

    await fillCoords(user);
    await user.click(screen.getByRole("button", { name: /add measured frequencies/i }));
    await user.type(screen.getByLabelText(/^frequency 1/i), "12000");
    await user.click(screen.getByRole("button", { name: /find towers/i }));

    // The bound is what makes this visible. Without it the search goes ahead,
    // parse_user_frequencies drops the value, and the ranking comes back
    // unchanged with nothing said.
    expect(onSearch).not.toHaveBeenCalled();
    const input = screen.getByLabelText(/^frequency 1/i) as HTMLInputElement;
    expect(input.validity.rangeOverflow).toBe(true);
  });
});

describe("SearchForm address lookup", () => {
  const ADDRESS = "1600 Pennsylvania Ave NW, Washington, DC";

  async function lookUp(user: ReturnType<typeof userEvent.setup>, text = ADDRESS) {
    await user.type(screen.getByLabelText(/^address/i), text);
    await user.click(screen.getByRole("button", { name: /look up/i }));
  }

  it("fills the coordinates from the match and names what it matched", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await lookUp(user);

    await waitFor(() =>
      expect(screen.getByLabelText(/latitude/i)).toHaveValue(MATCH.latitude),
    );
    expect(screen.getByLabelText(/longitude/i)).toHaveValue(MATCH.longitude);
    expect(screen.getByText(`Matched: ${MATCH.matched_address}`)).toBeInTheDocument();
  });

  it("looks the address up on Enter instead of submitting the search", async () => {
    const user = userEvent.setup();
    const onSearch = vi.fn();
    render(<SearchForm onSearch={onSearch} loading={false} />);

    // Coordinates first: without them handleSubmit bails on its own and an
    // accidental submit would go unnoticed here.
    await fillCoords(user);
    await user.type(screen.getByLabelText(/^address/i), `${ADDRESS}{Enter}`);

    expect(await screen.findByText(/^Matched:/)).toBeInTheDocument();
    expect(onSearch).not.toHaveBeenCalled();
  });

  it("shows the server's wording when the address has no match", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);
    geocodeReply = reply(404, { detail: "No match for that address" });

    await lookUp(user, "nowhere at all");

    expect(await screen.findByText("No match for that address")).toBeInTheDocument();
    expect(screen.getByLabelText(/latitude/i)).toHaveValue(null);
  });

  it("passes on the outage message when the geocoder is down", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);
    geocodeReply = reply(503, { detail: "Address lookup is unavailable right now" });

    await lookUp(user);

    expect(
      await screen.findByText("Address lookup is unavailable right now"),
    ).toBeInTheDocument();
  });

  it("warns that a locality match is only the city centre", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);
    geocodeReply = reply(200, {
      ...MATCH,
      matched_address: "Washington, DC",
      provider: "nominatim",
      precision: "locality",
    });

    await lookUp(user, "Washington DC");

    // 2 km ranking cells and a horizon check: a centroid this coarse can change
    // which towers come back, so it cannot be shown as a plain match.
    expect(await screen.findByText(/City centre only/i)).toBeInTheDocument();
  });

  it("drops the confirmation as soon as the address is edited", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await lookUp(user);
    expect(await screen.findByText(/^Matched:/)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/^address/i), " suite 2");

    // The coordinates stay — they are still the last thing looked up — but the
    // line claiming they belong to what is now in the box does not.
    expect(screen.queryByText(/^Matched:/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/latitude/i)).toHaveValue(MATCH.latitude);
  });

  it("drops the confirmation when the coordinates are edited by hand", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);

    await lookUp(user);
    expect(await screen.findByText(/^Matched:/)).toBeInTheDocument();

    await user.type(screen.getByLabelText(/latitude/i), "1");

    expect(screen.queryByText(/^Matched:/)).not.toBeInTheDocument();
  });

  it("offers no lookup until something is typed", async () => {
    const user = userEvent.setup();
    render(<SearchForm onSearch={vi.fn()} loading={false} />);
    const button = screen.getByRole("button", { name: /look up/i });

    expect(button).toBeDisabled();

    // Whitespace is not an address; the server would 422 it.
    await user.type(screen.getByLabelText(/^address/i), "   ");
    expect(button).toBeDisabled();

    await user.type(screen.getByLabelText(/^address/i), "1600");
    expect(button).toBeEnabled();
  });

  it("caps the address at the length the server accepts", () => {
    render(<SearchForm onSearch={vi.fn()} loading={false} />);
    expect(screen.getByLabelText(/^address/i)).toHaveAttribute("maxlength", "200");
  });
});
