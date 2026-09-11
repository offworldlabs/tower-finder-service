import { useEffect } from "react";
import { MapContainer, TileLayer, Marker, Popup, Circle, useMap } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./TowerMap.css";
import { withCartoKey } from "../utils/basemap";
import { rankTier, RANK_TIERS } from "../utils/rankTier";
import { formatAreaKm2 } from "../utils/format";

function makeTowerIcon(color: string, isHighlighted: boolean) {
  const size = isHighlighted ? 16 : 11;
  const border = isHighlighted ? 3 : 2;
  const shadow = isHighlighted
    ? "0 0 0 3px rgba(59,130,246,.3), 0 2px 6px rgba(0,0,0,.25)"
    : "0 1px 4px rgba(0,0,0,.3)";
  return L.divIcon({
    className: "tower-marker",
    html: `<div style="
      width:${size}px;height:${size}px;
      background:${color};
      border:${border}px solid #fff;
      border-radius:50%;
      box-shadow:${shadow};
      transition: all 0.15s;
    "></div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

const userIcon = L.divIcon({
  className: "user-marker",
  html: `<div style="
    width:16px;height:16px;
    background:#3b82f6;
    border:3px solid #fff;
    border-radius:50%;
    box-shadow:0 0 0 3px rgba(59,130,246,.25), 0 2px 8px rgba(0,0,0,.2);
  "></div>`,
  iconSize: [16, 16],
  iconAnchor: [8, 8],
});

function FitBounds({ towers, userLocation }) {
  const map = useMap();

  useEffect(() => {
    if (!userLocation) return;
    const points: [number, number][] = [[userLocation.latitude, userLocation.longitude]];
    towers.forEach((t) => points.push([t.latitude, t.longitude]));

    if (points.length > 1) {
      map.fitBounds(points, { padding: [50, 50], maxZoom: 13 });
    } else {
      map.setView(points[0], 10);
    }
  }, [towers, userLocation, map]);

  return null;
}

export default function TowerMap({ towers, userLocation, highlighted }) {
  const center: [number, number] = userLocation
    ? [userLocation.latitude, userLocation.longitude]
    : [39.8, -98.6]; // center of USA (default)

  return (
    <div className="map-wrap">
      <MapContainer center={center} zoom={4} className="map-container">
        <TileLayer
          url={withCartoKey("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png")}
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>'
        />

        {userLocation && (
          <>
            <Marker
              position={[userLocation.latitude, userLocation.longitude]}
              icon={userIcon}
            >
              <Popup>
                <span className="popup-callsign">Your Location</span>
              </Popup>
            </Marker>
            <Circle
              center={[userLocation.latitude, userLocation.longitude]}
              radius={80000}
              pathOptions={{
                color: "#3b82f6",
                weight: 1.5,
                fillOpacity: 0.04,
                dashArray: "6 4",
              }}
            />
          </>
        )}

        {towers.map((t) => {
          const tier = rankTier(t.rank, towers.length);
          return (
            <Marker
              key={`${t.rank}-${t.frequency_mhz}`}
              position={[t.latitude, t.longitude]}
              icon={makeTowerIcon(
                tier.color,
                highlighted &&
                  highlighted.callsign === t.callsign &&
                  highlighted.frequency_mhz === t.frequency_mhz
              )}
            >
              <Popup>
                <span className="popup-callsign">{t.callsign || "Unknown"}</span>
                <br />
                <span className="popup-detail">{t.name}</span>
                <br />
                <span className="popup-detail">
                  {t.latitude}, {t.longitude}
                  {t.altitude_m != null && ` · ${t.altitude_m} m ASL`}
                </span>
                <br />
                <span className="popup-freq">{t.frequency_mhz} MHz</span>{" "}
                ({t.band})
                <br />
                <span className="popup-detail">
                  {t.distance_km} km {t.bearing_cardinal} &middot; {t.received_power_dbm} dBm
                </span>
                <br />
                {t.expected_area_km2 != null && (
                  <>
                    <span className="popup-detail">
                      Detect area {formatAreaKm2(t.expected_area_km2)} km&sup2;
                      {t.best_azimuth_deg != null &&
                        ` · point ${Math.round(t.best_azimuth_deg)}°`}
                    </span>
                    <br />
                  </>
                )}
                <span style={{ color: tier.color, fontWeight: 600, fontSize: "0.78rem" }}>
                  #{t.rank} · {tier.label}
                </span>
                {t.shared_callsigns && t.shared_callsigns.length > 0 && (
                  <>
                    <br />
                    <span className="popup-detail">Shares transmitter with {t.shared_callsigns.join(", ")}</span>
                  </>
                )}
              </Popup>
            </Marker>
          );
        })}

        <FitBounds towers={towers} userLocation={userLocation} />
      </MapContainer>

      <div className="map-legend" aria-label="Marker colour by rank">
        <span className="map-legend-title">Rank</span>
        {RANK_TIERS.map((tier) => (
          <span key={tier.tier} className="map-legend-item">
            <span className="map-legend-swatch" style={{ background: tier.color }} />
            {tier.label}
          </span>
        ))}
      </div>
    </div>
  );
}
