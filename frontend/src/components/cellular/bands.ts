/**
 * Cellular band groups, named by their nominal frequency. The ranges are the
 * downlink edges the backend assigns a cell's frequency to its band by.
 */

export type BandGroup = "low" | "mid" | "upper" | "cband" | "unknown";

interface BandInfo {
  /** Downlink edges, MHz. */
  low: number;
  high: number;
  group: BandGroup;
}

const BANDS: Record<string, BandInfo> = {
  CELL600: { low: 617, high: 652, group: "low" },
  CELL700: { low: 716, high: 803, group: "low" },
  CELL850: { low: 859, high: 894, group: "low" },
  CELL900: { low: 925, high: 960, group: "low" },
  CELL1800: { low: 1805, high: 1880, group: "mid" },
  CELL1900: { low: 1930, high: 1995, group: "mid" },
  CELL2100: { low: 2110, high: 2200, group: "mid" },
  CELL2300: { low: 2300, high: 2400, group: "upper" },
  CELL2600: { low: 2496, high: 2690, group: "upper" },
  CELL3500: { low: 3300, high: 4200, group: "cband" },
};

/** "CELL2100" → "2100". A band this table does not know keeps its own name. */
export function bandNumber(band: string | null | undefined): string {
  if (!band) return "—";
  const m = /^CELL(\d+)$/.exec(band);
  return m ? m[1] : band;
}

export function bandGroup(band: string | null | undefined): BandGroup {
  return (band && BANDS[band]?.group) || "unknown";
}

/** Radio and band number, "LTE 2100", or the number alone with no radio. */
export function bandLabel(radio: string | null | undefined, band: string | null | undefined): string {
  const n = bandNumber(band);
  return radio ? `${radio} ${n}` : n;
}

/** "CELL2100 · 2110-2200 MHz downlink". */
export function bandTitle(band: string | null | undefined): string {
  const info = band ? BANDS[band] : undefined;
  if (!info) return band || "Unknown band";
  return `${band} · ${info.low}-${info.high} MHz downlink`;
}

/** Low frequency first; bands the table does not know go last, by name. */
export function compareBands(a: string, b: string): number {
  const fa = BANDS[a]?.low ?? Infinity;
  const fb = BANDS[b]?.low ?? Infinity;
  if (fa !== fb) return fa - fb;
  return a < b ? -1 : a > b ? 1 : 0;
}
