import json
import logging
import math
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0

# Band-specific tolerances for matching spectrum-analyser measurements to database towers.
# The analyser gives sub-kHz precision so the tolerance only needs to cover database
# inaccuracies, not human measurement error.
#   FM:      stations are 200 kHz apart — ±150 kHz avoids cross-station matches.
#   VHF/UHF: DVB-T channels are 7–8 MHz wide — ±4 MHz catches the right channel
#            without bleeding into an adjacent one.
MEASUREMENT_TOLERANCE_MHZ: dict[str, float] = {
    "FM": 0.15,
    "VHF": 4.0,
    "UHF": 4.0,
}

# Hand-typed frequencies (GET ?frequencies=), not analyser output: wide enough
# to forgive a remembered-roughly value. Same constant as the parent repo's
# in-process route, which this endpoint replaces behind nginx — the two must
# match a tower the same way while both exist.
FREQUENCY_MATCH_TOLERANCE_MHZ = 5.0

# ── Load configurable settings from tower_config.json ────────────────────
# Image-shipped default lives next to this module (config/ is image-only); the
# runtime overlay holds whatever PUT /api/config writes back, so the source
# tree never gets mutated at runtime. Override the overlay location with
# TOWER_FINDER_RUNTIME_DIR.
_SOURCE_DEFAULT_DIR = Path(__file__).resolve().parent.parent / "config"
_RUNTIME_DIR = Path(os.environ.get("TOWER_FINDER_RUNTIME_DIR", "data/runtime"))
_CONFIG_PATH = _RUNTIME_DIR / "tower_config.json"


def _seed_defaults() -> None:
    """Copy source defaults into the runtime overlay on first use. Idempotent."""
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if _CONFIG_PATH.exists():
        return
    src = _SOURCE_DEFAULT_DIR / "tower_config.json"
    if src.exists():
        shutil.copy2(src, _CONFIG_PATH)


def _load_config() -> dict:
    # Self-heal: this module is imported at app startup AND standalone by
    # tests. If the runtime overlay hasn't been seeded yet, seed it now so
    # the open() below finds a file.
    if not _CONFIG_PATH.exists():
        _seed_defaults()
    with _CONFIG_PATH.open() as f:
        return json.load(f)


# Band taxonomy: TV = VHF/UHF, FM = radio. Hardcoded (consistent with
# BAND_PRIORITY / MEASUREMENT_TOLERANCE_MHZ, which also key on these names).
# TODO(DAB): DAB (digital radio) sits in VHF Band III, overlapping TV-VHF;
# band-based gating would misclassify it as TV and wrongly withhold it from
# non-NA users. When DAB data is ingested, give it its own band label /
# service-level classification rather than reusing 'VHF'.
ALL_BANDS = frozenset({"FM", "VHF", "UHF"})
FM_ONLY = frozenset({"FM"})

# Regions whose broadcast TV standard matches the node's ATSC-only demodulation.
# TV towers are withheld everywhere else (DVB-T/ISDB-T regions) until the node
# gains multi-standard support. This is a capability allowlist, not geography.
TV_ELIGIBLE_REGIONS = frozenset({"us", "ca"})


def allowed_bands_for_region(source: str) -> frozenset:
    """Bands a request from `source` may be served.

    ATSC-capable regions get all bands; everyone else gets FM only, until the
    node gains multi-standard demodulation.
    """
    return ALL_BANDS if source in TV_ELIGIBLE_REGIONS else FM_ONLY


def _is_number(value) -> bool:
    # bool subclasses int, but a JSON true where a number belongs is a mistake.
    #
    # NaN and ±Infinity are rejected too. json.loads accepts those bare literals,
    # and every comparison against NaN is False, so an unfiltered NaN passes each
    # range check below and is persisted. It surfaces much later and far from
    # here: DEFAULT_LIMIT = nan makes towers[:effective_limit] raise TypeError on
    # every search, and a NaN distance-class bound silently matches nothing.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


