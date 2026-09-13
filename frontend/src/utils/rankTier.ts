export interface RankTier {
  tier: 1 | 2 | 3 | 4 | 5;
  label: string;
  color: string;
  bg: string;
}

export const RANK_TIERS: RankTier[] = [
  { tier: 1, label: "Best", color: "#16a34a", bg: "rgba(22,163,74,0.12)" },
  { tier: 2, label: "Upper", color: "#65a30d", bg: "rgba(101,163,13,0.12)" },
  { tier: 3, label: "Middle", color: "#ca8a04", bg: "rgba(202,138,4,0.12)" },
  { tier: 4, label: "Lower", color: "#ea580c", bg: "rgba(234,88,12,0.12)" },
  { tier: 5, label: "Worst", color: "#94a3b8", bg: "rgba(148,163,184,0.16)" },
];

/**
 * Quintile of a tower's position within the returned result set.
 *
 * The tier is relative to whatever list came back, not an absolute score —
 * with a short list the five colours still spread across the towers present
 * (e.g. rank 2 of 2 lands in the middle tier), rather than everything
 * clustering into "Best" because the list happened to be short.
 */
export function rankTier(rank: number, total: number): RankTier {
  if (!Number.isFinite(rank) || !Number.isFinite(total) || total < 1 || rank < 1) {
    return RANK_TIERS[0];
  }
  const p = (rank - 1) / total;
  const tier = Math.min(5, Math.max(1, Math.floor(p * 5) + 1));
  return RANK_TIERS[tier - 1];
}
