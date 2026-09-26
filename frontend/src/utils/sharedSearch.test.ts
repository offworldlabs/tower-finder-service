import { describe, expect, it } from "vitest";
import { normaliseSource, readSharedSearch, sharedSearchQuery } from "./sharedSearch";
import type { SearchRequest } from "../types";

const UBC: SearchRequest = {
  lat: 49.2648,
  lon: -123.2502,
  altitude: 95,
  altitudeSet: true,
  source: "auto",
  frequencies: [99.9, 102.1],
};

describe("sharedSearchQuery", () => {
  it("writes the documented spelling, with a literal comma between frequencies", () => {
    expect(sharedSearchQuery(UBC)).toBe("?lat=49.2648&lon=-123.2502&alt=95&f=99.9,102.1");
  });

  it("omits an altitude the operator did not set, auto, and no frequencies", () => {
    expect(
      sharedSearchQuery({ ...UBC, altitude: 43, altitudeSet: false, frequencies: [] }),
    ).toBe("?lat=49.2648&lon=-123.2502");
  });

  it("names an explicit source", () => {
    expect(sharedSearchQuery({ ...UBC, source: "ca" })).toContain("&source=ca&");
  });

  it("replaces its own keys and keeps anyone else's", () => {
    expect(sharedSearchQuery(UBC, "?utm_source=chat&lat=1&f=5&source=us")).toBe(
      "?lat=49.2648&lon=-123.2502&alt=95&f=99.9,102.1&utm_source=chat",
    );
  });

  it("encodes an exponent's plus, which a query string would read as a space", () => {
    const query = sharedSearchQuery({ ...UBC, altitude: 1e21 });
    expect(query).toContain("alt=1e%2B21");
    expect(readSharedSearch(query).request?.altitude).toBe(1e21);
  });
});

describe("readSharedSearch", () => {
  it("round-trips what sharedSearchQuery writes", () => {
    for (const req of [
      UBC,
      { ...UBC, source: "au", frequencies: [] },
      { ...UBC, altitude: 0, altitudeSet: false },
    ]) {
      expect(readSharedSearch(sharedSearchQuery(req)).request).toEqual(req);
    }
  });

  it("accepts a percent-encoded comma between frequencies", () => {
    expect(readSharedSearch("?lat=1&lon=2&f=99.9%2C102.1").request?.frequencies).toEqual([
      99.9, 102.1,
    ]);
  });

  it("asks nothing of an empty query string", () => {
    expect(readSharedSearch("")).toEqual({ initial: {}, request: null });
  });

  it("takes the coordinate bounds inclusive, as the inputs' min and max are", () => {
    expect(readSharedSearch("?lat=-90&lon=180").request).toMatchObject({ lat: -90, lon: 180 });
    expect(readSharedSearch("?lat=90.0001&lon=0").request).toBeNull();
    expect(readSharedSearch("?lat=0&lon=-180.5").request).toBeNull();
  });

  it("does not read a number out of the front of a word", () => {
    // parseFloat would take this as 49.
    const { initial, request } = readSharedSearch("?lat=49abc&lon=2");
    expect(request).toBeNull();
    expect(initial.lat).toBeUndefined();
    expect(initial.lon).toBe("2");
  });

  it("rejects hex, blanks and infinities", () => {
    for (const lat of ["0x10", "", " ", "Infinity", "NaN"]) {
      expect(readSharedSearch(`?lat=${lat}&lon=2`).request).toBeNull();
    }
  });

  it("drops frequencies the form would drop, and keeps at most ten", () => {
    const f = ["abc", "0", "-5", "10000", "12000", ...Array.from({ length: 12 }, (_, i) => 90 + i)];
    const { request, initial } = readSharedSearch(`?lat=1&lon=2&f=${f.join(",")}`);
    expect(request?.frequencies).toEqual([90, 91, 92, 93, 94, 95, 96, 97, 98, 99]);
    expect(initial.frequencies).toHaveLength(10);
  });

  it("runs with altitude 0 and marks it unset when the link has no alt", () => {
    expect(readSharedSearch("?lat=1&lon=2").request).toMatchObject({
      altitude: 0,
      altitudeSet: false,
    });
  });

  it("holds the search back when an alt is there but unusable", () => {
    const { initial, request } = readSharedSearch("?lat=1&lon=2&alt=-1");
    expect(request).toBeNull();
    expect(initial).toEqual({ lat: "1", lon: "2" });
  });
});

describe("normaliseSource", () => {
  it.each([
    ["auto", "auto"],
    ["US", "us"],
    ["Ca", "ca"],
    ["au", "au"],
    ["uk", "auto"],
    [null, "auto"],
    [undefined, "auto"],
  ])("%j -> %j", (given, expected) => {
    expect(normaliseSource(given)).toBe(expected);
  });
});