# Fields a ranking.sort_order rule may name: a field belongs here only if it
# resolves to a real number for every tower.
#
# band_priority and distance_priority are special-cased in _sort_key() and always
# do; the rest are numeric keys of the tower dict built in process_and_rank(),
# plus coverage_area_added_km2, which services/tower_coverage.py adds. The
# analyser fields (snr_db, score, power_db, obw_fraction) and the two booleans
# are numeric-or-None, and _sort_key() reads every field as ``or 0``, so a None
# or a missing key sorts as zero rather than raising.
#
# Deliberately absent: the string fields (callsign, name, state, band,
# bearing_cardinal, distance_class, licence_*), which _sort_key() would negate
# and raise TypeError on for a descending rule; and antenna_height_m, which is
# None whenever the upstream record omits it — it would not raise here, but a
# tower of unknown height ranking as a 0 m tower is a silent lie either way.
#
# This is the union of the two engines' sortable sets: the monolith's ranking
# fields and this service's analyser-measurement fields both validate, so
# switching ranking strategy is a config PUT rather than a code change. Adding a
# numeric field to the tower dict means adding it here too, or a config naming it
# is rejected.
_SORTABLE_FIELDS = frozenset(
    {
        # Shared with the monolith's engine.
        "band_priority",
        "distance_priority",
        "coverage_area_added_km2",
        "received_power_dbm",
        "distance_km",
        "bearing_deg",
        "frequency_mhz",
        "eirp_dbm",
        "frequency_matched",
        "latitude",
        "longitude",
        # This service's spectrum-analyser measurement fields.
        "score",
        "snr_db",
        "power_db",
        "obw_fraction",
        "measured",
    }
)


def validate_config(cfg: dict) -> str | None:
    """Return an error message if the tower config is unusable, else None.

    Covers exactly what apply_config() consumes, so anything accepted here can
    be applied without raising. Every section is optional — each has a default
    in apply_config() — but a section that is present must have the right shape.

    Everything a search consumes as a number has to be one, and three separate
    parts of the config feed that: the fields a sort_order rule names, the values
    in the band_priority and distance_priority tables, and search.default_limit.
    _sort_key() puts the first two into a tuple it sorts on and negates them for
    a descending rule, and default_limit ends up as a slice bound. A string, a
    null or a fractional number in any of those places passes every structural
    check and then raises TypeError on every search, far from the config that
    caused it.
    """
    if not isinstance(cfg, dict):
        return f"config must be an object, got {type(cfg).__name__}"

    for section in ("receiver", "ranking", "search", "broadcast_bands"):
        value = cfg.get(section)
        if value is not None and not isinstance(value, dict):
            return f"{section} must be an object, got {type(value).__name__}"

    receiver = cfg.get("receiver", {})
    for key in ("rx_antenna_gain_dbi", "sensitivity_dbm"):
        if key in receiver and not _is_number(receiver[key]):
            return f"receiver.{key} must be a number, got {receiver[key]!r}"

    for band, ranges in cfg.get("broadcast_bands", {}).items():
        if not isinstance(ranges, list):
            return f"broadcast_bands.{band} must be a list of [low, high] pairs"
        for r in ranges:
            if not isinstance(r, list) or len(r) != 2 or not all(_is_number(v) for v in r):
                return f"broadcast_bands.{band} entry must be a [low, high] pair of numbers, got {r!r}"
            if r[0] >= r[1]:
                return f"broadcast_bands.{band} range is not ascending: {r!r}"

    ranking = cfg.get("ranking", {})
    for key in ("band_priority", "distance_priority"):
        table = ranking.get(key)
        if table is None:
            continue
        if not isinstance(table, dict):
            return f"ranking.{key} must be an object, got {type(table).__name__}"
        # _sort_key() reads these straight into the sort tuple, alongside the
        # literal 99 it falls back to, so a non-numeric value here is compared
        # against an int and raises rather than sorting oddly.
        for name, priority in table.items():
            if not _is_number(priority):
                return f"ranking.{key}[{name!r}] must be a number, got {priority!r}"

    classes = ranking.get("distance_classes")
    if classes is not None:
        if not isinstance(classes, list):
            return f"ranking.distance_classes must be a list, got {type(classes).__name__}"
        for i, dc in enumerate(classes):
            if not isinstance(dc, dict):
                return f"ranking.distance_classes[{i}] must be an object, got {type(dc).__name__}"
            # apply_config() indexes these three directly rather than .get()ing
            # them, so a missing key is a KeyError at apply time, not a default.
            for key in ("label", "min_km", "max_km"):
                if key not in dc:
                    return f"ranking.distance_classes[{i}] is missing {key}"
            if not isinstance(dc["label"], str) or not dc["label"]:
                return f"ranking.distance_classes[{i}].label must be a non-empty string"
            if not _is_number(dc["min_km"]):
                return f"ranking.distance_classes[{i}].min_km must be a number, got {dc['min_km']!r}"
            # max_km is nullable: the last class is open-ended.
            if dc["max_km"] is not None:
                if not _is_number(dc["max_km"]):
                    return f"ranking.distance_classes[{i}].max_km must be a number or null, got {dc['max_km']!r}"
                if dc["max_km"] <= dc["min_km"]:
                    return f"ranking.distance_classes[{i}] has max_km <= min_km"

    sort_order = ranking.get("sort_order")
    if sort_order is not None:
        if not isinstance(sort_order, list):
            return f"ranking.sort_order must be a list, got {type(sort_order).__name__}"
        for i, rule in enumerate(sort_order):
            if not isinstance(rule, dict) or "field" not in rule:
                return f"ranking.sort_order[{i}] must be an object with a field key"
            # The type check has to come first: an unhashable value such as a
            # list makes the membership test below raise, and a validator that
            # raises turns a 400 into a 500.
            if not isinstance(rule["field"], str):
                return f"ranking.sort_order[{i}].field must be a string, got {rule['field']!r}"
            # A field that is not sortable is not merely ignored: _sort_key()
            # negates a descending value, so naming a string field raises
            # TypeError on every search once this config is live.
            if rule["field"] not in _SORTABLE_FIELDS:
                allowed = ", ".join(sorted(_SORTABLE_FIELDS))
                return f"ranking.sort_order[{i}].field must be one of {allowed}, got {rule['field']!r}"
            if "ascending" in rule and not isinstance(rule["ascending"], bool):
                return f"ranking.sort_order[{i}].ascending must be true or false, got {rule['ascending']!r}"

    search = cfg.get("search", {})
    if "default_radius_km" in search:
        radius = search["default_radius_km"]
        if not _is_number(radius) or radius <= 0:
            return f"search.default_radius_km must be a positive number, got {radius!r}"
    if "default_limit" in search:
        limit = search["default_limit"]
        # Stricter than a radius: this one is used as a slice bound, and
        # towers[:20.5] raises TypeError however positive 20.5 is.
        if not _is_number(limit) or isinstance(limit, float) or limit <= 0:
            return f"search.default_limit must be a positive whole number, got {limit!r}"

    return None


