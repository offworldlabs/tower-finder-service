import { describe, it, expect } from "vitest";
import { RANK_TIERS, rankTier } from "./rankTier";

describe("rankTier", () => {
  it("rank 1 of 1 is the top tier", () => {
    expect(rankTier(1, 1).tier).toBe(1);
  });

  it("rank 4 of 20 is the top tier", () => {
    expect(rankTier(4, 20).tier).toBe(1);
  });

  it("rank 5 of 20 crosses into the second tier", () => {
    expect(rankTier(5, 20).tier).toBe(2);
  });

  it("rank 20 of 20 is the bottom tier", () => {
    expect(rankTier(20, 20).tier).toBe(5);
  });

  it("rank 2 of 2 lands in the middle tier", () => {
    expect(rankTier(2, 2).tier).toBe(3);
  });

  it("total 0 falls back to the top tier", () => {
    expect(rankTier(1, 0).tier).toBe(1);
  });

  it("rank 100 of 100 is the bottom tier", () => {
    expect(rankTier(100, 100).tier).toBe(5);
  });

  it("rejects non-finite input by returning the top tier", () => {
    expect(rankTier(NaN, 20).tier).toBe(1);
    expect(rankTier(1, Infinity).tier).toBe(1);
    expect(rankTier(0, 20).tier).toBe(1);
  });

  it("every tier has a distinct color", () => {
    const colors = new Set(RANK_TIERS.map((t) => t.color));
    expect(colors.size).toBe(RANK_TIERS.length);
  });

  it("labels are in order from top to bottom", () => {
    expect(RANK_TIERS.map((t) => t.label)).toEqual(["Top 20%", "20–40%", "40–60%", "60–80%", "Bottom 20%"]);
  });
});
