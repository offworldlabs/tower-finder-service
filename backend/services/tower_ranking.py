import json
import logging
import math
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from services.tower_scoring import (
    DEFAULT_BAND_OFFSET_DB,
    DEFAULT_BAND_PARAMS,
    ScoringParams,
    score_towers,
)

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

# Records closer than this on the same frequency are one physical transmitter.
# FCC channel-sharing partners (two callsigns, one ATSC multiplex) and LPFM
# time-shares are licensed as separate stations at the same coordinates, and the
# coordinates of one site can differ by a few metres between records.
SHARED_TRANSMITTER_RADIUS_KM = 0.2

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
    # every search.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


# Fields a ranking.sort_order rule may name: a field belongs here only if it
# resolves to a real number for every tower.
#
# band_priority is special-cased in _sort_key() and always does; the rest are
# numeric keys of the tower dict built in process_and_rank(),
# plus coverage_area_added_km2, which services/tower_coverage.py adds. The
# analyser fields (snr_db, score, power_db, obw_fraction) and the two booleans
# are numeric-or-None, and _sort_key() reads every field as ``or 0``, so a None
# or a missing key sorts as zero rather than raising.
#
# Deliberately absent: the string fields (callsign, name, state, band,
# bearing_cardinal, licence_*), which _sort_key() would negate
# and raise TypeError on for a descending rule; and antenna_height_m, which is
# None whenever the upstream record omits it — it would not raise here, but a
# tower of unknown height ranking as a 0 m tower is a silent lie either way.
#
# This is the union of the two engines' sortable sets: the monolith's ranking
# fields and this service's analyser-measurement fields both validate, so
# switching ranking strategy is a config PUT rather than a code change. Adding a
# numeric field to the tower dict means adding it here too, or a config naming it
# is rejected.
#
# distance_priority used to be here. Towers no longer carry a distance class
# ("Too Close" / "Ideal" / "Good" / "Far"), so there is nothing for such a rule
# to sort on; see _drop_legacy_distance_rules() for how an overlay that still
# names it is handled.
_SORTABLE_FIELDS = frozenset(
    {
        # Shared with the monolith's engine.
        "band_priority",
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
        # The bistatic detection-area model (services/tower_scoring.py), which
        # the shipped sort_order now leads with. score_towers() stamps all
        # three on every tower it is given, so each is a real number whether or
        # not the model could score the tower.
        "expected_area_km2",
        "best_azimuth_deg",
        "horizon_km",
    }
)


# Keys of the optional `scoring` section, grouped by what validate_config has
# to prove about them before apply_config builds a ScoringParams. Every one is
# optional; the dataclass carries the default.
#
# The split is not cosmetic. A zero or negative grid_km divides by zero
# building the grid, a zero max_range_km or target_alt_km puts a 0 inside a
# log, and a non-integer n_azimuths is a range() bound — each would raise
# inside the scorer on every search, far from the config that caused it.
_SCORING_NUMBER_KEYS = (
    "cancellation_db",
    "snr_min_db",
    "noise_figure_db",
    "yagi_front_to_back_db",
    "rx_height_m",
)
_SCORING_POSITIVE_KEYS = (
    "target_rcs_m2",
    "target_alt_km",
    "grid_km",
    "max_range_km",
    "yagi_hpbw_deg",
)

# Ceiling on the grid the scoring disk is diced into. max_range_km/grid_km is
# squared into a cell count and every cell is held in memory for the azimuth
# sweep, so grid_km=0.01 over an 80 km disk is 256 million cells: not a slow
# search, an OOM-killed container that comes back and does it again.
_MAX_SCORING_CELLS = 2_000_000

# The scalar knobs apply_config copies out of the `scoring` section, as opposed
# to band_params (nested) and rx_gain_dbi (receiver.rx_antenna_gain_dbi). An
# unlisted key in the section is ignored rather than passed to ScoringParams,
# where it would be a TypeError on an unexpected keyword.
_SCORING_PARAM_KEYS = (
    *_SCORING_NUMBER_KEYS,
    *_SCORING_POSITIVE_KEYS,
    "max_bistatic_angle_deg",
    "n_azimuths",
)


