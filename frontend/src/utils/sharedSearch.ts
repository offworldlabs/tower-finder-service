import type { SearchRequest } from "../types";
import { MAX_FREQUENCIES, parseFrequency } from "./frequencies";

/**
 * The page URL as a share link.
 *
 * A search is fully determined by five inputs — lat, lon, altitude, source and
 * the measured frequencies — so the query string carries exactly those and
 * nothing else:
 *
 *   /?lat=49.2648&lon=-123.2502&alt=95&source=ca&f=99.9,102.1
 *
 * `alt` is present only when the operator set the altitude themselves. Left
 * blank, the form fills it from /api/elevation for display, but that value is
 * not theirs: the link leaves it out and the recipient's search sends 0, which
 * GET /api/towers answers by resolving the ground elevation itself. `source`
 * is omitted when "auto" and `f` when empty.
 *
 * Deliberately absent: the address (a geocoder's answer drifts; the resolved
 * coordinates are what the search ran on), the radius (not in the UI) and the
 * map viewport (derived from the results).
 */

/** The query keys this module owns. Anything else in the URL is left alone. */
const KEYS = ["lat", "lon", "alt", "source", "f"] as const;

export const SOURCES = ["auto", "us", "ca", "au"] as const;

/** A source the form offers, lower-cased; anything else is "auto". */
export function normaliseSource(raw: string | null | undefined): string {
  const s = (raw ?? "").trim().toLowerCase();
  return (SOURCES as readonly string[]).includes(s) ? s : "auto";
}

/** What the search form starts with. Strings, because the form's inputs are. */
export interface SearchFormInitial {
  lat?: string;
  lon?: string;
  altitude?: string;
  source?: string;
  frequencies?: string[];
}

export interface SharedSearch {
  /** Every parameter that parsed, for the form to show. */
  initial: SearchFormInitial;
  /** The search to run on load, or null when the link is partial or invalid. */
  request: SearchRequest | null;
}

// A plain decimal, optionally with an exponent. Stricter than Number(), which
// would take "0x10" or "" (as 0), and than parseFloat, which reads "49abc" as 49.
const DECIMAL = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

type Parsed = { present: false } | { present: true; value: number | null };

function readNumber(raw: string | null, min: number, max: number): Parsed {
  if (raw === null) return { present: false };
  const text = raw.trim();
  if (!DECIMAL.test(text)) return { present: true, value: null };
  const value = Number(text);
  return { present: true, value: Number.isFinite(value) && value >= min && value <= max ? value : null };
}

/**
 * Read a search out of a query string (`window.location.search`).
 *
 * Bounds are the form's own: the lat/lon/altitude inputs' min and max, and the
 * frequency rule the form submits by. A link is run only when lat and lon are
 * both valid and no parameter that decides the result (lat, lon, alt) is
 * present but unreadable; otherwise whatever did parse is prefilled and the
 * operator presses Find Towers. An unknown source falls back to "auto" and an
 * unusable frequency is dropped, which is what the form would have done.
 */
export function readSharedSearch(search: string): SharedSearch {
  const params = new URLSearchParams(search);
  const lat = readNumber(params.get("lat"), -90, 90);
  const lon = readNumber(params.get("lon"), -180, 180);
  const alt = readNumber(params.get("alt"), 0, Infinity);
  const source = normaliseSource(params.get("source"));
  const frequencies = (params.get("f") ?? "")
    .split(",")
    .map((s) => parseFrequency(s.trim()))
    .filter((f): f is number => f !== null)
    .slice(0, MAX_FREQUENCIES);

  const value = (p: Parsed) => (p.present ? p.value : null);
  const initial: SearchFormInitial = {};
  if (value(lat) !== null) initial.lat = String(value(lat));
  if (value(lon) !== null) initial.lon = String(value(lon));
  if (value(alt) !== null) initial.altitude = String(value(alt));
  if (params.has("source")) initial.source = source;
  if (frequencies.length) initial.frequencies = frequencies.map(String);

  const runnable =
    value(lat) !== null && value(lon) !== null && (!alt.present || alt.value !== null);
  const request: SearchRequest | null = runnable
    ? {
        lat: value(lat),
        lon: value(lon),
        altitude: value(alt) ?? 0,
        altitudeSet: alt.present,
        source,
        frequencies,
      }
    : null;
  return { initial, request };
}

/**
 * The query string for a search, `?`-prefixed. Parameters this module does
 * not own are carried over from `current` rather than stripped.
 *
 * Built by hand rather than with URLSearchParams, which would write the
 * frequency list as `f=99.9%2C102.1`; each value is still encoded, so an
 * exponent's `+` cannot come back as a space.
 */
export function sharedSearchQuery(req: SearchRequest, current = ""): string {
  const enc = (v: number | string) => encodeURIComponent(String(v));
  const parts = [`lat=${enc(req.lat)}`, `lon=${enc(req.lon)}`];
  if (req.altitudeSet) parts.push(`alt=${enc(req.altitude)}`);
  const source = normaliseSource(req.source);
  if (source !== "auto") parts.push(`source=${enc(source)}`);
  if (req.frequencies.length) parts.push(`f=${req.frequencies.map(enc).join(",")}`);

  const foreign = new URLSearchParams(current);
  for (const key of KEYS) foreign.delete(key);
  const rest = foreign.toString();
  if (rest) parts.push(rest);
  return `?${parts.join("&")}`;
}

/**
 * Make the address bar the share link for `req`. replaceState, not
 * pushState: a history entry per search would turn Back into an undo stack
 * the page does not otherwise have.
 */
export function writeSharedSearch(req: SearchRequest): void {
  const { pathname, search, hash } = window.location;
  try {
    window.history.replaceState(window.history.state, "", pathname + sharedSearchQuery(req, search) + hash);
  } catch {
    // A sandboxed frame can refuse; the search itself must still run.
  }
}
