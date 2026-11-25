import "../ResultsTable.css";
import "./cellular.css";
import { rankTier } from "../../utils/rankTier";
import { bearingCardinal, beyondHorizon, formatAreaKm2 } from "../../utils/format";
import BandChip from "./BandChip";
import { MarkIf, ModelledLegend } from "./ModelledMark";
import { SectorArrow, formatDegrees } from "./Sector";
import { siteKey, sourceName, type CellRow } from "./types";

interface Props {
  towers: CellRow[];
  onHover: (t: CellRow | null) => void;
}

/**
 * One row per cell, in the order served. What the cell is comes first (rank,
 * band and callsign, operator and the register it is listed in, sector),
 * then what the rank is made of (detect area, where to point), the signal,
 * and the geometry; a second value that belongs to a column sits on a muted
 * line under its first, and a header names both.
 * Numbers are right-aligned, with the modelled mark hung in the padding so
 * it never pushes a figure out of line with its neighbours.
 *
 * The table scrolls inside its own bounded box, both ways, with the header
 * row on top and the rank and cell columns pinned to the left edge, so a row
 * can still be told apart when scrolled out to its far columns.
 */
export default function CellularResultsTable({ towers, onHover }: Props) {
  const sites = new Set(towers.map(siteKey)).size;

  return (
    <div className="card results-wrap cell-results">
      <h2>
        Cells <span className="results-count">{towers.length}</span>
        <span className="cell-results-sites">
          across {sites} {sites === 1 ? "site" : "sites"}
        </span>
      </h2>
      <ModelledLegend />
      <div className="table-scroll cell-table-scroll">
        <table className="results-table cell-table">
          <thead>
            <tr>
              <th className="cell-col-rank">#</th>
              <th className="cell-col-cell">Cell</th>
              <th>
                Operator <span className="cell-th-sub">Source</span>
              </th>
              <th>Sector</th>
              <th className="cell-num">
                Detect Area <span className="cell-th-sub">(km&sup2;)</span>
              </th>
              <th>Point</th>
              <th className="cell-num cell-num-marked">
                Freq <span className="cell-th-sub">(MHz)</span>
              </th>
              <th className="cell-num">
                Rx Power <span className="cell-th-sub">(dBm)</span>
              </th>
              <th className="cell-num cell-num-marked">
                EIRP <span className="cell-th-sub">(dBm)</span>
              </th>
              <th className="cell-num cell-num-marked">
                Ant. Height <span className="cell-th-sub">(m)</span>
              </th>
              <th className="cell-num">
                Distance (km) <span className="cell-th-sub">Bearing</span>
              </th>
              <th className="cell-num cell-num-marked">Location</th>
              <th>
                Rank <span className="cell-th-sub">Tier</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {towers.map((t) => {
              const tier = rankTier(t.rank, towers.length);
              const overHorizon = beyondHorizon(t.distance_km, t.horizon_km);
              return (
                <tr
                  key={`${t.rank}|${siteKey(t)}|${t.callsign}|${t.frequency_mhz}`}
                  className={overHorizon ? "beyond-horizon" : undefined}
                  title={overHorizon ? "Beyond radio horizon" : undefined}
                  onMouseEnter={() => onHover(t)}
                  onMouseLeave={() => onHover(null)}
                >
                  <td className="rank cell-col-rank">
                    <span className="rank-badge" style={{ color: tier.color, background: tier.bg }}>
                      {t.rank}
                    </span>
                  </td>
                  <td className="cell-col-cell">
                    <BandChip band={t.band} radio={t.radio} size="sm" />
                    <span className="mono cell-callsign" title={t.callsign ?? undefined}>
                      {t.callsign}
                    </span>
                  </td>
                  <td className="cell-operator">
                    <span className="cell-operator-name" title={t.operator ?? undefined}>
                      {t.operator ?? <span className="cell-muted">Unknown</span>}
                    </span>{" "}
                    <span className="cell-sub cell-source">{sourceName(t.data_source)}</span>
                  </td>
                  <td className="cell-sector">
                    {t.azimuth_deg != null ? (
                      <span className="cell-sector-main">
                        <SectorArrow deg={t.azimuth_deg} />
                        <span>
                          {formatDegrees(t.azimuth_deg)}
                          <MarkIf row={t} field="azimuth_deg" />{" "}
                          <span className="cardinal">{bearingCardinal(t.azimuth_deg)}</span>
                        </span>
                      </span>
                    ) : (
                      <span className="cell-muted">Omni / unknown</span>
                    )}
                    {t.beamwidth_deg != null && (
                      <>
                        {" "}
                        <span className="cell-sub">
                          {formatDegrees(t.beamwidth_deg)} wide
                          <MarkIf row={t} field="beamwidth_deg" />
                        </span>
                      </>
                    )}
                  </td>
                  <td className="mono detect-area cell-num">{formatAreaKm2(t.expected_area_km2)}</td>
                  <td className="point">
                    {t.best_azimuth_deg != null && (
                      <>
                        {Math.round(t.best_azimuth_deg)}°{" "}
                        <span className="cardinal">{bearingCardinal(t.best_azimuth_deg)}</span>
                      </>
                    )}
                  </td>
                  <td className="mono cell-num cell-num-marked">
                    <span className="cell-v">
                      {t.frequency_mhz}
                      <MarkIf row={t} field="frequency_mhz" />
                    </span>
                    {t.bandwidth_mhz != null && (
                      <>
                        {" "}
                        <span className="cell-sub">
                          {t.bandwidth_mhz} MHz wide
                          <MarkIf row={t} field="bandwidth_mhz" />
                        </span>
                      </>
                    )}
                  </td>
                  <td className="mono power cell-num">{t.received_power_dbm}</td>
                  <td className="mono cell-num cell-num-marked">
                    <span className="cell-v">
                      {t.eirp_dbm}
                      <MarkIf row={t} field="eirp_dbm" />
                    </span>
                  </td>
                  <td className="mono cell-num cell-num-marked">
                    {t.antenna_height_m != null ? (
                      <span className="cell-v">
                        {t.antenna_height_m}
                        <MarkIf row={t} field="antenna_height_m" />
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="mono cell-num">
                    <span className="cell-v">{t.distance_km}</span>{" "}
                    <span className="cell-sub">
                      {t.bearing_deg}° <span className="cardinal">{t.bearing_cardinal}</span>
                    </span>
                  </td>
                  <td className="mono cell-num cell-num-marked cell-location">
                    <span className="cell-v">{t.latitude},</span>{" "}
                    <span className="cell-v">
                      {t.longitude}
                      <MarkIf row={t} field="position" />
                    </span>
                  </td>
                  <td>
                    <span className="chip" style={{ color: tier.color, background: tier.bg }}>
                      {tier.label}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
