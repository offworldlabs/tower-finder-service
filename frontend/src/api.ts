import type { ElevationResponse, TowerSearchResponse } from "./types";

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
 * Throws with the server's `detail` message on failure, including the 422 a
 * coordinate outside the supported regions produces.
 */
export async function fetchTowers(
  lat: number,
  lon: number,
  altitude = 0,
  limit = 20,
  source = "auto",
  signal?: AbortSignal,
): Promise<TowerSearchResponse> {
  const params = new URLSearchParams({
    lat: String(lat),
    lon: String(lon),
    altitude: String(altitude),
    limit: String(limit),
    source,
  });
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
