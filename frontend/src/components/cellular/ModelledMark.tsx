import "./cellular.css";
import type { CellRow } from "./types";

export const MODELLED_LEGEND =
  "≈ modelled: estimated from defaults where the source record has no value";

/** The API's name in `modelled` for each value the page shows. Tilt is never
 *  shown, so it has no entry. */
const MODELLED_FIELD = {
  frequency_mhz: "freq",
  bandwidth_mhz: "bandwidth",
  eirp_dbm: "eirp",
  antenna_height_m: "height",
  azimuth_deg: "azimuth",
  beamwidth_deg: "beamwidth",
  position: "position",
} as const;

export type ShownField = keyof typeof MODELLED_FIELD;

export function isModelled(row: CellRow, field: ShownField): boolean {
  return Array.isArray(row.modelled) && row.modelled.includes(MODELLED_FIELD[field]);
}

/** A superscript "≈" after a value the source did not supply. */
export default function ModelledMark() {
  return (
    <sup className="cell-modelled" title="modelled" aria-label="modelled" role="img">
      ≈
    </sup>
  );
}

/** The mark after `field`'s value when that field is modelled on this row. */
export function MarkIf({ row, field }: { row: CellRow; field: ShownField }) {
  return isModelled(row, field) ? <ModelledMark /> : null;
}

export function ModelledLegend({ className }: { className?: string }) {
  return (
    <p className={className ? `cell-modelled-legend ${className}` : "cell-modelled-legend"}>
      {MODELLED_LEGEND}
    </p>
  );
}