def _validate_scoring(scoring: dict) -> str | None:
    """Return an error message if the `scoring` section is unusable, else None.

    Held to the same standard as the rest: everything the detection-area model
    puts in a log, a divisor or a range() has to be a number of the right kind
    here, because the alternative is a TypeError or a ZeroDivisionError inside
    numpy on every search with nothing pointing back at the config.
    """
    for key in _SCORING_NUMBER_KEYS:
        if key in scoring and not _is_number(scoring[key]):
            return f"scoring.{key} must be a number, got {scoring[key]!r}"

    for key in _SCORING_POSITIVE_KEYS:
        if key in scoring and (not _is_number(scoring[key]) or scoring[key] <= 0):
            return f"scoring.{key} must be a positive number, got {scoring[key]!r}"

    if "max_bistatic_angle_deg" in scoring:
        angle = scoring["max_bistatic_angle_deg"]
        if not _is_number(angle) or not 0 < angle <= 180:
            return f"scoring.max_bistatic_angle_deg must be a number in (0, 180], got {angle!r}"

    if "n_azimuths" in scoring:
        n_az = scoring["n_azimuths"]
        # A count, not a measurement: 12.5 boresights is np.arange(12.5) at
        # best and a silently different sweep at worst.
        if not _is_number(n_az) or isinstance(n_az, float) or n_az <= 0:
            return f"scoring.n_azimuths must be a positive whole number, got {n_az!r}"

    grid_km = scoring.get("grid_km", 2.0)
    max_range_km = scoring.get("max_range_km", 80.0)
    if _is_number(grid_km) and _is_number(max_range_km) and grid_km > 0:
        if grid_km > max_range_km:
            return f"scoring.grid_km ({grid_km!r}) must not exceed scoring.max_range_km ({max_range_km!r})"
        cells = (2 * int(max_range_km / grid_km) + 1) ** 2
        if cells > _MAX_SCORING_CELLS:
            return (
                f"scoring.grid_km ({grid_km!r}) over scoring.max_range_km ({max_range_km!r}) "
                f"grids {cells} cells, above the {_MAX_SCORING_CELLS} ceiling"
            )

    bands = scoring.get("band_params")
    if bands is not None:
        if not isinstance(bands, dict):
            return f"scoring.band_params must be an object, got {type(bands).__name__}"
        for band, params in bands.items():
            if not isinstance(params, dict):
                return f"scoring.band_params.{band} must be an object, got {type(params).__name__}"
            for key in ("bw_hz", "cpi_s"):
                # Both go into 10*log10(bw*cpi); zero or negative is -inf or a
                # math domain error, and a missing one is a KeyError per tower.
                if key not in params:
                    return f"scoring.band_params.{band} is missing {key}"
                if not _is_number(params[key]) or params[key] <= 0:
                    return f"scoring.band_params.{band}.{key} must be a positive number, got {params[key]!r}"

    return None


def validate_config(cfg: dict) -> str | None:
    """Return an error message if the tower config is unusable, else None.

    Covers exactly what apply_config() consumes, so anything accepted here can
    be applied without raising. Every section is optional — each has a default
    in apply_config() — but a section that is present must have the right shape.

    Everything a search consumes as a number has to be one, and three separate
    parts of the config feed that: the fields a sort_order rule names, the values
    in the band_priority table, and search.default_limit.
    _sort_key() puts the first two into a tuple it sorts on and negates them for
    a descending rule, and default_limit ends up as a slice bound. A string, a
    null or a fractional number in any of those places passes every structural
    check and then raises TypeError on every search, far from the config that
    caused it.
    """
    if not isinstance(cfg, dict):
        return f"config must be an object, got {type(cfg).__name__}"

    for section in ("receiver", "ranking", "search", "broadcast_bands", "scoring"):
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
    table = ranking.get("band_priority")
    if table is not None:
        if not isinstance(table, dict):
            return f"ranking.band_priority must be an object, got {type(table).__name__}"
        # _sort_key() reads these straight into the sort tuple, alongside the
        # literal 99 it falls back to, so a non-numeric value here is compared
        # against an int and raises rather than sorting oddly.
        for name, priority in table.items():
            if not _is_number(priority):
                return f"ranking.band_priority[{name!r}] must be a number, got {priority!r}"

    offsets = ranking.get("band_offset_db")
    if offsets is not None:
        if not isinstance(offsets, dict):
            return f"ranking.band_offset_db must be an object, got {type(offsets).__name__}"
        # Added to EIRP inside the scoring model, so a string here raises deep
        # in numpy on every search rather than being ignored.
        for name, offset in offsets.items():
            if not _is_number(offset):
                return f"ranking.band_offset_db[{name!r}] must be a number, got {offset!r}"

    scoring_error = _validate_scoring(cfg.get("scoring", {}))
    if scoring_error:
        return scoring_error

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
    "BAND_OFFSET_DB",
    "SORT_ORDER",
    "SCORING_PARAMS",
    "DEFAULT_RADIUS_KM",
    "DEFAULT_LIMIT",
)

