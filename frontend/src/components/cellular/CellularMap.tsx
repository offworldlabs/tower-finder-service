import { useEffect, useMemo } from "react";
import L from "leaflet";
import { MapContainer, TileLayer, Marker, Popup, Circle, useMap } from "../../lib/leaflet";
import "leaflet/dist/leaflet.css";
import "../TowerMap.css";
import "./cellular.css";
import { InvalidateOnResize, TILE_URLS, userIcon } from "../TowerMap";
import { withCartoKey } from "../../utils/basemap";
import { rankTier, RANK_TIERS } from "../../utils/rankTier";
import BandChip from "./BandChip";
import ModelledMark, { MarkIf, isModelled } from "./ModelledMark";
import { formatDegrees } from "./Sector";
import { groupSites, siteKey, sourceName, type CellRow, type CellSite } from "./types";

interface Props {
  towers: CellRow[];
  userLocation: { latitude: number; longitude: number } | null;
  highlighted: CellRow | null;
  theme: "light" | "dark";
}

const SIZE = 36;
const SIZE_HIGHLIGHTED = 46;
/** Sector width drawn when the source gives none, and the range a drawn one
 *  is held to: a 9° pencil beam would vanish at marker size, and a 359°
 *  "sector" would hide every other wedge at the site. */
const DEFAULT_BEAMWIDTH = 65;
const MIN_WEDGE = 20;
const MAX_WEDGE = 120;
/** Cell sites stand tens of metres apart, so the fit may come in closer than
 *  the broadcast map's 13 before it stops. */
const SITE_MAX_ZOOM = 16;
const RANK_Z_STEP = 100;
const HIGHLIGHT_Z = 1_000_000;

/** SVG user space of the marker; the icon scales it to its pixel size. */
const VIEW = 48;
const C = VIEW / 2;
const WEDGE_R = 21;
const OMNI_R = 14;

interface Sector {
  azimuth: number;
  width: number;
}

/** One wedge per distinct whole-degree azimuth at the site, as wide as the
 *  widest beam any of its cells reports there. */
export function siteSectors(cells: CellRow[]): Sector[] {
  const widths = new Map<number, number | null>();
  for (const c of cells) {
    if (c.azimuth_deg == null || !Number.isFinite(c.azimuth_deg)) continue;
    const azimuth = ((Math.round(c.azimuth_deg) % 360) + 360) % 360;
    const bw = c.beamwidth_deg != null && Number.isFinite(c.beamwidth_deg) ? c.beamwidth_deg : null;
    const prev = widths.get(azimuth);
    if (!widths.has(azimuth) || (bw != null && (prev == null || bw > prev))) widths.set(azimuth, bw);
  }
  return [...widths].map(([azimuth, bw]) => ({
    azimuth,
    width: Math.min(MAX_WEDGE, Math.max(MIN_WEDGE, bw ?? DEFAULT_BEAMWIDTH)),
  }));
}

/** A point `r` out from the centre along a compass bearing (0 = north,
 *  clockwise), in SVG coordinates where y grows downward. */
function polar(bearing: number, r: number): string {
  const a = (bearing * Math.PI) / 180;
  return `${(C + r * Math.sin(a)).toFixed(2)} ${(C - r * Math.cos(a)).toFixed(2)}`;
}

/** A pie slice from the centre, swept clockwise across the beam. */
export function wedgePath({ azimuth, width }: Sector, r = WEDGE_R): string {
  const from = polar(azimuth - width / 2, r);
  const to = polar(azimuth + width / 2, r);
  return `M${C} ${C}L${from}A${r} ${r} 0 ${width > 180 ? 1 : 0} 1 ${to}Z`;
}

/**
 * The site marker: a diamond in the rank-tier colour of the site's best cell,
 * one translucent wedge per sector, or a dashed ring when no cell at the site
 * has a known azimuth. Every colour is a custom property reached through CSS
 * classes and `currentColor`, because this HTML lands in the document where
 * `var()` resolves; SVG presentation attributes would not substitute it.
 */
