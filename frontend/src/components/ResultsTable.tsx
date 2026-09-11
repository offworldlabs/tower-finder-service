import "./ResultsTable.css";
import { rankTier } from "../utils/rankTier";
import { bearingCardinal, beyondHorizon, formatAreaKm2 } from "../utils/format";

const BAND_COLORS = {
  VHF: "#7c3aed",
  UHF: "#0891b2",
  FM: "#db2777",
};

const BAND_BG = {
  VHF: "rgba(124, 58, 237, 0.08)",
  UHF: "rgba(8, 145, 178, 0.08)",
  FM: "rgba(219, 39, 119, 0.08)",
};

export default function ResultsTable({ towers, onHover }) {
  return (
    <div className="results-wrap">
      <h2>Results <span className="results-count">{towers.length}</span></h2>
      <div className="table-scroll">
        <table className="results-table">
          <thead>
            <tr>
              <th>#</th>
              {/* The ranking is sorted on detect area, so it sits beside the
                  rank rather than at the far end of the row. */}
              <th>Detect Area (km&sup2;)</th>
              <th>Point</th>
              <th>Callsign</th>
              <th>Location</th>
              <th>Lat</th>
              <th>Long</th>
              <th>Altitude (m)</th>
              <th>Ant. Height (m)</th>
              <th>Freq (MHz)</th>
              <th>Band</th>
              <th>EIRP</th>
              <th>Distance</th>
              <th>Bearing</th>
              <th>Rx Power</th>
              <th>Rank Tier</th>
            </tr>
          </thead>
          <tbody>
            {towers.map((t) => {
              const tier = rankTier(t.rank, towers.length);
              // Past the horizon the tower is still listed — it just reads as
              // muted, so a low-ranked but familiar transmitter explains itself.
              const overHorizon = beyondHorizon(t.distance_km, t.horizon_km);
              return (
                <tr
                  key={`${t.rank}-${t.frequency_mhz}`}
                  className={overHorizon ? "beyond-horizon" : undefined}
                  title={overHorizon ? "Beyond radio horizon" : undefined}
                  onMouseEnter={() => onHover(t)}
                  onMouseLeave={() => onHover(null)}
                >
                  <td className="rank">
                    <span className="rank-badge" style={{ color: tier.color, background: tier.bg }}>
                      {t.rank}
                    </span>
                  </td>
                  <td className="mono detect-area">{formatAreaKm2(t.expected_area_km2)}</td>
                  <td className="point">
                    {t.best_azimuth_deg != null && (
                      <>
                        {Math.round(t.best_azimuth_deg)}°{" "}
                        <span className="cardinal">{bearingCardinal(t.best_azimuth_deg)}</span>
                      </>
                    )}
                  </td>
                  <td className="callsign">
                    {t.callsign || "—"}
                    {t.shared_callsigns && t.shared_callsigns.length > 0 && (
                      <span
                        className="shared-callsigns"
                        title="Also licensed on this transmitter (channel sharing)"
                      >
                        + {t.shared_callsigns.join(", ")}
                      </span>
                    )}
                  </td>
                  <td className="location-name" title={`${t.name}${t.state ? `, ${t.state}` : ""}`}>
                    {t.name}
                    {t.state ? `, ${t.state}` : ""}
                  </td>
                  <td className="mono">{t.latitude}</td>
                  <td className="mono">{t.longitude}</td>
                  <td className="mono">{t.altitude_m != null ? t.altitude_m : "—"}</td>
                  <td className="mono">{t.antenna_height_m != null ? t.antenna_height_m : "—"}</td>
                  <td className="mono">
                    {t.frequency_mhz}
                    {t.frequency_matched && (
                      <span className="freq-match-badge" title="Matches measured frequency">&#10003;</span>
                    )}
                  </td>
                  <td>
                    <span
                      className="badge"
                      style={{
                        color: BAND_COLORS[t.band] || "#6b7280",
                        background: BAND_BG[t.band] || "rgba(107,114,128,0.08)",
                      }}
                    >
                      {t.band}
                    </span>
                  </td>
                  <td className="mono">{t.eirp_dbm} dBm</td>
                  <td className="mono">{t.distance_km} km</td>
                  <td>
                    {t.bearing_deg}° <span className="cardinal">{t.bearing_cardinal}</span>
                  </td>
                  <td className="mono power">{t.received_power_dbm} dBm</td>
                  <td>
                    <span className="badge" style={{ color: tier.color, background: tier.bg }}>
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
