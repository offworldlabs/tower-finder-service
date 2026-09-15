export interface RankTier {
  tier: 1 | 2 | 3 | 4 | 5;
  label: string;
  color: string;
  bg: string;
}

/**
 * Custom properties rather than literals, so the ramp brightens with the dark
 * theme; the values live in surface.css. Every consumer puts these into a CSS
 * declaration — a React `style` prop, or the inline style of a Leaflet
 * `divIcon`'s HTML — where `var()` resolves. A Leaflet `pathOptions` colour
 * does not qualify: that lands in an SVG presentation attribute, which
 * substitution does not reach, so a themed vector takes a class instead.
 */
export const RANK_TIERS: RankTier[] = [
  { tier: 1, label: "Best", color: "var(--rank-1)", bg: "var(--rank-1-wash)" },
  { tier: 2, label: "Upper", color: "var(--rank-2)", bg: "var(--rank-2-wash)" },
  { tier: 3, label: "Middle", color: "var(--rank-3)", bg: "var(--rank-3-wash)" },
  { tier: 4, label: "Lower", color: "var(--rank-4)", bg: "var(--rank-4-wash)" },
  { tier: 5, label: "Worst", color: "var(--rank-5)", bg: "var(--rank-5-wash)" },
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