function siteSvg(sectors: Sector[], color: string, size: number, highlighted: boolean): string {
  const parts: string[] = [];
  if (highlighted) parts.push(`<circle class="cell-site-halo" cx="${C}" cy="${C}" r="${C - 0.75}"/>`);
  if (sectors.length) {
    const paths = sectors.map((s) => wedgePath(s));
    for (const d of paths) parts.push(`<path class="cell-site-casing" d="${d}"/>`);
    for (const d of paths) parts.push(`<path class="cell-site-wedge" d="${d}"/>`);
  } else {
    parts.push(`<circle class="cell-site-omni-casing" cx="${C}" cy="${C}" r="${OMNI_R}"/>`);
    parts.push(`<circle class="cell-site-omni" cx="${C}" cy="${C}" r="${OMNI_R}"/>`);
  }
  parts.push(
    `<rect class="cell-site-glyph" x="${C - 5.5}" y="${C - 5.5}" width="11" height="11" rx="2" transform="rotate(45 ${C} ${C})"/>`,
  );
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${VIEW} ${VIEW}" width="${size}" height="${size}" style="color:${color}" aria-hidden="true">${parts.join("")}</svg>`;
}

function siteIcon(site: CellSite, total: number, highlighted: boolean): L.DivIcon {
  const size = highlighted ? SIZE_HIGHLIGHTED : SIZE;
  const tier = rankTier(site.bestRank, total);
  return L.divIcon({
    className: highlighted ? "cell-site-marker is-highlighted" : "cell-site-marker",
    html: siteSvg(siteSectors(site.cells), tier.color, size, highlighted),
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    // The popup's tip lands on the diamond's upper point, not on its middle.
    popupAnchor: [0, -6],
  });
}

/** Fits the view once per answer. Keyed on the site list rather than the
 *  array, so a re-created array with the same sites (a hover re-render) never
 *  pulls back a map the operator has panned. */
function FitSites({ sites, userLocation }: { sites: CellSite[]; userLocation: Props["userLocation"] }) {
  const map = useMap();
  const sitesKey = sites.map((s) => s.key).join(";");

  useEffect(() => {
    if (!userLocation) return;
    const points: [number, number][] = [[userLocation.latitude, userLocation.longitude]];
    sites.forEach((s) => points.push([s.latitude, s.longitude]));

    if (points.length > 1) {
      map.fitBounds(points, { padding: [50, 50], maxZoom: SITE_MAX_ZOOM });
    } else {
      map.setView(points[0], 10);
    }
  }, [sitesKey, userLocation, map]);

  return null;
}

