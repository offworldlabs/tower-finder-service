import { useEffect } from "react";
import { MapContainer, TileLayer, Marker, Popup, Circle, useMap } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "./TowerMap.css";
import { withCartoKey } from "../utils/basemap";
import { rankTier, RANK_TIERS } from "../utils/rankTier";
import { formatAreaKm2 } from "../utils/format";

/**
 * Marker colours are custom properties, which resolve because this HTML lands
 * in the document as an inline style. The ring stays a light hairline on both
 * themes: it separates the dot from the basemap, which is a light tile set
 * either way (see TowerMap.css).
 */
function makeTowerIcon(color: string, isHighlighted: boolean) {
  const size = isHighlighted ? 16 : 11;
  const border = isHighlighted ? 3 : 2;
  const shadow = isHighlighted
    ? "0 0 0 3px var(--accent-light), 0 2px 6px rgba(0,0,0,.25)"
    : "0 1px 4px rgba(0,0,0,.3)";
  return L.divIcon({
    className: "tower-marker",
    html: `<div style="
      width:${size}px;height:${size}px;
      background:${color};
      border:${border}px solid var(--marker-ring);
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
    background:var(--accent);
    border:3px solid var(--marker-ring);
    border-radius:50%;
    box-shadow:0 0 0 3px var(--accent-light), 0 2px 8px rgba(0,0,0,.2);
  "></div>`,
  iconSize: [16, 16],
  iconAnchor: [8, 8],
});

/**
 * Carto Positron on light, Voyager on dark, both pushed back by
 * `--tile-filter`. Swapping the tile set rather than only the filter is what
 * the live map does: Positron's near-white ground is what the console chrome
 * was drawn against, and dimming it far enough to sit under navy leaves a grey
 * wash with no geography left in it.
 */
const TILE_URLS = {
  light: "https://{s}.basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}{r}.png",
  dark: "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
} as const;

/**
 * Leaflet caches the container's size at init and only re-reads it when told
 * to. The panel now stretches to its grid cell, so adding a frequency row to
 * the form beside it changes that size with no window resize to notice.
 */
function InvalidateOnResize() {
  const map = useMap();

  useEffect(() => {
    const el = map.getContainer();
    const observer = new ResizeObserver(() => map.invalidateSize({ animate: false }));
    observer.observe(el);
    return () => observer.disconnect();
  }, [map]);

  return null;
}

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

export default function TowerMap({ towers, userLocation, highlighted, theme = "light" }) {
  const center: [number, number] = userLocation
    ? [userLocation.latitude, userLocation.longitude]
    : [39.8, -98.6]; // center of USA (default)

  return (
    <div className="card map-wrap">
      <MapContainer center={center} zoom={4} className="map-container">
        <TileLayer
          // Leaflet keeps the layer it was given; remounting is what makes a
          // theme change actually fetch the other tile set.
          key={theme}
          url={withCartoKey(TILE_URLS[theme] ?? TILE_URLS.light)}
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
              // Colour comes from the class, not from a `color` option: that
              // lands in an SVG presentation attribute, where a var() is not
              // substituted. The class has to be a top-level prop — react-leaflet
              // replays `pathOptions` through setStyle, which ignores className,
              // so one passed there is silently dropped.
              className="search-radius"
              pathOptions={{
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
              // Co-located stations (shared masts) get identical Leaflet z-indexes,
              // so DOM order decides and the worst rank would paint on top.
              // Keep the best-ranked marker visible at every site.
              zIndexOffset={1000 - t.rank}
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

        <InvalidateOnResize />
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