# The sort_order of every default this image has shipped, newest first. An
# overlay still holding one of these has never been PUT to, so it is expressing
# "whatever the image ranks on", not a choice — and leaving it alone would mean
# a deployed volume quietly ranking on the old scheme forever, which is exactly
# how the distance rules survived their own removal. See
# _upgrade_legacy_default_sort(). The pre-2026-05-28 default led with
# distance_priority and reduces to the third entry here once
# _drop_legacy_distance_rules() has run over it.
_LEGACY_DEFAULT_SORT_ORDERS = (
    [
        {"field": "band_priority", "ascending": True},
        {"field": "score", "ascending": False},
        {"field": "received_power_dbm", "ascending": False},
    ],
    [
        {"field": "band_priority", "ascending": True},
        {"field": "score", "ascending": False},
    ],
    [
        {"field": "band_priority", "ascending": True},
        {"field": "received_power_dbm", "ascending": False},
    ],
)

# What a config naming no sort_order ranks on. Matches the shipped
# tower_config.json, so an overlay that omits the section ranks the same way as
# a fresh one.
#
# Detection area first, modelled received power as the tie-break: the model
# returns a multiple of the cell area, so towers genuinely do tie, and power is
# the more informative of the two orders within a tie. The measured analyser
# score is deliberately not here any more — it ranked a tower by how well the
# SDR hears the illuminator, which is what the model says is the wrong question.
_DEFAULT_SORT_ORDER = [
    {"field": "expected_area_km2", "ascending": False},
    {"field": "received_power_dbm", "ascending": False},
]


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
    global BROADCAST_BANDS, BAND_PRIORITY, BAND_OFFSET_DB, SORT_ORDER, SCORING_PARAMS
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
    # The old hard tier: TV bands tie, FM sorts after every TV tower whatever
    # its power. No longer in the shipped sort_order — band_offset_db below is
    # its successor — but still applied and still sortable, so an overlay that
    # names it keeps ranking the way it asked to.
    band_priority = dict(ranking.get("band_priority", {"VHF": 0, "UHF": 0, "FM": 1}))
    # The tier's successor: a per-band EIRP nudge the scoring model applies,
    # rather than a rank that no power can overcome. band_priority stays
    # applied and sortable for overlays that still name it.
    band_offset_db = dict(ranking.get("band_offset_db", DEFAULT_BAND_OFFSET_DB))

    sort_order = [dict(rule) for rule in ranking.get("sort_order", _DEFAULT_SORT_ORDER)]

    # Same copy-don't-alias discipline, one level deeper: band_params holds a
    # dict per band, and assigning those would leave the live scorer reading
    # the request body PUT /api/config parsed.
    scoring_cfg = cfg.get("scoring", {})
    band_params = {band: dict(params) for band, params in scoring_cfg.get("band_params", DEFAULT_BAND_PARAMS).items()}
    scoring_knobs = {key: scoring_cfg[key] for key in _SCORING_PARAM_KEYS if key in scoring_cfg}
    # rx_gain_dbi is deliberately not settable here: the scoring model and the
    # FSPL link budget must use the same receiver antenna, and that lives in
    # receiver.rx_antenna_gain_dbi. _scoring_params() folds it in at call time.
    scoring_params = ScoringParams(band_params=band_params, **scoring_knobs)

    search = cfg.get("search", {})
    radius_km = search.get("default_radius_km", 80)
    limit = search.get("default_limit", 20)

    # Nothing above this line touches module state, and nothing below it can fail.
    RX_ANTENNA_GAIN_DBI = rx_gain
    SENSITIVITY_DBM = sensitivity
    BROADCAST_BANDS = bands
    BAND_PRIORITY = band_priority
    BAND_OFFSET_DB = band_offset_db
    SORT_ORDER = sort_order
    SCORING_PARAMS = scoring_params
    DEFAULT_RADIUS_KM = radius_km
    DEFAULT_LIMIT = limit