function SitePopup({ site, total }: { site: CellSite; total: number }) {
  const lead = site.cells[0];
  const positionModelled = site.cells.some((c) => isModelled(c, "position"));
  return (
    <div className="cell-popup">
      <span className="popup-callsign cell-popup-operators">
        {site.operators.length ? site.operators.join(" · ") : "Unknown operator"}
      </span>
      <span className="popup-detail cell-popup-position">
        {site.latitude}, {site.longitude}
        {positionModelled && <ModelledMark />}
      </span>
      <span className="popup-detail">
        {lead.distance_km} km {lead.bearing_cardinal} of you &middot;{" "}
        {site.sources.map(sourceName).join(", ") || "Unknown source"}
      </span>
      <span className="label cell-popup-count">
        {site.cells.length} {site.cells.length === 1 ? "cell" : "cells"}
      </span>
      <ul className="cell-popup-cells">
        {site.cells.map((c) => (
          <li key={`${c.rank}|${c.callsign}|${c.frequency_mhz}`} title={c.callsign ?? undefined}>
            <BandChip band={c.band} radio={c.radio} size="sm" />
            <span className="cell-popup-sector">
              {c.azimuth_deg != null ? (
                <>
                  {formatDegrees(c.azimuth_deg)}
                  <MarkIf row={c} field="azimuth_deg" />
                </>
              ) : (
                <span className="cell-muted">omni / unknown</span>
              )}
            </span>
            <span className="cell-popup-freq">
              {c.frequency_mhz} MHz
              <MarkIf row={c} field="frequency_mhz" />
            </span>
            <span className="cell-popup-rank" style={{ color: rankTier(c.rank, total).color }}>
              #{c.rank}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Legend() {
  return (
    <div className="map-legend cell-map-legend" aria-label="Cell site markers">
      <span className="map-legend-title">Sites</span>
      <span className="map-legend-item" title="Colour of the site's best-ranked cell, best to worst">
        <span className="cell-legend-ramp" aria-hidden="true">
          {RANK_TIERS.map((tier) => (
            <span key={tier.tier} className="cell-legend-diamond" style={{ background: tier.color }} />
          ))}
        </span>
        Best rank tier
      </span>
      <span className="map-legend-item">
        <svg className="cell-legend-icon" viewBox={`0 0 ${VIEW} ${VIEW}`} aria-hidden="true">
          <path className="cell-site-wedge" d={wedgePath({ azimuth: 45, width: 70 }, 22)} />
        </svg>
        Sector direction
      </span>
      <span className="map-legend-item">
        <svg className="cell-legend-icon" viewBox={`0 0 ${VIEW} ${VIEW}`} aria-hidden="true">
          <circle className="cell-site-omni" cx={C} cy={C} r={17} />
        </svg>
        Omni / unknown
      </span>
    </div>
  );
}

/**
 * The cellular map: the broadcast map's frame (tiles per theme, the search
 * point and radius, resize handling), with one marker per cell site in place
 * of one dot per transmitter.
 */
export default function CellularMap({ towers, userLocation, highlighted, theme = "light" }: Props) {
  const center: [number, number] = userLocation
    ? [userLocation.latitude, userLocation.longitude]
    : [39.8, -98.6];

  const sites = useMemo(() => groupSites(towers), [towers]);
  // Both looks are built once per answer, so a table hover swaps one icon
  // reference on each of two markers instead of rebuilding every marker.
  const icons = useMemo(() => {
    const total = towers.length;
    return new Map(
      sites.map((s) => [s.key, { normal: siteIcon(s, total, false), highlighted: siteIcon(s, total, true) }]),
    );
  }, [sites, towers.length]);
  const highlightedKey = highlighted ? siteKey(highlighted) : null;

  return (
    <div className="card map-wrap">
      <MapContainer center={center} zoom={4} className="map-container">
        <TileLayer
          key={theme}
          url={withCartoKey(TILE_URLS[theme] ?? TILE_URLS.light)}
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>'
        />

        {userLocation && (
          <>
            <Marker position={[userLocation.latitude, userLocation.longitude]} icon={userIcon}>
              <Popup>
                <span className="popup-callsign">Your Location</span>
              </Popup>
            </Marker>
            <Circle
              center={[userLocation.latitude, userLocation.longitude]}
              radius={80000}
              className="search-radius"
              pathOptions={{ weight: 1.5, fillOpacity: 0.04, dashArray: "6 4" }}
            />
          </>
        )}

        {sites.map((site) => {
          const isHighlighted = site.key === highlightedKey;
          const icon = icons.get(site.key);
          return (
            <Marker
              key={site.key}
              position={[site.latitude, site.longitude]}
              // Sites a few metres apart overlap. Leaflet stacks a marker by its
              // screen y plus this offset, so the offset steps by more than any
              // overlap in pixels: the best-ranked site paints on top whatever
              // its y, and the hovered one above them all.
              zIndexOffset={isHighlighted ? HIGHLIGHT_Z : (1000 - site.bestRank) * RANK_Z_STEP}
              icon={isHighlighted ? icon.highlighted : icon.normal}
            >
              <Popup minWidth={248} maxWidth={300}>
                <SitePopup site={site} total={towers.length} />
              </Popup>
            </Marker>
          );
        })}

        <InvalidateOnResize />
        <FitSites sites={sites} userLocation={userLocation} />
      </MapContainer>

      <Legend />
    </div>
  );
}
