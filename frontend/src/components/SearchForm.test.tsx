import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SearchForm from "./SearchForm";

// Waltham, MA. The old client-side bounding boxes checked Canada before the US
// along a flat 42°N line, so this coordinate — 0.387° above it — was pinned to
// "ca" and searched against Canadian ISED data.
const WALTHAM = { lat: "42.38708028093612", lon: "-71.24905416622781" };

beforeEach(() => {
  vi.restoreAllMocks();
  // Elevation auto-fill is debounced 400ms; stub it so it can't reject noisily.
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ elevation_m: 42 }) }),
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