def _scoring_params() -> ScoringParams:
    """The scoring knobs as one value, resolved at call time.

    The receiver gain and the band offsets live in sections of their own
    (receiver.rx_antenna_gain_dbi, ranking.band_offset_db) and are folded in
    here rather than baked into SCORING_PARAMS, so anything that assigns one of
    those globals — apply_config, or a test — cannot leave the scorer running
    on a stale copy of it.
    """
    return replace(SCORING_PARAMS, rx_gain_dbi=RX_ANTENNA_GAIN_DBI, band_offset_db=dict(BAND_OFFSET_DB))


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
    _drop_legacy_distance_rules(cfg)
    # After the distance rules are dropped, not before: that is what turns the
    # pre-2026-05-28 default into a shape this can recognise.
    _upgrade_legacy_default_sort(cfg)
    error = validate_config(cfg)
    if error:
        raise ValueError(f"{_CONFIG_PATH} is not a usable tower config: {error}")
    apply_config(cfg)


def _drop_legacy_distance_rules(cfg: dict) -> None:
    """Strip distance_priority sort rules from a config seeded before distance
    classes were removed.

    The runtime overlay is a persistent volume, seeded once from whatever
    default the image shipped at the time and never re-seeded. A default that
    shipped until 2026-05-28 sorted on distance_priority, so an overlay from
    then is still on disk in any environment nobody has PUT a config to since.
    Rejecting it would crash-loop the container on the first deploy after this
    change, over a rule that has nothing left to sort on. It is dropped here,
    with a warning, and the file on disk is left as it is: a PUT of the same
    body is still rejected, so nothing new can be written in this shape.

    The distance_classes and distance_priority tables such an overlay also
    carries are harmless: validate_config() and apply_config() no longer read
    them.
    """
    ranking = cfg.get("ranking") if isinstance(cfg, dict) else None
    sort_order = ranking.get("sort_order") if isinstance(ranking, dict) else None
    if not isinstance(sort_order, list):
        return
    kept = [rule for rule in sort_order if not (isinstance(rule, dict) and rule.get("field") == "distance_priority")]
    if len(kept) != len(sort_order):
        logger.warning(
            "%s names distance_priority in ranking.sort_order; towers no longer carry a distance class, "
            "so the rule is ignored. PUT /api/config to replace it.",
            _CONFIG_PATH,
        )
        ranking["sort_order"] = kept


def _upgrade_legacy_default_sort(cfg: dict) -> None:
    """Move an untouched overlay onto the current default sort_order.

    The runtime overlay is a persistent volume seeded once and never
    re-seeded, so changing the shipped default changes nothing in any
    environment that already has one — every deployment would keep ranking on
    band tier and received power for good, and the only visible symptom would
    be that the new ranking never appears. That is worse than the distance
    rules, which at least failed loudly.

    So an overlay whose sort_order is *exactly* one of the defaults this image
    has shipped is treated as "whatever the image ranks on" and upgraded in
    memory. Anything else is an operator's deliberate choice and is left
    alone, including a list that merely resembles a default.

    The file on disk is not rewritten: reload_config() runs at import and a
    surprise write into a mounted volume at boot is not something a deploy can
    undo. The warning says how to make the choice explicit.
    """
    ranking = cfg.get("ranking") if isinstance(cfg, dict) else None
    sort_order = ranking.get("sort_order") if isinstance(ranking, dict) else None
    if not isinstance(sort_order, list):
        return
    if sort_order == _DEFAULT_SORT_ORDER:
        return
    if not any(sort_order == legacy for legacy in _LEGACY_DEFAULT_SORT_ORDERS):
        return
    logger.warning(
        "%s still carries a shipped default ranking.sort_order (%s); ranking on expected detection area "
        "instead for this process. PUT /api/config to make the choice explicit either way.",
        _CONFIG_PATH,
        sort_order,
    )
    ranking["sort_order"] = [dict(rule) for rule in _DEFAULT_SORT_ORDER]


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


