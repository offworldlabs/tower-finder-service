import type { Tower } from "../../types";

export type CellRadio = "GSM" | "UMTS" | "CDMA" | "LTE" | "NR";

/** One cell of a kind=cellular /api/towers answer. A site carries many cells,
 *  which share its `site_id` and position. */
export interface CellRow extends Tower {
  kind: "cellular";
  site_id: string;
  cell_id: string | null;
  operator: string | null;
  radio: CellRadio | null;
  bandwidth_mhz: number | null;
  /** Main-lobe bearing, true north. Null is an omni antenna or one the
   *  source does not record. */
  azimuth_deg: number | null;
  beamwidth_deg: number | null;
  data_source: string;
  licence: string | null;
  /** Fields whose value is a default rather than the source's own, by the
   *  API's short names: freq, bandwidth, eirp, height, azimuth, position,
   *  tilt, beamwidth. */
  modelled: string[];
  confidence: number | null;
}

/** Read through a cast, so this holds whether or not Tower declares `kind`. */
export function isCellRow(t: Tower): t is CellRow {
  return (t as { kind?: unknown }).kind === "cellular";
}

/** One map marker per site. The id alone is not enough: some sources hand
 *  every cell in an area the same id while the rows sit at several masts. */
export function siteKey(t: CellRow): string {
  return `${t.site_id}|${t.latitude}|${t.longitude}`;
}

export const DATA_SOURCE_NAMES: Record<string, string> = {
  opencellid: "OpenCelliD",
  ised: "ISED",
  acma: "ACMA",
  fcc_uls: "FCC ULS",
};

export function sourceName(source: string | null | undefined): string {
  if (!source) return "Unknown";
  return DATA_SOURCE_NAMES[source] ?? source;
}

/** Distinct non-empty values in first-seen order. */
export function distinct<T>(values: (T | null | undefined)[]): T[] {
  const seen = new Set<T>();
  for (const v of values) if (v != null && v !== "") seen.add(v);
  return [...seen];
}

export interface CellSite {
  key: string;
  latitude: number;
  longitude: number;
  /** The site's cells, best rank first. */
  cells: CellRow[];
  bestRank: number;
  operators: string[];
  sources: string[];
}

/** Rows grouped by site, in the order of each site's best-ranked cell. */
export function groupSites(towers: CellRow[]): CellSite[] {
  const byKey = new Map<string, CellRow[]>();
  for (const t of towers) {
    const key = siteKey(t);
    const cells = byKey.get(key);
    if (cells) cells.push(t);
    else byKey.set(key, [t]);
  }
  const sites: CellSite[] = [];
  for (const [key, rows] of byKey) {
    const cells = [...rows].sort((a, b) => a.rank - b.rank);
    sites.push({
      key,
      latitude: cells[0].latitude,
      longitude: cells[0].longitude,
      cells,
      bestRank: cells[0].rank,
      operators: distinct(cells.map((c) => c.operator)),
      sources: distinct(cells.map((c) => c.data_source)),
    });
  }
  return sites.sort((a, b) => a.bestRank - b.bestRank);
}
