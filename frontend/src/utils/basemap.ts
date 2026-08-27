/**
 * Attaches the CARTO API key to basemap tile URLs.
 *
 * Not optional in practice. CARTO watermarks every unkeyed tile with "API KEY
 * REQUIRED" right across basemaps.cartocdn.com — voyager, light_all and
 * dark_all, every {s} subdomain, plain and @2x alike (verified 2026-08-27). A
 * build without the key still produces a working map, just a defaced one, so
 * an environment that is missing it degrades visibly rather than failing.
 *
 * The key arrives as a build-time value (VITE_CARTO_API_KEY, threaded from the
 * host's gitignored env through the Dockerfile's frontend stage), which means
 * it is baked into the shipped bundle and readable by anyone who opens
 * devtools. That is the supported shape for a basemap token rather than a
 * mistake: tile requests are issued by the browser, so no server sits in the
 * path that could hold a secret. It does mean this must only ever carry a key
 * scoped to fetching tiles.
 *
 * The parameter is `key`, and getting that wrong is a silent failure worth
 * knowing about: CARTO accepts any other name and ignores it, so `?api_key=`
 * returns a byte-identical watermarked tile rather than an error.
 */
const CARTO_API_KEY = (import.meta.env.VITE_CARTO_API_KEY ?? "").trim();

/**
 * The only host the key belongs to. Guarding on it keeps this safe to wrap
 * around any tile URL, so swapping a layer to OSM or another provider later
 * cannot silently start appending a Carto key to someone else's CDN.
 */
const CARTO_HOST = "basemaps.cartocdn.com";

export function withCartoKey(url: string): string {
  if (!CARTO_API_KEY || !url.includes(CARTO_HOST)) return url;
  return `${url}${url.includes("?") ? "&" : "?"}key=${encodeURIComponent(CARTO_API_KEY)}`;
}