# Slack added to every tolerance comparison so exact-boundary float noise
# (abs(88.25 - 88.10) is 0.15000000000000568) doesn't reject a genuine match.
# 0.5 Hz against tolerances that are kHz to MHz wide.
_TOLERANCE_SLACK_MHZ = 5e-7


def _within_tolerance(diff: float, tolerance: float) -> bool:
    """Whether diff is within tolerance, boundary included."""
    return diff <= tolerance + _TOLERANCE_SLACK_MHZ


def _match_measurement(freq_mhz: float, band: str, measurements: list[dict]) -> dict | None:
    """Return the closest measurement to freq_mhz within the band-specific tolerance.

    If multiple measurements fall within tolerance, the one with the smallest
    frequency difference wins. Returns None when no measurement matches.
    """
    # Hoisted, not _within_tolerance per measurement: this runs once per
    # tower-by-measurement pair and a call frame here costs more than the
    # comparison inside it.
    limit = MEASUREMENT_TOLERANCE_MHZ.get(band, 1.0) + _TOLERANCE_SLACK_MHZ
    best: dict | None = None
    best_diff = float("inf")
    for m in measurements:
        diff = abs(m["freq_mhz"] - freq_mhz)
        if diff <= limit and diff < best_diff:
            best = m
            best_diff = diff
    return best


def parse_user_frequencies(raw: str | Sequence[str], max_count: int = 10) -> list[float]:
    """Parse frequencies in MHz, returning up to max_count valid values.

    Takes either one comma-separated string or the occurrences a repeated query
    key produces, each of which may itself be comma-separated. Nothing is
    joined and nothing is truncated, so a malformed value discards only itself.
    """
    if not raw:
        return []
    occurrences = (raw,) if isinstance(raw, str) else raw
    freqs: list[float] = []
    # No ceiling on tokens examined: one would drop a valid value sitting
    # behind enough empty or unparseable siblings. Cost is bounded by the
    # request size the transport allows, which is 8 kB through nginx but
    # 64 kB for a caller reaching the container directly on retina-edge.
    for occurrence in occurrences:
        for part in occurrence.split(","):
            part = part.strip()
            # Cheaper than letting float() raise: an unparseable token is the
            # dominant cost of a junk-heavy request, and this rejects most of
            # them without building an exception.
            if not part or not (part[0].isdigit() or part[0] in "+-."):
                continue
            try:
                val = float(part)
            except ValueError:
                continue
            if 0 < val < 10000:  # reasonable MHz range
                freqs.append(val)
                if len(freqs) >= max_count:
                    return freqs
    return freqs