# The settings apply_config() assigns. Anything added there must be added here,
# or the test suite stops putting it back between tests and one test's config
# silently becomes the next one's.
CONFIG_SETTINGS = (
    "RX_ANTENNA_GAIN_DBI",
    "SENSITIVITY_DBM",
    "BROADCAST_BANDS",
    "BAND_PRIORITY",
    "DISTANCE_CLASSES",
    "DISTANCE_PRIORITY",
    "SORT_ORDER",
    "DEFAULT_RADIUS_KM",
    "DEFAULT_LIMIT",
)


def apply_config(cfg: dict) -> None:
    """Push a config dict into the module-level settings.

    Raises on a shape this cannot handle. Every value is computed into a local
    first and the globals are assigned only once all of them exist, so a config
    that fails partway leaves the previous one intact rather than a half-applied
    mix of old and new that concurrent requests would serve.

    PUT /api/config calls this directly, before writing anything, because it
    needs the failure in order to reject the write.
    """
    global RX_ANTENNA_GAIN_DBI, SENSITIVITY_DBM
    global BROADCAST_BANDS, BAND_PRIORITY
    global DISTANCE_CLASSES, DISTANCE_PRIORITY, SORT_ORDER
    global DEFAULT_RADIUS_KM, DEFAULT_LIMIT

    rx = cfg.get("receiver", {})
    rx_gain = rx.get("rx_antenna_gain_dbi", 6.0)
    sensitivity = rx.get("sensitivity_dbm", -95.0)

    bands = {band: [tuple(r) for r in ranges] for band, ranges in cfg.get("broadcast_bands", {}).items()}

    # The tables and rules below are copied rather than referenced: PUT
    # /api/config applies the parsed request body itself, so assigning the
    # objects nested inside it would let anything the handler does to that body
    # afterwards rewrite live ranking state.
    ranking = cfg.get("ranking", {})
    band_priority = dict(ranking.get("band_priority", {"VHF": 0, "UHF": 1, "FM": 2}))

    distance_classes = []
    for dc in ranking.get("distance_classes", []):
        max_km = dc["max_km"] if dc["max_km"] is not None else float("inf")
        distance_classes.append((dc["label"], dc["min_km"], max_km))

    distance_priority = dict(ranking.get("distance_priority", {}))
    # Unchanged from before coverage scoring was portable here: a config that
    # names no sort_order keeps ranking exactly as it did. The monolith leads
    # its own fallback with coverage_area_added_km2; adopting that here would
    # silently re-rank every deployment whose config omits the section.
    sort_order = [
        dict(rule)
        for rule in ranking.get(
            "sort_order",
            [
                {"field": "band_priority", "ascending": True},
                {"field": "distance_priority", "ascending": True},
                {"field": "received_power_dbm", "ascending": False},
            ],
        )
    ]

    search = cfg.get("search", {})
    radius_km = search.get("default_radius_km", 80)
    limit = search.get("default_limit", 20)

    # Nothing above this line touches module state, and nothing below it can fail.
    RX_ANTENNA_GAIN_DBI = rx_gain
    SENSITIVITY_DBM = sensitivity
    BROADCAST_BANDS = bands
    BAND_PRIORITY = band_priority
    DISTANCE_CLASSES = distance_classes
    DISTANCE_PRIORITY = distance_priority
    SORT_ORDER = sort_order
    DEFAULT_RADIUS_KM = radius_km
    DEFAULT_LIMIT = limit


