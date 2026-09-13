import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchTowers, geocodeAddress } from "./api";

function stubFetch() {
  // The url parameter is declared so the mock's call tuple is typed and the
  // assertions below can read it back.
  const mock = vi.fn((_url: string) =>
    Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ towers: [], query: {}, count: 0 }),
    }),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

function requestedUrl(mock: ReturnType<typeof stubFetch>) {
  // Relative URL, so give it a base the URL parser will accept.
  return new URL(mock.mock.calls[0][0], "https://towers.invalid");
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("fetchTowers frequencies", () => {
  // One comma-separated string is the spelling the SPA sends. The route also
  // accepts the key repeated, so this pins our choice of the two, not the only
  // form the server understands.
  it("sends the frequencies as a single comma-separated parameter", async () => {
    const mock = stubFetch();

    await fetchTowers(42.38, -71.24, 0, 20, "auto", [95.5, 101.1]);

    expect(requestedUrl(mock).searchParams.getAll("frequencies")).toEqual(["95.5,101.1"]);
  });

  it("omits the parameter entirely when no frequencies were entered", async () => {
    const mock = stubFetch();

    await fetchTowers(42.38, -71.24, 0, 20, "auto", []);

    expect(requestedUrl(mock).searchParams.has("frequencies")).toBe(false);
  });
});

describe("geocodeAddress", () => {
  function stubGeocode(status: number, body: unknown) {
    const mock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) }),
    );
    vi.stubGlobal("fetch", mock);
    return mock;
  }

  it("POSTs the address as JSON rather than putting it in the URL", async () => {
    const mock = stubGeocode(200, { latitude: 38.9, longitude: -77.0 });

    await geocodeAddress("1600 Pennsylvania Ave NW");

    const [url, init] = mock.mock.calls[0];
    expect(url).toBe("/api/geocode");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toMatchObject({ "Content-Type": "application/json" });
    expect(JSON.parse(init?.body as string)).toEqual({
      query: "1600 Pennsylvania Ave NW",
    });
  });

  it("throws the server's detail so the form can show its wording", async () => {
    stubGeocode(404, { detail: "No match for that address" });

    await expect(geocodeAddress("nowhere")).rejects.toThrow("No match for that address");
  });

  it("falls back to the status when detail is pydantic's array of errors", async () => {
    // 422 answers with [{loc, msg, type}, …], which is not a sentence to show.
    stubGeocode(422, { detail: [{ loc: ["body", "query"], msg: "too short" }] });

    await expect(geocodeAddress("")).rejects.toThrow("Request failed (422)");
  });
});