def _merge_shared_transmitters(towers: list) -> list:
    """Collapse FCC channel-sharing pairs and LPFM time-shares into one row each.

    These are separate FCC licences (separate callsigns) but one physical
    illuminator: two callsigns broadcasting from the same transmitter on the
    same frequency, or an LPFM trio time-sharing one channel. Ranking or
    counting them twice would inflate the result list and put the same signal
    on the map at the same spot twice. A passive-radar node only needs the
    frequency and the site to use the signal as an illuminator — the extra
    callsigns are informational, not a second target.

    Towers are grouped by exact frequency_mhz match, then greedily clustered
    within a group in input order: a tower joins the first existing cluster
    whose anchor (its first member) is within SHARED_TRANSMITTER_RADIUS_KM,
    else it starts a new cluster. Each cluster collapses to its strongest
    member (by received_power_dbm; ties keep the earliest in input order),
    with the other members' callsigns attached as `shared_callsigns`.

    Every returned tower carries `shared_callsigns` (empty when it stands
    alone) so the response schema is stable whether or not a merge happened.
    """
    clusters: list[dict] = []
    for t in towers:
        freq = t["frequency_mhz"]
        cluster = next(
            (
                c
                for c in clusters
                if c["frequency"] == freq
                and haversine(t["latitude"], t["longitude"], c["anchor"]["latitude"], c["anchor"]["longitude"])
                <= SHARED_TRANSMITTER_RADIUS_KM
            ),
            None,
        )
        if cluster is not None:
            cluster["members"].append(t)
        else:
            clusters.append({"frequency": freq, "anchor": t, "members": [t]})

    merged = []
    for cluster in clusters:
        members = cluster["members"]
        primary = members[0]
        for m in members[1:]:
            if m["received_power_dbm"] > primary["received_power_dbm"]:
                primary = m
        shared_callsigns = sorted({m["callsign"] for m in members if m is not primary and m["callsign"]})
        collapsed = dict(primary)
        collapsed["shared_callsigns"] = shared_callsigns
        merged.append(collapsed)

    return merged


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

    Every returned tower carries ``expected_area_km2``, ``best_azimuth_deg``
    and ``horizon_km`` from the bistatic detection-area model
    (services/tower_scoring.py), on top of every field it carried before. The
    shipped sort_order ranks on the first of those; nothing was renamed or
    dropped, because retina-gui and retina-spectrum read this response.

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

    # Collapse FCC channel-sharing pairs and LPFM time-shares (distinct
    # callsigns, one physical transmitter) into a single row — the
    # (callsign, frequency) key above cannot catch these since the callsigns differ.
    towers = _merge_shared_transmitters(towers)

    # When the SDR has provided measurements, only rank towers it can actually see.
    # Towers with no matching measurement are invisible to the radar — drop them.
    # (An empty measurements list means no scan data was sent; treat as no filter.)
    if measurements:
        # Keyed on `measured`, not `frequency_matched`: the latter is also set
        # by a hand-typed user frequency, which says nothing about what the SDR
        # can see and must not exempt a tower from this filter.
        towers = [t for t in towers if t["measured"]]

    # After the measured filter, not before it: scoring is the most expensive
    # step here and towers the SDR cannot see are about to be discarded.
    # score_towers() is per-tower, so the saving is real, and it never reads
    # one tower to score another.
    try:
        score_towers(towers, user_lat, user_lon, _scoring_params())
    except Exception as exc:
        # Fail-soft like the coverage scorer, and for the same reason: a fault
        # in the model must not take the towers endpoint down with it. The
        # three fields are part of the response contract, so they are filled in
        # regardless and the sort falls through to its next rule. score_towers
        # writes them before it can raise; the setdefault below covers a fault
        # in building the params, before it was ever called.
        logger.warning("Detection-area scoring failed: %s", exc)
        for t in towers:
            t.setdefault("expected_area_km2", 0.0)
            t.setdefault("best_azimuth_deg", 0.0)
            t.setdefault("horizon_km", 0.0)

    # Fleet outcomes correct the modelled area before the sort sees it. The
    # import is deferred so a fault in the feedback store's module cannot stop
    # this one importing at app start; apply_feedback itself never raises.
    from services.tower_feedback import apply_feedback

    apply_feedback(towers, user_lat, user_lon)

    # Same place, same reason as above. The coverage scores themselves are
    # unaffected by the filter — one call stamps the same values onto every
    # tower it is given.
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
            # (_SORTABLE_FIELDS), and the BAND_PRIORITY values read below.
            # Loosening either gate reintroduces a TypeError on every search. The `or 0` covers the fields that are
            # legitimately absent or None — an unmatched tower's analyser fields,
            # and the coverage annotations when no scorer ran.
            field = rule["field"]
            asc = rule.get("ascending", True)
            if field == "band_priority":
                val = BAND_PRIORITY.get(t["band"], 99)
            else:
                val = t.get(field) or 0
            parts.append(val if asc else -val)
        return tuple(parts)

    towers.sort(key=_sort_key)

    # Assign ranks
    for i, t in enumerate(towers[:effective_limit], 1):
        t["rank"] = i

    return towers[:effective_limit]
