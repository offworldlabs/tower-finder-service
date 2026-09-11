/* ------------------------------------------------------------------ */
/*  Display formatting for the new ranking fields                     */
/* ------------------------------------------------------------------ */

/** 16-point compass, mirroring `bearing_to_cardinal` in
 *  backend/services/tower_ranking.py. The server sends a cardinal for
 *  `bearing_deg` but not for `best_azimuth_deg`, so the pointing advice is
 *  named client-side from the same table — keep the two in step. */
const COMPASS_POINTS = [
  "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
];

/**
 * Compass point for a true-north bearing in degrees, e.g. 240 → "WSW".
 *
 * Bearings outside 0–360 are wrapped rather than rejected, so a 370° or a
 * negative azimuth still names a real direction. Returns "" for input that
 * isn't a number at all, which is how an older backend's missing field reads.
 */
export function bearingCardinal(deg: number | null | undefined): string {
  if (deg == null || !Number.isFinite(deg)) return "";
  const wrapped = ((deg % 360) + 360) % 360;
  return COMPASS_POINTS[Math.round(wrapped / 22.5) % 16];
}

const AREA_FORMAT = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 });

/**
 * Detectable area in km², to the nearest whole km² with thousands separators
 * (16900.4 → "16,900"). Absent values render as "" — these fields are additive
 * and an older backend simply omits them, which must leave a blank cell rather
 * than a "NaN" or a placeholder implying a measured zero.
 */
export function formatAreaKm2(km2: number | null | undefined): string {
  if (km2 == null || !Number.isFinite(km2)) return "";
  return AREA_FORMAT.format(km2);
}

/**
 * True when the tower sits past its own radio horizon — the backend penalises
 * these heavily, so the row is muted rather than dropped: an operator who can
 * see why a known-strong transmitter ranks low learns more than one who finds
 * it missing.
 */
export function beyondHorizon(distanceKm: number, horizonKm: number | null | undefined): boolean {
  if (horizonKm == null || !Number.isFinite(horizonKm) || !Number.isFinite(distanceKm)) return false;
  return distanceKm > horizonKm;
}
