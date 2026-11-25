import type {
  CellularStatusResponse,
  ElevationResponse,
  GeocodeResponse,
  TowerSearchResponse,
} from "./types";

const API_BASE = "/api";

// Rows are cells and one site carries many, so the usual 20 would show only a
// few sites; the API caps limit at 200.
export const CELLULAR_LIMIT = 100;

/**
 * Search for towers near a point.
 *
 * `source` defaults to "auto", which asks the server to classify the
 * coordinate against real border polygons (services/region_lookup.py). The
 * client deliberately does NOT guess the country itself — an earlier
 * bounding-box heuristic here returned "ca" for every US point above 42°N,
 * and because it pinned the result into the request the server's correct
 * answer never got a chance to apply. Leave the classification server-side.
 *
 * `frequencies` are the operator's own measurements in MHz, which rank matching
 * towers ahead of the rest. They go over the wire as one comma-separated value:
 * the route types the parameter as a scalar `str`, so repeating the key would
 * leave Starlette holding only the last one and quietly drop the others.
 *
 * `kind` "cellular" asks for cell sites instead of broadcast transmitters. It
 * goes last, and left out the request is exactly the broadcast search.
 *
 * Throws with the server's `detail` message on failure, including the 422 a
 * coordinate with no supported region (US, CA, AU) within the search radius
 * produces; that detail names the radius and suggests a larger one.
 */
export async function fetchTowers(
  lat: number,
  lon: number,
  altitude = 0,
  limit = 20,
  source = "auto",
  frequencies: number[] = [],
  signal?: AbortSignal,
  kind?: "cellular",
): Promise<TowerSearchResponse> {
  const params = new URLSearchParams({
    lat: String(lat),
    lon: String(lon),
    altitude: String(altitude),
    limit: String(limit),
    source,
  });
  if (frequencies.length > 0) {
    params.set("frequencies", frequencies.join(","));
  }
  if (kind) {
    params.set("kind", kind);
  }
  const res = await fetch(`${API_BASE}/towers?${params}`, { signal });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

// The page holds its searches until this answer is in. It gets as long as a
// tower search does: the first answer after a server restart scans the whole
// cell table and can be slow on a cold box, while a hung server still falls
// back to the standard search.
const CELLULAR_STATUS_TIMEOUT_MS = 60_000;

/**
 * Whether the server offers cellular towers, asked once when the page is
 * opened with the cellular flag.
 *
 * The status object only on a 200 whose body is a JSON object with
 * `enabled: true`. Anything else is null: a 404 (the route exists only while
 * the server has cellular switched on), any other status, a body that is not
 * that object, a network failure, an abort, or no full answer within a
 * minute. Never throws, because every one of those means the same thing to
 * the page: carry on as the broadcast search.
 *
 * An `attribution` that is not an array comes back as an empty one: the
 * credits block lists it, and the view still turns on without it.
 */
export async function fetchCellularStatus(
  signal?: AbortSignal,
): Promise<CellularStatusResponse | null> {
  // One controller ends the request on the time limit or the caller's abort,
  // whichever comes first. Aborting it fails the body read as well as the
  // fetch, so a server that sends headers and then stalls is covered too.
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timer = setTimeout(abort, CELLULAR_STATUS_TIMEOUT_MS);
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort);
  try {
    const res = await fetch(`${API_BASE}/cellular`, { signal: controller.signal });
    if (res.status !== 200) return null;
    const body: unknown = await res.json();
    if (typeof body !== "object" || body === null || Array.isArray(body)) return null;
    if ((body as { enabled?: unknown }).enabled !== true) return null;
    const status = body as CellularStatusResponse;
    return Array.isArray(status.attribution) ? status : { ...status, attribution: [] };
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

/** Ground elevation at a point, or null if the upstream lookup failed. */
export async function fetchElevation(
  lat: number,
  lon: number,
  signal?: AbortSignal,
): Promise<number | null> {
  const params = new URLSearchParams({ lat: String(lat), lon: String(lon) });
  const res = await fetch(`${API_BASE}/elevation?${params}`, { signal });
  if (!res.ok) return null;
  const data: ElevationResponse = await res.json();
  return data.elevation_m;
}

/**
 * Turn an address typed by the operator into a coordinate.
 *
 * POSTed rather than sent as a query string: the address is free text that
 * would otherwise land in server access logs and browser history, and it is
 * long enough (200 chars) to be awkward in a URL.
 *
 * Throws with the server's `detail` so the form can show the server's own
 * wording — "No match for that address" (404), "Address lookup is unavailable
 * right now" (503) — instead of inventing one per status code. A 422 answers
 * with pydantic's array of errors, which is not a sentence, so that case falls
 * through to the generic message the same way fetchTowers handles it.
 */
export async function geocodeAddress(
  query: string,
  signal?: AbortSignal,
): Promise<GeocodeResponse> {
  const res = await fetch(`${API_BASE}/geocode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
    signal,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string" ? body.detail : `Request failed (${res.status})`,
    );
  }
  return res.json();
}
