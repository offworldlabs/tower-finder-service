import "./cellular.css";
import CopyLink from "../CopyLink";
import { formatAreaKm2 } from "../../utils/format";
import BandChip from "./BandChip";
import { compareBands } from "./bands";
import { distinct, siteKey, sourceName, type CellRow } from "./types";

const REGION_LABELS: Record<string, string> = {
  us: "United States",
  ca: "Canada",
  au: "Australia",
};

interface Props {
  towers: CellRow[];
  query: { source: string; radius_km: number } | null;
  share: boolean;
}

/** The summary strip of the cellular version: the same cards and classes as
 *  the broadcast one, counted in cells, sites, operators and bands. */
export default function CellularSummary({ towers, query, share }: Props) {
  if (!towers.length) return null;

  const best = towers[0];
  const sites = new Set(towers.map(siteKey)).size;
  const operators = distinct(towers.map((t) => t.operator));
  const bands = distinct(towers.map((t) => t.band)).sort(compareBands);
  const sources = distinct(towers.map((t) => t.data_source)).map(sourceName);
  const region = query ? REGION_LABELS[query.source] : undefined;

  return (
    <div className="summary-strip cell-summary">
      <div className="card stat-card cell-stat-wide">
        <span className="stat-value cell-stat-pick">
          <BandChip band={best.band} radio={best.radio} />
          <span className="cell-stat-pick-name" title={best.operator ?? undefined}>
            {best.operator || "Unknown operator"}
          </span>
        </span>
        <span className="label stat-label">
          Top Pick — {best.distance_km} km
          {best.expected_area_km2 != null && ` · ${formatAreaKm2(best.expected_area_km2)} km²`}
        </span>
      </div>
      <div className="card stat-card cell-stat-compact">
        <span className="stat-value">{towers.length}</span>
        <span className="label stat-label">Cells Found</span>
      </div>
      <div className="card stat-card cell-stat-compact">
        <span className="stat-value">{sites}</span>
        <span className="label stat-label">Sites</span>
      </div>
      <div
        className="card stat-card cell-stat-compact"
        title={operators.length ? operators.join("\n") : undefined}
      >
        <span className="stat-value">{operators.length}</span>
        <span className="label stat-label">Operators</span>
      </div>
      <div className="card stat-card cell-stat-bands">
        <span className="stat-value cell-stat-chips">
          {bands.map((b) => (
            <BandChip key={b} band={b} />
          ))}
        </span>
        <span className="label stat-label">Bands</span>
      </div>
      {query && (
        <div className="card stat-card cell-stat-region">
          <span className="stat-value">{query.source.toUpperCase()}</span>
          <span className="label stat-label">
            {region || "Data Source"}
            {sources.length > 0 && ` · ${sources.join(", ")}`}
          </span>
        </div>
      )}
      {share && (
        <div className="card stat-card share-card cell-stat-region">
          <CopyLink />
          <span className="label stat-label">Share this search</span>
        </div>
      )}
    </div>
  );
}
