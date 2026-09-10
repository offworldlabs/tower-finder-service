import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchTowers } from "./api";

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