def reload_config():
    """Re-read tower_config.json and update module-level settings.

    Validates before applying, so a config hand-edited inside the runtime volume
    is held to the same contract as one that arrives through PUT /api/config —
    the endpoint cannot be the only gate when the file it writes is a mounted
    volume an operator can edit directly.

    Raises rather than degrading: this runs at import, so an unusable overlay
    stops the container with the reason on stderr instead of booting a service
    whose every search would raise TypeError deep in the sort.
    """
    cfg = _load_config()
    error = validate_config(cfg)
    if error:
        raise ValueError(f"{_CONFIG_PATH} is not a usable tower config: {error}")
    apply_config(cfg)


# Seed every setting from the in-code defaults before any file is read, so they
# all exist whatever happens next. apply_config({}) takes no input that can
# vary, so unlike reload_config() it cannot fail on data.
apply_config({})

# Initialise on import.
reload_config()


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two points."""
    rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
    rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Bearing in degrees (0-360) from point 1 to point 2."""
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def bearing_to_cardinal(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    ix = round(deg / 22.5) % 16
    return dirs[ix]


def fspl(distance_km: float, freq_mhz: float) -> float:
    """Free-space path loss in dB."""
    if distance_km <= 0 or freq_mhz <= 0:
        return 0.0
    d_m = distance_km * 1000
    f_hz = freq_mhz * 1e6
    return 20 * math.log10(d_m) + 20 * math.log10(f_hz) - 147.55


def received_power(eirp_dbm: float, distance_km: float, freq_mhz: float) -> float:
    """Estimated received power (dBm) at a small directional antenna."""
    return eirp_dbm + RX_ANTENNA_GAIN_DBI - fspl(distance_km, freq_mhz)


def classify_band(freq_mhz: float) -> str | None:
    for band, ranges in BROADCAST_BANDS.items():
        for lo, hi in ranges:
            if lo <= freq_mhz <= hi:
                return band
    return None


def classify_distance(distance_km: float) -> str:
    for label, lo, hi in DISTANCE_CLASSES:
        if lo <= distance_km < hi:
            return label
    return "Far"


def watts_to_dbm(watts: float) -> float:
    """Convert watts to dBm. Returns -inf for zero/negative input."""
    if watts <= 0:
        return float("-inf")
    return 10 * math.log10(watts) + 30


def eirp_dbm_from_device(device: dict) -> float | None:
    """
    Extract or estimate EIRP in dBm from a device record.
    NOTE: Maprad stores power values in watts regardless of requested unit.
    """
    eirp = device.get("eirp")
    if eirp is not None:
        val = _as_float(eirp)
        if val is not None and val > 0:
            return watts_to_dbm(val)

    tp = device.get("transmitPower")
    gain = (device.get("antenna") or {}).get("gain")
    if tp is not None:
        tp_val = _as_float(tp)
        if tp_val is not None and tp_val > 0:
            tp_dbm = watts_to_dbm(tp_val)
            # antenna gain is in dBi
            antenna_gain = gain if gain is not None else 10.0
            return tp_dbm + antenna_gain

    return None


def _as_float(val) -> float | None:
    """Coerce a scalar value that might be float, int, string, or dict."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            return None
    if isinstance(val, dict):
        # FloatValueBlock might have a 'value' or 'low'/'high' key
        if "value" in val:
            return float(val["value"])
        if "low" in val and "high" in val:
            return (float(val["low"]) + float(val["high"])) / 2
    return None


def parse_geom(geom) -> tuple[float, float] | None:
    """
    Extract (latitude, longitude) from a Maprad geom field.
    Handles both POINT and POLYGON/MULTIPOLYGON (uses centroid).
    The API returns geom as {"string": "WKT"} dict.
    """
    if not geom:
        return None
    # The API wraps the WKT in a {"string": "..."} object
    if isinstance(geom, dict):
        geom = geom.get("string") or geom.get("wkt") or ""
    if not isinstance(geom, str) or not geom.strip():
        return None

    wkt = geom.strip().upper()

    if wkt.startswith("POINT"):
        try:
            inner = geom[geom.index("(") + 1 : geom.index(")")]
        except ValueError:
            # Malformed POINT WKT — missing opening or closing paren.
            return None
        parts = inner.split()
        if len(parts) >= 2:
            try:
                return float(parts[1]), float(parts[0])  # WKT is lng lat
            except ValueError:
                return None
        return None

    # For polygons / multipolygons, compute centroid from the first ring
    if "POLYGON" in wkt:
        return _polygon_centroid(geom)

    return None


def _polygon_centroid(wkt: str) -> tuple[float, float] | None:
    """Rough centroid: average of all coordinate pairs in the first ring."""
    # Find the first parenthesized coordinate sequence
    # MULTIPOLYGON has triple parens, POLYGON has double
    match = re.search(r"\(\([\(]?([-\d\.\s,]+)\)?", wkt)
    if not match:
        return None
    coords_str = match.group(1)
    lats, lngs = [], []
    for pair in coords_str.split(","):
        parts = pair.strip().split()
        if len(parts) >= 2:
            try:
                lngs.append(float(parts[0]))
                lats.append(float(parts[1]))
            except ValueError:
                continue
    if not lats:
        return None
    return sum(lats) / len(lats), sum(lngs) / len(lngs)


def _within_tolerance(diff: float, tolerance: float) -> bool:
    """Whether diff is within tolerance, boundary included.

    Rounds to 6 dp (nearest 1 Hz) so exact-boundary float noise doesn't
    reject a genuine match; the same rounding also admits a diff up to
    ~0.5 Hz past tolerance. Negligible: every tolerance in this module is
    kHz to MHz wide.
    """
    return round(diff, 6) <= tolerance


def _match_measurement(freq_mhz: float, band: str, measurements: list[dict]) -> dict | None:
    """Return the closest measurement to freq_mhz within the band-specific tolerance.

    If multiple measurements fall within tolerance, the one with the smallest
    frequency difference wins. Returns None when no measurement matches.
    """
    tolerance = MEASUREMENT_TOLERANCE_MHZ.get(band, 1.0)
    best: dict | None = None
    best_diff = float("inf")
    for m in measurements:
        diff = abs(m["freq_mhz"] - freq_mhz)
        if _within_tolerance(diff, tolerance) and diff < best_diff:
            best = m
            best_diff = diff
    return best


# Ceiling on the raw input length examined, applied before the comma split.
# Bounds the cost of a single comma-free value of any size reaching float(),
# independent of max_count (which only counts values already found valid).
_MAX_FREQUENCY_INPUT_CHARS = 2000


def parse_user_frequencies(raw: str, max_count: int = 10) -> list[float]:
    """Parse a comma-separated string of frequencies in MHz. Returns up to max_count valid values.

    Bounds its own cost via _MAX_FREQUENCY_INPUT_CHARS regardless of input size,
    so a caller (this module is called directly from tests, not only from the
    route) does not have to bound the input itself.
    """
    if not raw or not raw.strip():
        return []
    if len(raw) > _MAX_FREQUENCY_INPUT_CHARS:
        # Drop the truncated trailing token rather than parse a partial value.
        raw = raw[:_MAX_FREQUENCY_INPUT_CHARS].rsplit(",", 1)[0]
    freqs = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            val = float(part)
            if 0 < val < 10000:  # reasonable MHz range
                freqs.append(val)
        except ValueError:
            continue
        if len(freqs) >= max_count:
            break
    return freqs


def process_and_rank(
    raw_systems: list,
    user_lat: float,
    user_lon: float,
    limit: int = 0,
    radius_km: float = 0,
    measurements: list[dict] | None = None,
    user_frequencies: list[float] | None = None,
    allowed_bands: frozenset = ALL_BANDS,
    coverage_scorer=None,
) -> list:
    """
    Takes raw system records from Maprad/FCC, filters and ranks them
    for passive radar suitability.

    Args:
        limit: Max towers to return. 0 means use DEFAULT_LIMIT from config.
        radius_km: Search radius in km. Towers beyond this are excluded.
                   0 means use DEFAULT_RADIUS_KM.
        measurements: Optional list of spectrum-analyser measurement dicts
            (see models.measurements.Measurement).  Each tower that matches
            a measurement gains ``measured=True`` plus the analyser quality
            fields (``snr_db``, ``score``, ``power_db``, ``obw_fraction``)
            and ``frequency_matched=True``.  Unmatched towers carry
            ``measured=False`` and None for those fields.
        user_frequencies: Optional hand-typed frequencies in MHz (from
            GET ?frequencies=).  A tower within FREQUENCY_MATCH_TOLERANCE_MHZ
            of any of them gains ``frequency_matched=True`` and sorts ahead of
            unmatched towers; nothing is dropped, unlike ``measurements``.
        allowed_bands: Bands to keep. Defaults to unrestricted (ALL_BANDS);
            callers narrow it (e.g. FM_ONLY for non-ATSC regions) rather than
            opting out of a wider set.
        coverage_scorer: Optional callable(surviving tower list) -> None,
            annotating ``coverage_area_added_km2`` (and friends) on each tower
            in place — see services/tower_coverage.py. Run in a try/except:
            scoring must never break the towers endpoint. A config whose
            sort_order does not name a coverage field ignores the annotations.
    """
    effective_radius = radius_km if radius_km > 0 else DEFAULT_RADIUS_KM
    effective_limit = limit if limit > 0 else DEFAULT_LIMIT
    towers = []

    for system in raw_systems:
        licence = system.get("licence") or {}
        for device in system.get("devices") or []:
            freq_val = _as_float(device.get("frequency"))
            if freq_val is None:
                continue

            band = classify_band(freq_val)
            if band is None:
                continue  # not in a broadcast band

            if band not in allowed_bands:
                continue  # band withheld for this request (e.g. TV to a non-ATSC region)

            loc = device.get("location") or {}
            coords = parse_geom(loc.get("geom"))
            if coords is None:
                continue

            tower_lat, tower_lon = coords
            dist = haversine(user_lat, user_lon, tower_lat, tower_lon)

            # Filter by search radius
            if dist > effective_radius:
                continue

            eirp = eirp_dbm_from_device(device)
            if eirp is None:
                # Reasonable default for a broadcast tower
                eirp = 50.0 if band == "FM" else 60.0

            pwr = received_power(eirp, dist, freq_val)
            if pwr < SENSITIVITY_DBM:
                continue

            brg = initial_bearing(user_lat, user_lon, tower_lat, tower_lon)
            dist_class = classify_distance(dist)

            # Match against spectrum-analyser measurements (band-specific tolerance).
            measurement = _match_measurement(freq_val, band, measurements) if measurements else None
            freq_matched = measurement is not None
            if not freq_matched and user_frequencies:
                freq_matched = any(
                    _within_tolerance(abs(freq_val - uf), FREQUENCY_MATCH_TOLERANCE_MHZ) for uf in user_frequencies
                )

            towers.append(
                {
                    "callsign": device.get("callsign") or "",
                    "name": loc.get("name") or "",
                    "state": loc.get("state") or "",
                    "frequency_mhz": round(freq_val, 3),
                    "band": band,
                    "latitude": round(tower_lat, 6),
                    "longitude": round(tower_lon, 6),
                    "antenna_height_m": device.get("antennaHeight"),
                    "distance_km": round(dist, 1),
                    "bearing_deg": round(brg, 1),
                    "bearing_cardinal": bearing_to_cardinal(brg),
                    "received_power_dbm": round(pwr, 1),
                    "distance_class": dist_class,
                    "eirp_dbm": round(eirp, 1),
                    "licence_type": licence.get("type") or "",
                    "licence_subtype": licence.get("subtype") or "",
                    "frequency_matched": freq_matched,
                    # Spectrum-analyser fields — populated when a measurement matched, None otherwise.
                    "measured": measurement is not None,
                    "snr_db": measurement["snr_db"] if measurement else None,
                    "score": measurement["score"] if measurement else None,
                    "power_db": measurement["power_db"] if measurement else None,
                    "obw_fraction": measurement["obw_fraction"] if measurement else None,
                }
            )

    # Deduplicate by (callsign, frequency) — keep the strongest
    seen = {}
    for t in towers:
        key = (t["callsign"], t["frequency_mhz"])
        if key not in seen or t["received_power_dbm"] > seen[key]["received_power_dbm"]:
            seen[key] = t
    towers = list(seen.values())

    # When the SDR has provided measurements, only rank towers it can actually see.
    # Towers with no matching measurement are invisible to the radar — drop them.
    # (An empty measurements list means no scan data was sent; treat as no filter.)
    if measurements:
        # Keyed on `measured`, not `frequency_matched`: the latter is also set
        # by a hand-typed user frequency, which says nothing about what the SDR
        # can see and must not exempt a tower from this filter.
        towers = [t for t in towers if t["measured"]]

    # After the measured filter, not before it: scoring is the most expensive
    # step here and towers the SDR cannot see are about to be discarded. The
    # scores themselves are unaffected — one call stamps the same values onto
    # every tower it is given.
    if coverage_scorer is not None:
        try:
            coverage_scorer(towers)
        except Exception as exc:
            logger.warning("Coverage scoring failed: %s", exc)

    # Sort using configurable sort order.
    # If user frequencies were provided, frequency-matched towers sort first.
    has_user_freqs = bool(user_frequencies)

    def _sort_key(t):
        parts = []
        if has_user_freqs:
            parts.append(0 if t.get("frequency_matched") else 1)
        for rule in SORT_ORDER:
            # Everything this builds a sort tuple from is constrained to a number
            # by validate_config, on write and on load: the field named here
            # (_SORTABLE_FIELDS), and the BAND_PRIORITY / DISTANCE_PRIORITY
            # values read below. Loosening any of those gates reintroduces a
            # TypeError on every search. The `or 0` covers the fields that are
            # legitimately absent or None — an unmatched tower's analyser fields,
            # and the coverage annotations when no scorer ran.
            field = rule["field"]
            asc = rule.get("ascending", True)
            if field == "band_priority":
                val = BAND_PRIORITY.get(t["band"], 99)
            elif field == "distance_priority":
                val = DISTANCE_PRIORITY.get(t["distance_class"], 99)
            else:
                val = t.get(field) or 0
            parts.append(val if asc else -val)
        return tuple(parts)

    towers.sort(key=_sort_key)

    # Assign ranks
    for i, t in enumerate(towers[:effective_limit], 1):
        t["rank"] = i

    return towers[:effective_limit]
