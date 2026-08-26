/* ------------------------------------------------------------------ */
/*  API response types — tower-finder-service frontend                */
/* ------------------------------------------------------------------ */

/** Single tower returned by /api/towers, as shaped by services/tower_ranking.py */
export interface Tower {
  rank: number;
  callsign: string | null;
  name: string;
  state?: string | null;
  latitude: number;
  longitude: number;
  /** Ground elevation at the tower, metres ASL. Null when the lookup failed. */
  elevation_m: number | null;
  /** Ground elevation + antenna height. Null when elevation is unavailable. */
  altitude_m: number | null;
  antenna_height_m: number | null;
  frequency_mhz: number;
  /** True when the tower matched one of the POSTed spectrum measurements. */
  frequency_matched?: boolean;
  band: string;
  eirp_dbm: number;
  distance_km: number;
  distance_class: string;
  bearing_deg: number;
  bearing_cardinal: string;
  received_power_dbm: number;
}

/** The echo of what the server actually searched — note `source` is the
 *  RESOLVED region, so it reports what "auto" was classified as. */
export interface TowerQuery {
  latitude: number;
  longitude: number;
  altitude_m: number;
  radius_km: number;
  source: string;
}

/** /api/towers response */
export interface TowerSearchResponse {
  towers: Tower[];
  query: TowerQuery;
  count: number;
}

/** /api/elevation response */
export interface ElevationResponse {
  latitude: number;
  longitude: number;
  elevation_m: number;
}
