import { describe, expect, it } from "vitest";
import { bearingCardinal, beyondHorizon, formatAreaKm2 } from "./format";

describe("bearingCardinal", () => {
  it("names the four cardinals", () => {
    expect(bearingCardinal(0)).toBe("N");
    expect(bearingCardinal(90)).toBe("E");
    expect(bearingCardinal(180)).toBe("S");
    expect(bearingCardinal(270)).toBe("W");
  });

  it("names the 16-point intercardinals the backend uses", () => {
    expect(bearingCardinal(240)).toBe("WSW");
    expect(bearingCardinal(22.5)).toBe("NNE");
    expect(bearingCardinal(112.5)).toBe("ESE");
    expect(bearingCardinal(337.5)).toBe("NNW");
  });

  it("wraps back to N just below 360, as bearing_to_cardinal does", () => {
    expect(bearingCardinal(359)).toBe("N");
    expect(bearingCardinal(360)).toBe("N");
    expect(bearingCardinal(370)).toBe("N");
    expect(bearingCardinal(-10)).toBe("N");
  });

  it("returns an empty string when the field is absent", () => {
    expect(bearingCardinal(null)).toBe("");
    expect(bearingCardinal(undefined)).toBe("");
    expect(bearingCardinal(NaN)).toBe("");
  });
});

describe("formatAreaKm2", () => {
  it("groups thousands and drops decimals", () => {
    expect(formatAreaKm2(16900.4)).toBe("16,900");
    expect(formatAreaKm2(1234567)).toBe("1,234,567");
    expect(formatAreaKm2(42)).toBe("42");
  });

  it("keeps a real zero visible", () => {
    expect(formatAreaKm2(0)).toBe("0");
  });

  it("renders nothing when the field is absent", () => {
    expect(formatAreaKm2(null)).toBe("");
    expect(formatAreaKm2(undefined)).toBe("");
    expect(formatAreaKm2(NaN)).toBe("");
  });
});

describe("beyondHorizon", () => {
  it("is true only past the horizon distance", () => {
    expect(beyondHorizon(200, 180)).toBe(true);
    expect(beyondHorizon(180, 180)).toBe(false);
    expect(beyondHorizon(12.3, 180)).toBe(false);
  });

  it("is false when the backend omits horizon_km", () => {
    expect(beyondHorizon(200, null)).toBe(false);
    expect(beyondHorizon(200, undefined)).toBe(false);
  });
});
