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
  bearing_deg: number;
  bearing_cardinal: string;
  received_power_dbm: number;
  /** Other stations licensed on this same transmitter and frequency (FCC
   *  channel-sharing partners, LPFM time-shares). Empty when it stands alone. */
  shared_callsigns?: string[];

  /* ---- expected-area ranking -------------------------------------------
   * Every field below is additive and OPTIONAL: an older backend omits them
   * entirely, and the UI must render nothing rather than a placeholder that
   * would read as a measured value. */

  /** Modelled detectable area for a 10 m² aircraft, km². `rank` is sorted on
   *  this, descending — it is the number the ranking is built on. */
  expected_area_km2?: number;
  /** Surveillance-antenna azimuth (0–360, true north) at which that area is
   *  achieved. Pointing advice, not the bearing to the tower. */
  best_azimuth_deg?: number;
  /** Radio horizon distance. A tower whose `distance_km` exceeds this is past
   *  the horizon and heavily penalised in the ranking. */
  horizon_km?: number;
  /** Fleet-feedback multiplier applied to the modelled area; 1.0 means no
   *  fleet data has been folded in yet. */
  feedback_factor?: number;
  /** How many fleet observations that factor rests on. */
  feedback_n?: number;
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

/** /api/geocode response */
export interface GeocodeResponse {
  /** Echo of what was asked for, so a late reply can be told from a current one. */
  query: string;
  latitude: number;
  longitude: number;
  matched_address: string;
  provider: "census" | "nominatim";
  /** How specific the match is. Anything other than "street" is a centroid:
   *  the ranking grids its disk in 2 km cells and applies a radio-horizon
   *  check, so a city-centre point can sit 10 km from the real site and come
   *  back with a different answer. The form says so when it does. */
  precision: "street" | "postcode" | "locality";
}

/** One search as the form submits it and the share link records it. */
export interface SearchRequest {
  lat: number;
  lon: number;
  /** Metres. 0 asks the server to resolve the ground elevation itself. */
  altitude: number;
  /** True when the operator set the altitude (typed it, or it came from a
   *  link that carried one). False when it is blank or the /api/elevation
   *  prefill, which the share link leaves out so the recipient's search
   *  resolves it the same way. */
  altitudeSet: boolean;
  source: string;
  frequencies: number[];
}
