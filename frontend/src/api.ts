import type { ElevationResponse, GeocodeResponse, TowerSearchResponse } from "./types";

const API_BASE = "/api";

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
 * Throws with the server's `detail` message on failure, including the 422 a
 * coordinate outside the supported regions produces.
 */
export async function fetchTowers(
  lat: number,
  lon: number,
  altitude = 0,
  limit = 20,
  source = "auto",
  frequencies: number[] = [],
  signal?: AbortSignal,
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
  const res = await fetch(`${API_BASE}/towers?${params}`, { signal });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
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
