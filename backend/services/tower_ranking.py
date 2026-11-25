import heapq
import json
import logging
import math
import os
import re
import shutil
import statistics
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import numpy as np

from services.cell_sites import CellCandidates, CellDataChanged, row_order_key
from services.fleet_calibration import CALIBRATION_FILENAME, HARDWARE_TERMS, load_calibration
from services.tower_scoring import (
    DEFAULT_BAND_OFFSET_DB,
    DEFAULT_BAND_PARAMS,
    KM_PER_DEG,
    CellularScoring,
    ScoringParams,
    carrier_area_bounds,
    receiver_square_lacks_tiles,
    score_towers,
    sector_gain_db,
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
_CALIBRATION_PATH = _RUNTIME_DIR / CALIBRATION_FILENAME


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
# The cellular carrier fields (bandwidth_mhz, azimuth_deg, beamwidth_deg,
# confidence) are absent for the first reason: a broadcast row has none of them.
#
# Also absent, and not an oversight: the annotations the ordering itself
# produces — feedback_factor / feedback_n, diversity_penalty, site_channels,
# measurement_quality. They are outputs of a rank, so a sort_order naming one
# would rank on the answer to the question it is being asked.
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


# ── ranking.diversity: the MMR knobs ─────────────────────────────────────────
#
# A node tunes one centre frequency at a time, and Auto-Calibrate tries at most
# three candidates from the top of this list. A San Francisco search returns 14
# channels on Sutro Tower and 7 on Mount Diablo, so a list ordered on area alone
# spends every candidate slot on one mast: the same direct path, the same
# geometry and the same failure three times over. The diversity pass reorders so
# each slot is worth taking.
_DEFAULT_DIVERSITY = {
    "enabled": True,
    "lambda": 0.7,
    "same_site_same_band": 1.0,
    "same_site_other_band": 0.5,
    "bearing_sigma_deg": 30,
    "distance_sigma_km": 15,
    "other_band_factor": 0.5,
    "site_radius_km": 1.0,
}

# Similarities, and the lambda that scales them. They meet as a
# (1 - lambda * sim) multiplier on the tower's area: above 1 that multiplier
# goes negative and the most redundant tower sorts first, below 0 it promotes
# instead of demoting. Either way the pass does the opposite of its job on a
# config that looks plausible.
_DIVERSITY_UNIT_KEYS = ("lambda", "same_site_same_band", "same_site_other_band", "other_band_factor")

# Divisors: two Gaussian widths and the radius the site clustering greedily
# groups on. Zero divides by zero inside the similarity, and a negative radius
# makes every tower its own site, silently disabling the same-site rule.
_DIVERSITY_POSITIVE_KEYS = ("bearing_sigma_deg", "distance_sigma_km", "site_radius_km")


def _validate_diversity(diversity) -> str | None:
    """Return an error message if the `ranking.diversity` section is unusable, else None."""
    if not isinstance(diversity, dict):
        return f"ranking.diversity must be an object, got {type(diversity).__name__}"

    if "enabled" in diversity and not isinstance(diversity["enabled"], bool):
        return f"ranking.diversity.enabled must be true or false, got {diversity['enabled']!r}"

    for key in _DIVERSITY_UNIT_KEYS:
        if key in diversity and (not _is_number(diversity[key]) or not 0.0 <= diversity[key] <= 1.0):
            return f"ranking.diversity.{key} must be a number in [0, 1], got {diversity[key]!r}"

    for key in _DIVERSITY_POSITIVE_KEYS:
        if key in diversity and (not _is_number(diversity[key]) or diversity[key] <= 0):
            return f"ranking.diversity.{key} must be a positive number, got {diversity[key]!r}"

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

    for section in ("receiver", "ranking", "search", "broadcast_bands", "scoring", "propagation"):
        value = cfg.get(section)
        if value is not None and not isinstance(value, dict):
            return f"{section} must be an object, got {type(value).__name__}"

    propagation = cfg.get("propagation", {})
    if "model" in propagation and propagation["model"] not in PROPAGATION_MODELS:
        allowed = ", ".join(PROPAGATION_MODELS)
        return f"propagation.model must be one of {allowed}, got {propagation['model']!r}"
    if "environment" in propagation and propagation["environment"] not in ENVIRONMENTS:
        allowed = ", ".join(ENVIRONMENTS)
        return f"propagation.environment must be one of {allowed}, got {propagation['environment']!r}"
    for key in ("rx_height_m", "beam_tilt_deg", "max_underbeam_loss_db"):
        if key in propagation and (not _is_number(propagation[key]) or propagation[key] < 0):
            return f"propagation.{key} must be a non-negative number, got {propagation[key]!r}"
    beamwidths = propagation.get("vertical_beamwidth_deg")
    if beamwidths is not None:
        if not isinstance(beamwidths, dict):
            return f"propagation.vertical_beamwidth_deg must be an object of band: degrees, got {type(beamwidths).__name__}"
        # A zero here is a division by zero inside underbeam_loss() on every
        # search; a negative one makes the derating grow the wrong way.
        for band, width in beamwidths.items():
            if not _is_number(width) or width <= 0:
                return f"propagation.vertical_beamwidth_deg[{band!r}] must be a positive number, got {width!r}"

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

    diversity_error = _validate_diversity(ranking.get("diversity", {}))
    if diversity_error:
        return diversity_error

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
    "DIVERSITY",
    "SORT_ORDER",
    "SCORING_PARAMS",
    "DEFAULT_RADIUS_KM",
    "DEFAULT_LIMIT",
    "PROPAGATION_MODEL",
    "ENVIRONMENT",
    "RX_HEIGHT_M",
    "BEAM_TILT_DEG",
    "VERTICAL_BEAMWIDTH_DEG",
    "MAX_UNDERBEAM_LOSS_DB",
    "RX_HARDWARE_DESCRIPTORS",
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

PROPAGATION_MODELS = ("hata", "free_space")
ENVIRONMENTS = ("open", "suburban", "urban")

# Vertical half-power beamwidth of a typical broadcast transmit antenna, by
# band. A high-gain UHF panel array is a degree or two; VHF high-band a few;
# an FM multi-bay ring is wider. Unknown bands fall back to the VHF figure.
DEFAULT_VERTICAL_BEAMWIDTH_DEG = {"FM": 8.0, "VHF": 4.0, "UHF": 2.0}


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
    global BROADCAST_BANDS, BAND_PRIORITY, BAND_OFFSET_DB, DIVERSITY, SORT_ORDER, SCORING_PARAMS
    global DEFAULT_RADIUS_KM, DEFAULT_LIMIT
    global PROPAGATION_MODEL, ENVIRONMENT, RX_HEIGHT_M
    global BEAM_TILT_DEG, VERTICAL_BEAMWIDTH_DEG, MAX_UNDERBEAM_LOSS_DB, RX_HARDWARE_DESCRIPTORS

    rx = cfg.get("receiver", {})
    rx_hardware = {name: rx[name] for name in HARDWARE_TERMS if rx.get(name) is not None}
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

    # Read key by key rather than copied wholesale: every knob has a default, so
    # a section naming one of them still gets the rest, and a key the MMR pass
    # does not read is dropped here instead of sitting in live state looking
    # like it does something.
    diversity_cfg = ranking.get("diversity", {})
    diversity = {key: diversity_cfg.get(key, default) for key, default in _DEFAULT_DIVERSITY.items()}

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

    # An overlay written before this section existed gets the terrestrial
    # model, not free space: the point of the change is that every deployment
    # stops quoting free-space numbers, and a config PUT is not the trigger
    # for that. Free space is one line of config away for anyone who wants it.
    propagation = cfg.get("propagation", {})
    model = propagation.get("model", "hata")
    environment = propagation.get("environment", "suburban")
    rx_height = float(propagation.get("rx_height_m", 5.0))
    beam_tilt = float(propagation.get("beam_tilt_deg", 1.0))
    beamwidths = {
        **DEFAULT_VERTICAL_BEAMWIDTH_DEG,
        **{band: float(w) for band, w in propagation.get("vertical_beamwidth_deg", {}).items()},
    }
    max_underbeam = float(propagation.get("max_underbeam_loss_db", 20.0))

    # Nothing above this line touches module state, and nothing below it can fail.
    RX_ANTENNA_GAIN_DBI = rx_gain
    SENSITIVITY_DBM = sensitivity
    BROADCAST_BANDS = bands
    BAND_PRIORITY = band_priority
    BAND_OFFSET_DB = band_offset_db
    DIVERSITY = diversity
    SORT_ORDER = sort_order
    SCORING_PARAMS = scoring_params
    DEFAULT_RADIUS_KM = radius_km
    DEFAULT_LIMIT = limit
    PROPAGATION_MODEL = model
    ENVIRONMENT = environment
    RX_HEIGHT_M = rx_height
    BEAM_TILT_DEG = beam_tilt
    VERTICAL_BEAMWIDTH_DEG = beamwidths
    MAX_UNDERBEAM_LOSS_DB = max_underbeam
    RX_HARDWARE_DESCRIPTORS = rx_hardware


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


# ── Cellular carriers: parameters (backend/config/cellular_config.json) ──────
#
# Image-only, read once at import, and never part of tower_config.json: GET
# /api/config echoes that file byte for byte and PUT /api/config rewrites it,
# and neither may see or move these. Where each value comes from:
#
#   bands: the downlink groups the cell DB builder filters on (3GPP TS 36.101
#     Table 5.5-1 and TS 38.104 band edges), inclusive. A carrier in none of
#     them is dropped; broadcast classification never looks at this table.
#   default_bandwidth_mhz: used when a row carries no channel bandwidth. 10 MHz
#     is a standard LTE channel (1.4/3/5/10/15/20 MHz, NTIA "LTE (FDD)
#     Transmitter Characteristics") that fits every LTE band in these groups,
#     band 13 being exactly 10 MHz wide (TS 36.101). CELL3500 is mostly NR, whose
#     carriers are wider than any LTE channel; 20 MHz, the LTE maximum (B42/B48),
#     is the low end there.
#   radio_bandwidth_mhz: radios with one fixed channel width override the band
#     default: GSM 200 kHz, UMTS 5 MHz (3.84 Mcps), cdma2000 1x 1.25 MHz.
#   cpi_s: 0.2 s, the coherent integration time of the published LTE-downlink
#     UAV tracking experiment (Sun et al., arXiv:2412.20788, 5 MHz at 2.13 GHz).
#   reference_share_db: the part of a carrier's power that is on air whatever
#     the cell's load, which is all a ranking can count on (an idle cell sends
#     little else; Demissie and Steffes, IET RSN 2025, process the reference
#     elements alone). LTE: 2-port CRS, 8 resource elements per port in each
#     12 x 14 resource block pair (TS 36.211 section 6.10.1), 16/168 = 9.5%,
#     -10.2 dB. NR, approximate: NR has no always-on CRS; the SS/PBCH block
#     (240 subcarriers x 4 symbols, TS 38.211 section 7.4.3) repeated 8 times
#     per 20 ms over a 100 MHz, 30 kHz carrier is about 0.4% of its resource
#     elements, -24 dB, and TRS/CSI-RS add a configuration-dependent amount.
#     Chosen for the older radios: UMTS -10 dB, the CPICH share of cell power
#     common in network planning; cdma2000 -7 dB, the usual pilot Ec/Ior; GSM
#     0 dB, because the BCCH carrier is keyed in every timeslot (idle slots
#     carry dummy bursts) and the receiver correlates against all of it.
#     A row with no radio is processed as unknown_radio (LTE).
#   eirp: when a row has none, NTIA's 61 dBm per carrier at up to 5 MHz plus
#     10*log10(bw / 5) above that (about +3 dB per doubling; NTIA gives 64 dBm
#     above 10 MHz). The three ISED rows read at 5 MHz carry 60.8 to 61.0 dBm.
#   default_height_m: 30 m, the urban/suburban macro height in NTIA's table
#     and TR 36.942. Only for a missing height: a real 15 m mast stays 15 m.
#   sector: TR 36.942 v15.0.0 horizontal pattern (its Figure 4.1), 65 degree
#     3 dB beamwidth when the row has none, A_m = 20 dB.
#   vertical: the elevation pattern the received power pays (the cellular
#     counterpart of underbeam_loss), same parabolic form. Tilt 3 degrees down
#     (NTIA; the ISED sample reads 2 to 4) when the row has none; 10 degree
#     beamwidth from the gain-beamwidth estimate G ~ 26000 / (theta_h theta_v)
#     for the ~16 dBi, 65 degree panels in the ISED sample (approximate);
#     floored at the same 20 dB as the horizontal pattern.
#   grid: 0.5 km cells (0.25 km^2) out to 30 km, the 4/3-earth radio horizon of
#     a 30 m mast seen from the 10 m receiver (3.57 * (sqrt 30 + sqrt 10) =
#     30.8 km). ~11,300 cells: 500 carriers score well inside the request.
_CELLULAR_CONFIG_PATH = _SOURCE_DEFAULT_DIR / "cellular_config.json"

# (dotted key, lower bound, whether the bound itself is excluded). Each goes
# into a log, a divisor or a range below, like the scoring knobs above.
_CELLULAR_NUMBERS = (
    ("cpi_s", 0.0, True),
    ("eirp.reference_dbm", None, False),
    ("eirp.reference_bandwidth_mhz", 0.0, True),
    ("default_height_m", 0.0, True),
    ("sector.beamwidth_deg", 0.0, True),
    ("sector.max_attenuation_db", 0.0, False),
    ("vertical.beamwidth_deg", 0.0, True),
    ("vertical.tilt_deg", None, False),
    ("vertical.max_attenuation_db", 0.0, False),
    ("grid.grid_km", 0.0, True),
    ("grid.max_range_km", 0.0, True),
)


def _validate_cellular_config(cfg) -> str | None:
    """Return an error message if the cellular config is unusable, else None."""
    if not isinstance(cfg, dict):
        return f"config must be an object, got {type(cfg).__name__}"

    bands = cfg.get("bands")
    if not isinstance(bands, dict) or not bands:
        return "bands must be a non-empty object of band: [low, high]"
    for band, r in bands.items():
        if band in ALL_BANDS:
            return f"bands.{band} reuses a broadcast band name"
        if not isinstance(r, list) or len(r) != 2 or not all(_is_number(v) for v in r) or r[0] >= r[1]:
            return f"bands.{band} must be an ascending [low, high] pair of numbers, got {r!r}"

    for dotted, low, exclusive in _CELLULAR_NUMBERS:
        value = cfg
        for part in dotted.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if not _is_number(value) or (low is not None and (value <= low if exclusive else value < low)):
            kind = "a number" if low is None else ("a positive number" if exclusive else "a non-negative number")
            return f"{dotted} must be {kind}, got {value!r}"

    for table in ("default_bandwidth_mhz", "radio_bandwidth_mhz", "reference_share_db"):
        if not isinstance(cfg.get(table), dict):
            return f"{table} must be an object"
    for band in bands:
        width = cfg["default_bandwidth_mhz"].get(band)
        if not _is_number(width) or width <= 0:
            return f"default_bandwidth_mhz.{band} must be a positive number, got {width!r}"
    for radio, width in cfg["radio_bandwidth_mhz"].items():
        if not _is_number(width) or width <= 0:
            return f"radio_bandwidth_mhz.{radio} must be a positive number, got {width!r}"
    for radio, share in cfg["reference_share_db"].items():
        if not _is_number(share) or share > 0:
            return f"reference_share_db.{radio} must be a number of dB at or below 0, got {share!r}"
    if cfg.get("unknown_radio") not in cfg["reference_share_db"]:
        return f"unknown_radio must name a reference_share_db entry, got {cfg.get('unknown_radio')!r}"

    grid_km, max_range_km = cfg["grid"]["grid_km"], cfg["grid"]["max_range_km"]
    if grid_km > max_range_km:
        return f"grid.grid_km ({grid_km!r}) must not exceed grid.max_range_km ({max_range_km!r})"
    if (2 * int(max_range_km / grid_km) + 1) ** 2 > _MAX_SCORING_CELLS:
        return f"grid of {grid_km!r} km over {max_range_km!r} km is above the {_MAX_SCORING_CELLS} cell ceiling"
    return None


def _load_cellular_config(path: Path = _CELLULAR_CONFIG_PATH) -> dict:
    """Read and check the cellular config. Raises with the reason: this runs at
    import, so a broken file stops the container instead of failing searches."""
    try:
        with path.open() as f:
            cfg = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not a usable cellular config: {exc}") from exc
    error = _validate_cellular_config(cfg)
    if error:
        raise ValueError(f"{path} is not a usable cellular config: {error}")
    return cfg


CELLULAR_CONFIG = _load_cellular_config()
CELLULAR_BANDS = {band: (float(lo), float(hi)) for band, (lo, hi) in CELLULAR_CONFIG["bands"].items()}
CELLULAR_SCORING = CellularScoring(
    grid_km=float(CELLULAR_CONFIG["grid"]["grid_km"]),
    max_range_km=float(CELLULAR_CONFIG["grid"]["max_range_km"]),
    cpi_s=float(CELLULAR_CONFIG["cpi_s"]),
    reference_share_db={radio: float(db) for radio, db in CELLULAR_CONFIG["reference_share_db"].items()},
    unknown_radio_share_db=float(CELLULAR_CONFIG["reference_share_db"][CELLULAR_CONFIG["unknown_radio"]]),
    beamwidth_deg=float(CELLULAR_CONFIG["sector"]["beamwidth_deg"]),
    max_attenuation_db=float(CELLULAR_CONFIG["sector"]["max_attenuation_db"]),
)


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


# Okumura-Hata's stated validity. Inputs are clamped to it rather than refused:
# a 320 m mast or an FM frequency is a real tower, and the formula's excess over
# free space at the edge of its range is a better estimate for it than free
# space is. Below 1 km the excess is held at its 1 km value, so the loss keeps
# falling with distance the way free space does instead of flattening out.
HATA_FREQ_MHZ = (150.0, 1500.0)
HATA_TX_HEIGHT_M = (30.0, 200.0)
HATA_RX_HEIGHT_M = (1.0, 10.0)
HATA_MIN_DISTANCE_KM = 1.0


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(bounds[1], max(bounds[0], value))


def hata_excess_loss(
    distance_km: float,
    freq_mhz: float,
    tx_height_m: float,
    rx_height_m: float,
    environment: str,
) -> float:
    """Okumura-Hata loss in excess of free space, in dB, never negative.

    Free space is the floor because a tower you can see is at least free
    space away and the open-area correction can dip below it at short range
    from a tall mast. Everything beyond that floor is what a low receive
    antenna pays for terrain and clutter that the free-space model ignores,
    which is the whole difference between a tower's licensed EIRP and what a
    receiver a few metres off the ground hears: the earlier model was 20 to
    50 dB optimistic against measurements on a real installation.

    `environment` is Okumura's own vocabulary: "urban" (small/medium city
    form), "suburban", or "open" (nothing within a few hundred metres).
    """
    d = max(distance_km, HATA_MIN_DISTANCE_KM)
    f = _clamp(freq_mhz, HATA_FREQ_MHZ)
    hb = _clamp(tx_height_m, HATA_TX_HEIGHT_M)
    hm = _clamp(rx_height_m, HATA_RX_HEIGHT_M)
    log_f = math.log10(f)
    log_hb = math.log10(hb)
    a_hm = (1.1 * log_f - 0.7) * hm - (1.56 * log_f - 0.8)
    urban = 69.55 + 26.16 * log_f - 13.82 * log_hb - a_hm + (44.9 - 6.55 * log_hb) * math.log10(d)
    if environment == "suburban":
        loss = urban - 2 * (math.log10(f / 28.0)) ** 2 - 5.4
    elif environment == "open":
        loss = urban - 4.78 * log_f**2 + 18.33 * log_f - 40.94
    else:
        loss = urban
    return max(0.0, loss - fspl(d, f))


def path_loss(distance_km: float, freq_mhz: float, tx_height_m: float | None) -> tuple[float, float]:
    """(total path loss dB, the part of it in excess of free space).

    The excess is 0 under the free-space model, and under the terrestrial
    model when the tower's height is unknown it is computed for a 100 m mast,
    the same stand-in the sensitivity filter has always relied on implicitly:
    a tower with no height on record is still a tower, and refusing to model
    it would rank it on free space alone, above every tower that does have one.
    """
    free = fspl(distance_km, freq_mhz)
    if PROPAGATION_MODEL == "free_space" or distance_km <= 0 or freq_mhz <= 0:
        return free, 0.0
    height = tx_height_m if tx_height_m is not None and tx_height_m > 0 else 100.0
    excess = hata_excess_loss(distance_km, freq_mhz, height, RX_HEIGHT_M, ENVIRONMENT)
    return free + excess, excess


def underbeam_loss(distance_km: float, tx_height_m: float | None, band: str | None) -> float:
    """dB the licensed EIRP overstates a receiver sitting under the main beam.

    A broadcast antenna's power is concentrated in a beam a few degrees
    tall, aimed a degree or so below horizontal so it lands on the coverage
    area, not the sky. A receiver close to a tall mast sits well below that
    beam: 3 km from a 300 m mast is 6 degrees down, where a UHF panel array is
    20 dB or more below its peak. The model is a parabolic main lobe in dB,
    12 * (angle / half-power beamwidth)^2, capped at `max_underbeam_loss_db`
    to stand in for the null fill and sidelobes real arrays are built with.
    Distance is ground range and height is above ground: the site's own
    elevation above the receiver is not known here, so a hilltop mast is
    derated less than it should be, never more.
    """
    if MAX_UNDERBEAM_LOSS_DB <= 0 or distance_km <= 0 or tx_height_m is None or tx_height_m <= 0:
        return 0.0
    depression_deg = math.degrees(math.atan2(tx_height_m, distance_km * 1000.0))
    below_beam = depression_deg - BEAM_TILT_DEG
    if below_beam <= 0:
        return 0.0
    beamwidth = VERTICAL_BEAMWIDTH_DEG.get(band or "", DEFAULT_VERTICAL_BEAMWIDTH_DEG["VHF"])
    return min(MAX_UNDERBEAM_LOSS_DB, 12.0 * (below_beam / beamwidth) ** 2)


def received_power(
    eirp_dbm: float,
    distance_km: float,
    freq_mhz: float,
    tx_height_m: float | None = None,
    band: str | None = None,
) -> float:
    """Estimated received power (dBm) at a small directional antenna.

    What a receiver with `receiver.rx_antenna_gain_dbi` of gain, pointed at the
    tower and polarisation-matched, hears at the configured height. Not what an
    indoor whip hears: that installation-specific loss is the node's to measure.
    """
    loss, _ = path_loss(distance_km, freq_mhz, tx_height_m)
    return eirp_dbm + RX_ANTENNA_GAIN_DBI - loss - underbeam_loss(distance_km, tx_height_m, band)


def classify_band(freq_mhz: float) -> str | None:
    for band, ranges in BROADCAST_BANDS.items():
        for lo, hi in ranges:
            if lo <= freq_mhz <= hi:
                return band
    return None


def classify_cellular_band(freq_mhz: float) -> str | None:
    """The cellular band group a downlink frequency falls in, edges included."""
    for band, (lo, hi) in CELLULAR_BANDS.items():
        if lo <= freq_mhz <= hi:
            return band
    return None


# Where the cellular path loss switches from Okumura-Hata to COST 231 Hata.
OKUMURA_HATA_MAX_MHZ = 1500.0


def cellular_excess_loss(
    distance_km: float,
    freq_mhz: float,
    tx_height_m: float,
    rx_height_m: float,
    environment: str,
) -> float:
    """Path loss in excess of free space for a cellular carrier, in dB, never negative.

    hata_excess_loss with the two clamps a cell site breaks taken out:
      * Frequency. Okumura-Hata (valid 150-1500 MHz) up to 1500 MHz; above
        that COST 231 Hata (COST Action 231 final report, 1999),
        Okumura's urban curve refitted for 1500-2000 MHz:
        L = 46.3 + 33.9 log f - 13.82 log hb - a(hm) + (44.9 - 6.55 log hb) log d
        + Cm, with Cm = 0 dB, its medium-city value and the same city form
        hata_excess_loss calls "urban". Above 2000 MHz (CELL2100 to CELL3500)
        this is an extrapolation of COST 231 beyond its stated range: it keeps
        the 33.9 dB/decade frequency slope rather than inventing another, and
        is quoted as such. The suburban and open corrections are Okumura's,
        applied above the urban figure exactly as below 1500 MHz.
      * Transmitter height. The real height goes in: COST 231 states 30-200 m,
        but a 15 m mast lifted to 30 m would be modelled as the better site it
        is not. The receiver height keeps its 1-10 m clamp and the distance
        its 1 km floor, as in hata_excess_loss.
    Free space stays the floor, for the reason hata_excess_loss gives.
    """
    d = max(distance_km, HATA_MIN_DISTANCE_KM)
    hm = _clamp(rx_height_m, HATA_RX_HEIGHT_M)
    log_f = math.log10(freq_mhz)
    log_hb = math.log10(tx_height_m)
    a_hm = (1.1 * log_f - 0.7) * hm - (1.56 * log_f - 0.8)
    if freq_mhz <= OKUMURA_HATA_MAX_MHZ:
        urban = 69.55 + 26.16 * log_f
    else:
        urban = 46.3 + 33.9 * log_f
    urban += -13.82 * log_hb - a_hm + (44.9 - 6.55 * log_hb) * math.log10(d)
    if environment == "suburban":
        loss = urban - 2 * (math.log10(freq_mhz / 28.0)) ** 2 - 5.4
    elif environment == "open":
        loss = urban - 4.78 * log_f**2 + 18.33 * log_f - 40.94
    else:
        loss = urban
    return max(0.0, loss - fspl(d, freq_mhz))


def _cellular_link(
    eirp_dbm: float, distance_km: float, freq_mhz: float, tx_height_m: float, tilt_deg: float, sector_db: float
) -> tuple[float, float, float, float]:
    """(received power dBm, total path loss, excess path loss, elevation loss) of a carrier.

    received_power's cellular counterpart. The elevation loss is the vertical
    pattern (see the cellular config notes) at the receiver's depression angle
    below the antenna, the tilt taken off. It adds to the horizontal sector
    loss ``-sector_db``, and the sum is floored at the sector's A_m: TR 36.942's
    pattern has no direction further down than that.
    """
    free = fspl(distance_km, freq_mhz)
    excess = 0.0
    if PROPAGATION_MODEL != "free_space" and distance_km > 0:
        excess = cellular_excess_loss(distance_km, freq_mhz, tx_height_m, RX_HEIGHT_M, ENVIRONMENT)
    elevation = 0.0
    if distance_km > 0:
        vertical = CELLULAR_CONFIG["vertical"]
        depression = math.degrees(math.atan2(tx_height_m - RX_HEIGHT_M, distance_km * 1000.0))
        elevation = min(
            vertical["max_attenuation_db"], 12.0 * ((depression - tilt_deg) / vertical["beamwidth_deg"]) ** 2
        )
    pattern = min(elevation - sector_db, CELLULAR_SCORING.max_attenuation_db)
    return eirp_dbm + RX_ANTENNA_GAIN_DBI - free - excess - pattern, free + excess, excess, elevation


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


def _match_measurement(
    freq_mhz: float, band: str, measurements: list[dict], tolerance_mhz: float | None = None
) -> dict | None:
    """Return the closest measurement to freq_mhz within the band-specific tolerance.

    If multiple measurements fall within tolerance, the one with the smallest
    frequency difference wins. Returns None when no measurement matches.
    ``tolerance_mhz`` replaces the band's tolerance for a transmitter whose
    channel width is its own (a cellular carrier).
    """
    # Hoisted, not _within_tolerance per measurement: this runs once per
    # tower-by-measurement pair and a call frame here costs more than the
    # comparison inside it.
    tolerance = MEASUREMENT_TOLERANCE_MHZ.get(band, 1.0) if tolerance_mhz is None else tolerance_mhz
    limit = tolerance + _TOLERANCE_SLACK_MHZ
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


# ── Site-aware diversity ordering ────────────────────────────────────────────


# Below the km in a degree of latitude on EARTH_RADIUS_KM (111.19), so a band
# of radius / this many degrees is at least the radius tall.
_KM_PER_DEG_FLOOR = 111.0


def _site_id(lat: float, lon: float) -> str:
    """A site's stable name: its lead tower's coordinates, to ~11 m."""
    return f"{lat:.4f},{lon:.4f}"


def _assign_sites(towers: list[dict], radius_km: float, stamped: list[dict] | None = None) -> None:
    """Stamp ``site_id`` and ``site_channels`` on every tower, in place.

    Greedy clustering in list order, as _merge_shared_transmitters does it: a
    tower joins the first cluster whose lead is within ``radius_km``, else it
    leads one. Unlike that merge this crosses frequency and band — the point
    here is the mast, not the channel, and 14 channels on one mast is exactly
    what the diversity pass exists to notice.

    Leads are filed in cells of latitude bands at least ``radius_km`` tall and
    longitude columns at least as wide as two towers within the radius can be
    apart at the most poleward latitude here (from the haversine formula:
    cos(lat1) cos(lat2) sin^2(dlon / 2) <= sin^2(d / 2R)), widened, with a
    whole number of columns around the globe so the last is beside the first.
    A tower looks only at the leads of its own cell and the eight around it (no
    lead outside them is within the radius), and takes the earliest of them
    within it: the same lead as a scan of every lead. A tower whose longitude
    is not finite is within the radius of no lead and leads its own cluster.

    ``stamped``, when given, are the towers stamped; the clustering is still
    over every tower of ``towers``, in their order.
    """
    band_deg = radius_km / _KM_PER_DEG_FLOOR
    finite = [t["longitude"] for t in towers if math.isfinite(t["longitude"])]
    lon0 = finite[0] if finite else 0.0
    top_lat = min(max((abs(t["latitude"]) for t in towers), default=0.0) + band_deg, 90.0)
    reach = math.sin(radius_km / (2 * EARTH_RADIUS_KM)) / math.cos(math.radians(top_lat))
    width = 360.0 if not reach < 1 else math.degrees(2 * math.asin(reach)) * (1 + 1e-6) + 1e-9
    columns = max(1, int(360.0 // width))
    column_deg = 360.0 / columns

    leads = 0
    counts: dict[str, int] = {}
    site_of: dict[int, str] = {}
    by_cell: dict[tuple[int, int], list[tuple[int, dict]]] = {}
    for t in towers:
        lat, lon = t["latitude"], t["longitude"]
        band = math.floor(lat / band_deg)
        column = min(int(((lon - lon0) % 360.0) // column_deg), columns - 1) if math.isfinite(lon) else None
        lead, first = None, leads
        if column is not None:
            near = ((column - 1) % columns, column, (column + 1) % columns)  # a cell twice is only read twice
            for b in (band - 1, band, band + 1):
                for c in near:
                    for i, candidate in by_cell.get((b, c), ()):
                        if (
                            i < first
                            and haversine(lat, lon, candidate["latitude"], candidate["longitude"]) <= radius_km
                        ):
                            lead, first = candidate, i
        if lead is None:
            if column is not None:
                by_cell.setdefault((band, column), []).append((leads, t))
            leads += 1
            lead = t
        site = _site_id(lead["latitude"], lead["longitude"])
        site_of[id(t)] = site
        counts[site] = counts.get(site, 0) + 1

    for t in towers if stamped is None else stamped:
        t["site_id"] = site_of[id(t)]
        t["site_channels"] = counts[t["site_id"]]


def _diversity_similarity(a: dict, b: dict, cfg: dict) -> float:
    """How much of what ``b`` offers a second pick of ``a`` would repeat, in [0, 1].

    Same mast is the case that matters: one centre frequency at a time means a
    second channel from the same site buys a new signal but the same direct
    path, the same bearing and the same terrain. Otherwise the overlap falls off
    as a Gaussian in bearing and in range — two towers on opposite sides of the
    receiver illuminate different ground — halved again across bands, where the
    node has to retune anyway.
    """
    same_band = a.get("band") == b.get("band")
    if haversine(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) <= cfg["site_radius_km"]:
        return float(cfg["same_site_same_band"] if same_band else cfg["same_site_other_band"])

    # Wrapped: 350 degrees and 10 degrees are 20 apart, not 340.
    d_bearing = abs((float(a["bearing_deg"]) - float(b["bearing_deg"]) + 180.0) % 360.0 - 180.0)
    d_dist = abs(float(a["distance_km"]) - float(b["distance_km"]))
    sim = math.exp(-0.5 * (d_bearing / cfg["bearing_sigma_deg"]) ** 2) * math.exp(
        -0.5 * (d_dist / cfg["distance_sigma_km"]) ** 2
    )
    return float(sim if same_band else sim * cfg["other_band_factor"])


def _mmr_order(towers: list[dict], cfg: dict, picks: int | None = None) -> list[dict]:
    """Reorder by maximal marginal relevance on ``expected_area_km2``.

    Each pick maximises ``area * (1 - lambda * max_sim_to_already_picked)``, so
    the list reads as "the best tower that is not another view of the one above
    it". ``diversity_penalty`` records what the winner paid at the moment it was
    picked, which is the only honest place to record it: the penalty a tower
    would pay changes with every later pick.

    ``towers`` must arrive in the plain sorted order. The model returns a
    multiple of the cell area so towers genuinely tie, and a strict comparison
    below then keeps the configured order within a tie.

    The running ``max_sim`` is what keeps each pick O(n) rather than O(n^2): a
    new pick can only raise a remaining tower's similarity, never lower it.

    ``picks`` stops after that many picks, for a caller that keeps only the head
    of the list. Every pick depends on the picks before it and on nothing after,
    so the head is the same as that of the full order, at O(n * picks).
    """
    remaining = list(towers)
    max_sim = [0.0] * len(remaining)
    chosen: list[dict] = []

    while remaining and (picks is None or len(chosen) < picks):
        best_i = 0
        best_value = None
        for i, t in enumerate(remaining):
            value = (t.get("expected_area_km2") or 0.0) * (1.0 - cfg["lambda"] * max_sim[i])
            if best_value is None or value > best_value:
                best_i, best_value = i, value

        pick = remaining.pop(best_i)
        pick["diversity_penalty"] = float(cfg["lambda"] * max_sim.pop(best_i))
        chosen.append(pick)

        for i, t in enumerate(remaining):
            max_sim[i] = max(max_sim[i], _diversity_similarity(t, pick, cfg))

    return chosen


def _leads_with_expected_area(sort_order: list[dict]) -> bool:
    """Whether the configured sort ranks on the detection area, descending.

    The MMR value is that one field, so an operator who has ranked on anything
    else keeps their sort untouched rather than getting a diversity pass over a
    number their config does not use.
    """
    if not sort_order:
        return False
    first = sort_order[0]
    return first.get("field") == "expected_area_km2" and not first.get("ascending", True)


# ── Measurement calibration (POST /api/towers) ───────────────────────────────

# The bands whose measurements carry an absolute power reading. retina-spectrum
# sends power_db only for TV — the ATSC pilot peak, in dBFS — and null for FM,
# so an FM row has nothing to calibrate the model with.
_CALIBRATED_BANDS = frozenset({"VHF", "UHF"})

# Fewest matched TV towers an offset may be taken from. One tower is not a
# calibration: it is that tower's own terrain error, moved onto every other
# tower in the sweep and then called a measurement.
_MIN_CALIBRATION_TOWERS = 2


def _calibrate_direct_power(towers: list[dict]) -> tuple[float | None, int]:
    """Refer the sweep's measured powers to the modelled link budget, in place.

    ``power_db`` is dBFS at the node's front end, so it carries no absolute
    scale — but the differences between channels within one sweep are real, and
    terrain is the largest error in the FSPL the model otherwise falls back on.
    The median of (measured - modelled) over the matched TV towers is therefore
    taken as the sweep's fixed offset (front-end gain, conversion loss, the dBFS
    reference), and subtracting it leaves each tower's own residual on the
    modelled dBm scale, which is what ``direct_power_dbm_override`` wants.

    Median rather than mean: one tower behind a ridge is a large one-sided
    residual, and a mean would spread its terrain across every other tower.

    A cellular carrier takes no part at either end. Its band is not in
    _CALIBRATED_BANDS, and the offset is not applied to it either: the offset
    is the front end's scale on the TV channels it was taken from and says
    nothing about the gain at a carrier's frequency. A carrier keeps the
    modelled direct power whatever the analyser read for it.

    Returns the offset — None when there was too little to take one — and how
    many towers it was applied to.
    """
    residuals = [
        t["power_db"] - t["received_power_dbm"]
        for t in towers
        if t.get("measured") and t.get("band") in _CALIBRATED_BANDS and _is_number(t.get("power_db"))
    ]
    if len(residuals) < _MIN_CALIBRATION_TOWERS:
        return None, 0

    offset = statistics.median(residuals)
    calibrated = 0
    for t in towers:
        if t.get("measured") and t.get("kind") != "cellular" and _is_number(t.get("power_db")):
            t["direct_power_dbm_override"] = t["power_db"] - offset
            t["direct_power_source"] = "measured"
            calibrated += 1
    return offset, calibrated


def _apply_measurement_quality(towers: list[dict]) -> None:
    """Discount a measured tower's area by how good the measurement was.

    ``score`` is no longer a sort key — it says how well the SDR hears the
    illuminator, not how much ground the illuminator lights up — but a channel
    the analyser barely resolved is a worse bet than the model alone suggests.
    It becomes a multiplier in [0.5, 1] rather than an order: half the area at
    worst, so a poor score demotes a tower without deleting it.

    Deliberately never compared across towers: FM's score is an SNR ramp and
    TV's is absolute dBFS, so the two are on different scales upstream.
    """
    for t in towers:
        if not t.get("measured"):
            continue
        score = t.get("score")
        if not _is_number(score):
            continue
        # Clamped, not trusted: the scale is set upstream in another repo, and a
        # score above 1 would quietly multiply the area rather than discount it.
        quality = 0.5 + 0.5 * min(max(float(score), 0.0), 1.0)
        t["measurement_quality"] = quality
        area = t.get("expected_area_km2")
        if _is_number(area):
            t["expected_area_km2"] = area * quality


# Grid cell for the mast lookup below, in degrees. Wider than the borrowing
# radius, so a mast within that radius is always in the record's own cell or one
# of the eight around it, and the lookup reads nine cells instead of every mast.
_MAST_CELL_DEG = 0.01


def _mast_cell(lat: float, lon: float) -> tuple[int, int]:
    return (math.floor(lat / _MAST_CELL_DEG), math.floor(lon / _MAST_CELL_DEG))


def _mast_heights(raw_systems: list) -> dict[tuple[int, int], list[tuple[float, float, float]]]:
    """(lat, lon, antenna height) of every record that carries a height, by grid cell."""
    masts: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for system in raw_systems:
        for device in system.get("devices") or []:
            height = _as_float(device.get("antennaHeight"))
            if not height or height <= 0:
                continue
            coords = parse_geom((device.get("location") or {}).get("geom"))
            if coords is not None:
                masts.setdefault(_mast_cell(*coords), []).append((coords[0], coords[1], height))
    return masts


def _borrowed_mast_height(
    lat: float, lon: float, masts: dict[tuple[int, int], list[tuple[float, float, float]]]
) -> float | None:
    """Tallest height on record within SHARED_TRANSMITTER_RADIUS_KM, or None.

    The same radius the channel-sharing merge uses to call two records one
    transmitter: records of one mast differ by metres between sources, and a
    rounding bucket would split them at its edges, which is why the grid here
    is only an index and the radius is what decides.
    """
    cell_lat, cell_lon = _mast_cell(lat, lon)
    best = None
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for mast_lat, mast_lon, height in masts.get((cell_lat + di, cell_lon + dj), ()):
                if (best is None or height > best) and haversine(
                    lat, lon, mast_lat, mast_lon
                ) <= SHARED_TRANSMITTER_RADIUS_KM:
                    best = height
    return best


def _number_or_none(value) -> float | None:
    if type(value) is float:  # a REAL as sqlite3 reads it: _is_number's answer, without its checks
        return value if math.isfinite(value) else None
    return float(value) if _is_number(value) else None


def _cellular_tower(
    row: dict,
    user_lat: float,
    user_lon: float,
    radius_km: float,
    measurements: list[dict] | None,
    user_frequencies: list[float] | None,
) -> dict | None:
    """One cell DB row (schema v1 columns) as a tower row; None when it is not ranked.

    Same keys and rounding as a broadcast row, plus the carrier's own fields.
    ``antenna_height_m``, ``eirp_dbm`` and ``bandwidth_mhz`` are the values the
    link and the scoring used; every one of them this function had to default
    is added to the row's ``modelled`` list after the DB's own. A row out of
    the radius, in no cellular band or under the sensitivity is not ranked.
    """
    cfg = CELLULAR_CONFIG
    freq = _number_or_none(row.get("freq_mhz"))
    tower_lat = _number_or_none(row.get("lat"))
    tower_lon = _number_or_none(row.get("lon"))
    if freq is None or tower_lat is None or tower_lon is None:
        return None
    band = classify_cellular_band(freq)
    if band is None:
        return None  # in no cellular band group

    dist = haversine(user_lat, user_lon, tower_lat, tower_lon)
    if dist > radius_km:
        return None

    modelled = [m.strip() for m in (row.get("modelled") or "").split(",") if m.strip()]
    radio = row.get("radio")

    bandwidth = _number_or_none(row.get("bandwidth_mhz"))
    if bandwidth is None or bandwidth <= 0:
        bandwidth = float(cfg["radio_bandwidth_mhz"].get(radio, cfg["default_bandwidth_mhz"][band]))
        modelled.append("bandwidth")

    eirp = _number_or_none(row.get("eirp_dbm"))
    if eirp is None:
        # NTIA's 61 dBm covers carriers up to 5 MHz; wider ones scale with
        # their width (see the cellular config notes).
        reference_bw = cfg["eirp"]["reference_bandwidth_mhz"]
        eirp = cfg["eirp"]["reference_dbm"] + 10 * math.log10(max(bandwidth, reference_bw) / reference_bw)
        modelled.append("eirp")

    height = _number_or_none(row.get("height_m"))
    if height is None or height <= 0:
        height = float(cfg["default_height_m"])
        modelled.append("height")

    tilt = _number_or_none(row.get("tilt_deg"))
    if tilt is None:
        tilt = float(cfg["vertical"]["tilt_deg"])
        modelled.append("tilt")

    azimuth = _number_or_none(row.get("azimuth_deg"))
    beamwidth = _number_or_none(row.get("beamwidth_deg"))
    sector_db = 0.0  # omni when the azimuth is unknown
    if azimuth is not None:
        if beamwidth is None or beamwidth <= 0:
            modelled.append("beamwidth")
        sector_db = float(
            sector_gain_db(
                initial_bearing(tower_lat, tower_lon, user_lat, user_lon),
                azimuth,
                beamwidth if beamwidth is not None and beamwidth > 0 else CELLULAR_SCORING.beamwidth_deg,
                CELLULAR_SCORING.max_attenuation_db,
            )
        )

    pwr, total_loss, excess_loss, elevation_loss = _cellular_link(eirp, dist, freq, height, tilt, sector_db)
    if pwr < SENSITIVITY_DBM:
        return None

    brg = initial_bearing(user_lat, user_lon, tower_lat, tower_lon)
    # Half the channel either side of the DB centre: an analyser peak
    # anywhere inside the carrier is that carrier. Calibration neither
    # takes its offset from it nor applies one to it (_calibrate_direct_power).
    measurement = _match_measurement(freq, band, measurements, tolerance_mhz=bandwidth / 2) if measurements else None
    freq_matched = measurement is not None
    if not freq_matched and user_frequencies:
        freq_matched = any(_within_tolerance(abs(freq - uf), FREQUENCY_MATCH_TOLERANCE_MHZ) for uf in user_frequencies)

    site_id = row.get("site_id")
    cell_id = row.get("cell_id")
    callsign = f"{radio or 'CELL'} {site_id}" + (f"/{cell_id}" if cell_id is not None else "")
    return {
        "callsign": callsign,
        "name": row.get("operator") or "",
        "state": "",
        "frequency_mhz": round(freq, 3),
        "band": band,
        "latitude": round(tower_lat, 6),
        "longitude": round(tower_lon, 6),
        "antenna_height_m": height,
        "distance_km": round(dist, 1),
        "bearing_deg": round(brg, 1),
        "bearing_cardinal": bearing_to_cardinal(brg),
        "received_power_dbm": round(pwr, 1),
        "eirp_dbm": round(eirp, 1),
        "path_loss_db": round(total_loss, 1),
        "excess_path_loss_db": round(excess_loss, 1),
        "underbeam_loss_db": round(elevation_loss, 1),
        "licence_type": "Cellular",
        "licence_subtype": radio or "",
        "frequency_matched": freq_matched,
        "measured": measurement is not None,
        "snr_db": measurement["snr_db"] if measurement else None,
        "score": measurement["score"] if measurement else None,
        "power_db": measurement["power_db"] if measurement else None,
        "obw_fraction": measurement["obw_fraction"] if measurement else None,
        # Never merged (below), so never shared; present so both kinds
        # carry the same keys.
        "shared_callsigns": [],
        "kind": "cellular",
        "cell_site_id": site_id,
        "cell_id": cell_id,
        "operator": row.get("operator"),
        "radio": radio,
        "azimuth_deg": azimuth,
        "beamwidth_deg": beamwidth,
        "bandwidth_mhz": bandwidth,
        "data_source": row.get("source"),
        "licence": row.get("licence"),
        "modelled": list(dict.fromkeys(modelled)),
        "confidence": row.get("confidence"),
    }


def _carrier_key(tower: dict) -> tuple:
    """A carrier's identity: never its callsign, and with the azimuth when the cell id is unknown.

    Sectors of one site share a site id and a frequency and are different
    transmitters. The key holds the source and site id, so two rows of one
    carrier always belong to one site.
    """
    key = (tower["data_source"], tower["cell_site_id"], tower["cell_id"], tower["frequency_mhz"])
    return key + (tower["azimuth_deg"],) if tower["cell_id"] is None else key


def _cellular_towers(
    rows: list[dict],
    user_lat: float,
    user_lon: float,
    radius_km: float,
    measurements: list[dict] | None,
    user_frequencies: list[float] | None,
) -> list[dict]:
    """Cell DB rows (schema v1 columns) as tower rows (see _cellular_tower), deduped, not merged.

    Dedupe by _carrier_key, keeping the strongest, at the place of the key's
    first row. And no _merge_shared_transmitters: two sectors on one mast are
    not an FCC channel-sharing pair, and merging them would keep one sector's
    pattern and drop the others' coverage.
    """
    seen: dict[tuple, dict] = {}
    for row in rows:
        t = _cellular_tower(row, user_lat, user_lon, radius_km, measurements, user_frequencies)
        if t is None:
            continue
        key = _carrier_key(t)
        if key not in seen or t["received_power_dbm"] > seen[key]["received_power_dbm"]:
            seen[key] = t
    return list(seen.values())


# ── Cellular search over every carrier in the radius ─────────────────────────
#
# A dense city holds a few hundred thousand carriers inside the default radius,
# and scoring one costs about half a millisecond, so a search cannot score them
# all. It works in the order of an upper bound on each carrier's ranked value
# (tower_scoring.carrier_area_bounds, times the fleet feedback factor its own
# tower would get), and stops when no unscored carrier can enter the answer:
#
#   The answer is the MMR head of the sorted list (or the sorted head, with the
#   diversity pass off), per group when frequencies were typed. Each pick is the
#   first tower in sorted order whose area * (1 - lambda * max_sim) is largest.
#   The picks are made lazily from a heap of stale values (a value only falls as
#   picks are added): a scored tower at the top with a fresh value is the pick.
#   A channel at the top that has not been read is read whole (every row of
#   the source's site on that rounded frequency that can be ranked, measured
#   or not, which holds every row of each of its carriers, so each is deduped
#   as in the full list before unmeasured ones are dropped) and its carriers
#   go into the heap on their bounds; a carrier at the top gets the tighter
#   block bounds, then is scored. Unscored values are the bound times the
#   penalty, the similarity floored from the row's own position, band,
#   bearing and distance; a scored tower keeps that floor until it first
#   comes to the top, and the exact similarity from then on. An unscored entry
#   sorts ahead of a scored tower of equal value, so a tie is never decided by
#   a bound. The head is then the head over every carrier, value for value.
#
# Only site_id and site_channels differ from the full list's: they count the
# masts of the scored towers. The full ranking is still what process_and_rank
# does with a plain list of rows, and what it falls back to when the bound does
# not apply: a sort that does not lead with the area, a coverage scorer, or
# terrain tiles under the receiver (whose one decision per query reads every
# carrier's path). A fault anywhere in this search costs the answer its cell
# rows (CellRankingFailed), never a fall back to scoring every row.

_POSITION_SLACK_KM = 2e-4  # the stored position against the 6-decimal one score_towers reads, with room
_FLOAT32_REL = 1e-6  # relative error of the index's float32 columns, rounded up
_LINK_SLACK_DB = 1e-3  # numpy and float32 against _cellular_link, far above either's error
_CARRIERS_PER_BATCH = 32
# Below this many (tower, pick) pairs to catch up on, one exact similarity per pair beats numpy's overhead.
_SCALAR_PAIRS = 16
_REFINE_ROWS, _REFINE_SCAN = 256, 256  # how much one tightening call takes from the top of the heap
# Carriers per carrier_area_bounds call: each carrier's bound is its own, so
# the chunks only cap the (carriers x azimuths) arrays of the table bound and
# the (pairs x azimuths) arrays of the block bound one call holds.
_BOUND_CHUNK = 4_000
_BLOCK_CHUNK = 128
_BOUND_BLOCKS = (4, 2)  # block sizes a carrier's bound is tightened through before it is scored


def _np_haversine(lat1, lon1, lat2, lon2):
    rlat1, rlon1, rlat2, rlon2 = (np.radians(v) for v in (lat1, lon1, lat2, lon2))
    a = np.sin((rlat2 - rlat1) / 2) ** 2 + np.cos(rlat1) * np.cos(rlat2) * np.sin((rlon2 - rlon1) / 2) ** 2
    return EARTH_RADIUS_KM * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def _np_bearing(lat1, lon1, lat2, lon2):
    rlat1, rlat2, dlon = np.radians(lat1), np.radians(lat2), np.radians(lon2 - lon1)
    x = np.sin(dlon) * np.cos(rlat2)
    y = np.cos(rlat1) * np.sin(rlat2) - np.sin(rlat1) * np.cos(rlat2) * np.cos(dlon)
    return (np.degrees(np.arctan2(x, y)) + 360) % 360


def _np_cellular_received_dbm(eirp, dist, freq, height, tilt, sector_db):
    """_cellular_link's received power for arrays, the same formulas in numpy (see _LINK_SLACK_DB)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        free = np.where(dist > 0, 20 * np.log10(dist * 1000) + 20 * np.log10(freq * 1e6) - 147.55, 0.0)
        excess = np.zeros_like(dist)
        if PROPAGATION_MODEL != "free_space":
            d = np.maximum(dist, HATA_MIN_DISTANCE_KM)
            hm = _clamp(RX_HEIGHT_M, HATA_RX_HEIGHT_M)
            log_f, log_hb = np.log10(freq), np.log10(height)
            a_hm = (1.1 * log_f - 0.7) * hm - (1.56 * log_f - 0.8)
            urban = np.where(freq <= OKUMURA_HATA_MAX_MHZ, 69.55 + 26.16 * log_f, 46.3 + 33.9 * log_f)
            urban = urban - 13.82 * log_hb - a_hm + (44.9 - 6.55 * log_hb) * np.log10(d)
            if ENVIRONMENT == "suburban":
                loss = urban - 2 * np.log10(freq / 28.0) ** 2 - 5.4
            elif ENVIRONMENT == "open":
                loss = urban - 4.78 * log_f**2 + 18.33 * log_f - 40.94
            else:
                loss = urban
            free_d = 20 * np.log10(d * 1000) + 20 * np.log10(freq * 1e6) - 147.55
            excess = np.where(dist > 0, np.maximum(0.0, loss - free_d), 0.0)
        vertical = CELLULAR_CONFIG["vertical"]
        depression = np.degrees(np.arctan2(height - RX_HEIGHT_M, dist * 1000.0))
        elevation = np.minimum(
            vertical["max_attenuation_db"], 12.0 * ((depression - tilt) / vertical["beamwidth_deg"]) ** 2
        )
        elevation = np.where(dist > 0, elevation, 0.0)
    pattern = np.minimum(elevation - sector_db, CELLULAR_SCORING.max_attenuation_db)
    return eirp + RX_ANTENNA_GAIN_DBI - free - excess - pattern


def _similarity_floor(
    rows: dict, members: np.ndarray, picked: list[dict], cfg: dict, columns: dict | None = None
) -> np.ndarray:
    """Per row of ``members``, at most the largest _diversity_similarity of its tower to a ``picked`` tower.

    From the stored position, which the tower row rounds to 6 decimals, and
    the unrounded bearing and distance, which it rounds to 0.1: each gap is
    widened by what the rounding can close, the Gaussian taken there, and a
    pair that may fall either side of the site radius gets the smaller of the
    two answers. Only ever used to penalise a bound, so lower is safe.
    ``columns``, when given, is _pick_columns(picked), kept by the caller.
    """
    lat, lon = rows["lat"][members][:, None], rows["lon"][members][:, None]
    band = rows["band_name"][members][:, None]
    cols = _pick_columns(picked) if columns is None else columns
    p_lat, p_lon, p_band = cols["lat"][None, :], cols["lon"][None, :], cols["band"][None, :]
    same_band = band == p_band
    apart = _np_haversine(lat, lon, p_lat, p_lon)
    radius = cfg["site_radius_km"]
    surely_same = apart + _POSITION_SLACK_KM < radius
    surely_not = apart - _POSITION_SLACK_KM > radius
    same = np.where(same_band, cfg["same_site_same_band"], cfg["same_site_other_band"])
    d_bearing = np.abs((rows["bearing"][members][:, None] - cols["bearing"][None, :] + 180.0) % 360.0 - 180.0)
    d_dist = np.abs(rows["dist"][members][:, None] - cols["dist"][None, :])
    d_bearing = d_bearing + 0.05 + 1e-6
    d_dist = d_dist + 0.05 + 1e-6
    gauss = np.exp(-0.5 * (d_bearing / cfg["bearing_sigma_deg"]) ** 2) * np.exp(
        -0.5 * (d_dist / cfg["distance_sigma_km"]) ** 2
    )
    gauss = np.where(same_band, gauss, gauss * cfg["other_band_factor"]) * (1 - 1e-9)
    floor = np.where(surely_same, same, np.where(surely_not, gauss, np.minimum(same, gauss)))
    return np.maximum(floor, 0.0).max(axis=1)


def _pick_columns(picked: list[dict]) -> dict:
    """The fields _diversity_similarity reads of each tower in ``picked``, as arrays."""
    return {
        "lat": np.array([p["latitude"] for p in picked], dtype=float),
        "lon": np.array([p["longitude"] for p in picked], dtype=float),
        "band": np.array([p.get("band") for p in picked], dtype=object),
        "bearing": np.array([float(p["bearing_deg"]) for p in picked]),
        "dist": np.array([float(p["distance_km"]) for p in picked]),
    }


def _similarity_ceiling(items: dict, cols: dict, start: int, cfg: dict) -> np.ndarray:
    """Per tower of ``items`` (a row) and pick from ``start`` on (a column), at least _diversity_similarity(tower, pick).

    ``items`` and ``cols`` are _pick_columns of the towers and of the picks.
    _diversity_similarity's formulas in numpy, widened: the same float
    operations on the same values, so numpy's exp and haversine can drift from
    math's by a few ulps only: a pair within 1e-6 km of the site radius takes
    the larger of the two answers, and every value is raised by far more than
    that drift. NaN becomes infinity, so such a pick is always taken exactly.
    """
    lat, lon, band = cols["lat"][None, start:], cols["lon"][None, start:], cols["band"][None, start:]
    apart = _np_haversine(items["lat"][:, None], items["lon"][:, None], lat, lon)
    same_band = band == items["band"][:, None]
    same = np.where(same_band, cfg["same_site_same_band"], cfg["same_site_other_band"])
    d_bearing = np.abs((items["bearing"][:, None] - cols["bearing"][None, start:] + 180.0) % 360.0 - 180.0)
    d_dist = np.abs(items["dist"][:, None] - cols["dist"][None, start:])
    gauss = np.exp(-0.5 * (d_bearing / cfg["bearing_sigma_deg"]) ** 2) * np.exp(
        -0.5 * (d_dist / cfg["distance_sigma_km"]) ** 2
    )
    gauss = np.where(same_band, gauss, gauss * cfg["other_band_factor"])
    ceiling = np.where(apart <= cfg["site_radius_km"] + 1e-6, np.fmax(same, gauss), gauss)
    return np.nan_to_num(ceiling * (1 + 1e-9) + 1e-12, nan=np.inf)


class CellRankingFailed(RuntimeError):
    """The cellular search failed on something other than a change of the cell database."""


def _feedback_factors(lat: np.ndarray, lon: np.ndarray, freq: np.ndarray, groups: dict | None) -> np.ndarray:
    """The factor apply_feedback with ``groups`` gives each row's tower: 1 for a row in no group.

    A row is keyed as its tower is, by tower_key of the position and frequency
    _cellular_tower rounds; only rows within rounding reach of a group's key
    (under 1e-3 degrees and 100 kHz from it) are keyed at all.
    """
    from services import tower_feedback

    factor = np.ones(lat.size)
    for key, (weight, mean_log) in (groups or {}).items():
        try:
            key_lat, key_lon, key_hz = (float(v) for v in key.split("|"))
        except ValueError:
            continue  # no row's key has another form
        near = (np.abs(lat - key_lat) < 1e-3) & (np.abs(lon - key_lon) < 1e-3) & (np.abs(freq * 1e6 - key_hz) < 1e5)
        for i in np.flatnonzero(near).tolist():
            row_hz = round(float(freq[i]), 3) * 1e6
            if tower_feedback.tower_key(round(float(lat[i]), 6), round(float(lon[i]), 6), row_hz) == key:
                factor[i] = tower_feedback.group_factor(weight, mean_log)
    return factor


def _carrier_bounds(
    cands: CellCandidates,
    user_lat: float,
    user_lon: float,
    radius_km: float,
    measurements: list[dict] | None,
    user_frequencies: list[float] | None,
    params: ScoringParams,
    feedback: dict | None,
) -> dict:
    """Every row of the box that _cellular_tower can rank, with an upper bound on its ranked value.

    A row is dropped only where it surely is not ranked: out of every band, past
    the radius, or under the sensitivity by more than _LINK_SLACK_DB. ``heard``
    says which rows can match a measurement (every row without measurements):
    a row that cannot still takes part in its carrier's dedupe, where it can
    be the strongest row and so leave the carrier unmeasured. ``bound`` is
    carrier_area_bounds times the row's feedback factor from the ``feedback``
    groups (see _feedback_factors) for a heard row, 0 for any other, and
    ``finer(rows, block)`` the bound of those rows on blocks of that size;
    ``matched`` and ``unmatched`` say which side of the typed frequencies a row
    can fall on (both near a tolerance edge).
    """
    cfg = CELLULAR_CONFIG
    index, at = cands.index, cands.at
    lat, lon, freq = index.lat[at], index.lon[at], index.freq[at]
    bands = list(CELLULAR_BANDS)
    band = np.full(at.size, -1, dtype=np.int64)
    for code, (low, high) in enumerate(CELLULAR_BANDS.values()):
        band[(band < 0) & (freq >= low) & (freq <= high)] = code
    dist = _np_haversine(user_lat, user_lon, lat, lon)
    keep = (band >= 0) & np.isfinite(freq) & (dist <= radius_km * (1 + 1e-12) + 1e-9)
    at, lat, lon, freq, band, dist = (v[keep] for v in (at, lat, lon, freq, band, dist))

    radio_code = index.radio[at]
    radio_width = np.array([cfg["radio_bandwidth_mhz"].get(r, np.nan) for r in index.radios] or [np.nan])[radio_code]
    band_width = np.array([cfg["default_bandwidth_mhz"][b] for b in bands], dtype=float)[band]
    bandwidth = index.bandwidth[at].astype(float)
    bandwidth = np.where(
        np.isfinite(bandwidth) & (bandwidth > 0), bandwidth, np.where(np.isnan(radio_width), band_width, radio_width)
    )
    ref_bw = cfg["eirp"]["reference_bandwidth_mhz"]
    eirp = index.eirp[at].astype(float)
    eirp = np.where(
        np.isfinite(eirp), eirp, cfg["eirp"]["reference_dbm"] + 10 * np.log10(np.maximum(bandwidth, ref_bw) / ref_bw)
    )
    height = index.height[at].astype(float)
    height = np.where(np.isfinite(height) & (height > 0), height, float(cfg["default_height_m"]))
    tilt = index.tilt[at].astype(float)
    tilt = np.where(np.isfinite(tilt), tilt, float(cfg["vertical"]["tilt_deg"]))
    azimuth = index.azimuth[at].astype(float)
    azimuth = np.where(np.isfinite(azimuth), azimuth, np.nan)
    width = index.beamwidth[at].astype(float)
    width = np.where(np.isfinite(width) & (width > 0), width, CELLULAR_SCORING.beamwidth_deg)

    toward_rx = _np_bearing(lat, lon, user_lat, user_lon)
    off = np.abs((toward_rx - np.nan_to_num(azimuth) + 180.0) % 360.0 - 180.0)
    sector_db = np.where(
        np.isnan(azimuth), 0.0, -np.minimum(12.0 * (off / width) ** 2, CELLULAR_SCORING.max_attenuation_db)
    )
    received = _np_cellular_received_dbm(eirp, dist, freq, height, tilt, sector_db)
    keep = received >= SENSITIVITY_DBM - _LINK_SLACK_DB
    picks = {name: v[keep] for name, v in dict(at=at, lat=lat, lon=lon, freq=freq, band=band, dist=dist).items()}
    bandwidth, eirp, height, azimuth, width, radio_code = (
        v[keep] for v in (bandwidth, eirp, height, azimuth, width, radio_code)
    )
    heard = np.ones(picks["at"].size, dtype=bool)
    if measurements:
        seen = np.array([m["freq_mhz"] for m in measurements], dtype=float)
        limit = bandwidth / 2 + _TOLERANCE_SLACK_MHZ + 1e-9
        heard = (np.abs(seen[None, :] - picks["freq"][:, None]) <= limit[:, None]).any(axis=1)

    matched = unmatched = np.ones(picks["at"].size, dtype=bool)
    if user_frequencies:
        diff = np.abs(picks["freq"][:, None] - np.asarray(user_frequencies, dtype=float)[None, :])
        edge = FREQUENCY_MATCH_TOLERANCE_MHZ + _TOLERANCE_SLACK_MHZ
        matched = (diff <= edge + 1e-9).any(axis=1)
        unmatched = ~(diff <= edge - 1e-9).any(axis=1)

    share = np.array(
        [CELLULAR_SCORING.reference_share_db.get(r, CELLULAR_SCORING.unknown_radio_share_db) for r in index.radios]
    )
    share = share[radio_code]
    nr = CELLULAR_SCORING.reference_share_db.get("NR", CELLULAR_SCORING.unknown_radio_share_db)
    none_code = [i for i, r in enumerate(index.radios) if r is None]
    if none_code:
        share = np.where((radio_code == none_code[0]) & (bandwidth > 20.0), nr, share)
    offsets = np.array([float(params.band_offset_db.get(b, 0.0)) for b in bands])[picks["band"]]
    cos_lat = max(math.cos(math.radians(user_lat)), 0.01)
    x_km = ((picks["lon"] - user_lon + 180.0) % 360.0 - 180.0) * KM_PER_DEG * cos_lat
    y_km = (picks["lat"] - user_lat) * KM_PER_DEG
    inputs = (x_km, y_km, eirp, picks["freq"], bandwidth * 1e6, share, height, azimuth, width, offsets)
    factor = _feedback_factors(picks["lat"], picks["lon"], picks["freq"], feedback)

    def bounds(which, block: int) -> np.ndarray:
        if block and isinstance(which, np.ndarray) and which.size > _BLOCK_CHUNK:
            return np.concatenate(
                [bounds(which[i : i + _BLOCK_CHUNK], block) for i in range(0, which.size, _BLOCK_CHUNK)]
            )
        area = carrier_area_bounds(
            *(v[which] for v in inputs),
            params,
            CELLULAR_SCORING,
            terrain=False,
            position_km=_POSITION_SLACK_KM,
            rel=_FLOAT32_REL,
            block=block,
        )
        return area * factor[which]

    if heard.all():
        chunks = [bounds(slice(i, i + _BOUND_CHUNK), 0) for i in range(0, picks["at"].size, _BOUND_CHUNK)]
        bound = np.concatenate(chunks) if chunks else np.zeros(0)
    else:
        bound, at_heard = np.zeros(picks["at"].size), np.flatnonzero(heard)
        for i in range(0, at_heard.size, _BOUND_CHUNK):
            bound[at_heard[i : i + _BOUND_CHUNK]] = bounds(at_heard[i : i + _BOUND_CHUNK], 0)
    picks.update(bound=bound, matched=matched, unmatched=unmatched, heard=heard)
    picks["site"] = index.site[picks["at"]]
    picks["bearing"] = _np_bearing(user_lat, user_lon, picks["lat"], picks["lon"])
    picks["band_name"] = np.array(bands, dtype=object)[picks["band"]]
    picks["finer"] = bounds
    picks["omni"] = np.isnan(azimuth)
    return picks


def warm_cell_bounds() -> None:
    """Fill the tables carrier_area_bounds builds on first use, at every level _carrier_bounds asks for.

    Two made-up carriers (one sectored, one omni) under the current scoring
    params, so the first cellular search does not pay for the tables.
    """
    pair = np.ones(2)
    inputs = (pair * 3.0, pair * 4.0, pair * 60.0, pair * 2140.0, pair * 20e6, pair * 0.0, pair * 30.0)
    inputs += (np.array([90.0, np.nan]), pair * 65.0, pair * 0.0)
    params = _scoring_params()
    for block in (0, *_BOUND_BLOCKS):
        carrier_area_bounds(
            *inputs,
            params,
            CELLULAR_SCORING,
            terrain=False,
            position_km=_POSITION_SLACK_KM,
            rel=_FLOAT32_REL,
            block=block,
        )


def _sort_parts(tower: dict) -> tuple:
    """The SORT_ORDER part of process_and_rank's sort key."""
    parts = []
    for rule in SORT_ORDER:
        field = rule["field"]
        val = BAND_PRIORITY.get(tower["band"], 99) if field == "band_priority" else (tower.get(field) or 0)
        parts.append(val if rule.get("ascending", True) else -val)
    return tuple(parts)


def _rank_candidates(
    towers: list[dict],
    cands: CellCandidates,
    user_lat: float,
    user_lon: float,
    radius_km: float,
    limit: int,
    measurements: list[dict] | None,
    user_frequencies: list[float] | None,
    diagnostics: dict | None,
) -> list | None:
    """process_and_rank with every carrier of ``cands``, scoring only the sites that can reach the answer.

    ``towers`` are the broadcast rows as process_and_rank has them after its
    merge, stamped with their kind; they are copied, never changed. None when
    the bound does not apply here (see the block comment above), for the
    caller to rank every row instead. CellDataChanged passes through; any other
    fault is logged and raised as CellRankingFailed.
    """
    try:
        return _rank_by_bounds(
            towers, cands, user_lat, user_lon, radius_km, limit, measurements, user_frequencies, diagnostics
        )
    except CellDataChanged:
        raise
    except Exception as exc:
        logger.warning("Cellular ranking failed, answering without cell rows", exc_info=True)
        raise CellRankingFailed(str(exc)) from exc


def _rank_by_bounds(
    towers: list[dict],
    cands: CellCandidates,
    user_lat: float,
    user_lon: float,
    radius_km: float,
    limit: int,
    measurements: list[dict] | None,
    user_frequencies: list[float] | None,
    diagnostics: dict | None,
) -> list | None:
    """_rank_candidates without its fault handling."""
    if not _leads_with_expected_area(SORT_ORDER):
        return None
    use_mmr = bool(DIVERSITY["enabled"])
    lam = DIVERSITY["lambda"]
    params = _scoring_params()
    cellular = CELLULAR_SCORING

    broadcast = [dict(t) for t in towers if t["measured"] or not measurements]
    for t in broadcast:
        t["direct_power_dbm_override"] = None
        t["direct_power_source"] = "model"
        t["measurement_quality"] = None
    calibration_offset_db, calibrated_towers = _calibrate_direct_power(broadcast) if measurements else (None, 0)

    # The query's one terrain decision, as score_towers would make it over
    # every row: on the smooth earth when a tile under the receiver's square is
    # absent, whatever the towers. Otherwise every carrier's path decides it.
    any_broadcast = any(
        t.get("band") in params.band_params
        and _is_number(t.get("eirp_dbm"))
        and _is_number(t.get("frequency_mhz"))
        and t["frequency_mhz"] > 0
        for t in broadcast
    )
    if any_broadcast and params.max_range_km < cellular.max_range_km:
        return None
    reach_km = params.max_range_km if any_broadcast else cellular.max_range_km
    if not receiver_square_lacks_tiles(user_lat, user_lon, reach_km):
        return None
    from services.tower_feedback import apply_feedback, nearby_groups

    # One read of the feedback store for the whole search: every factor, bound
    # and stamp below comes from it. None (the store unreadable) ranks as
    # apply_feedback's fault does: no feedback keys, every factor 1.
    feedback = nearby_groups(user_lat, user_lon)

    try:
        calibration = load_calibration(_CALIBRATION_PATH)
    except Exception as exc:
        logger.warning("Fleet calibration unavailable: %s", exc)
        calibration = None
    score_args = (user_lat, user_lon, params)
    score_kwargs = dict(calibration=calibration, rx_descriptors=RX_HARDWARE_DESCRIPTORS, cellular=cellular)
    try:
        score_towers(broadcast, *score_args, **score_kwargs, smooth_earth=True)
    finally:
        for t in broadcast:
            t.pop("_fleet_descriptors", None)
            t.pop("_antenna", None)
    if measurements:
        _apply_measurement_quality(broadcast)
    if feedback is not None:
        apply_feedback(broadcast, user_lat, user_lon, groups=feedback)

    rows = _carrier_bounds(cands, user_lat, user_lon, radius_km, measurements, user_frequencies, params, feedback)
    # Rows grouped by site and the frequency a carrier's key rounds to: every
    # rankable row of a carrier, heard or not, lands in one group, so a group
    # read whole dedupes as the full list does. Called sites below, they are
    # read and bounded as units; only their heard rows give them a value.
    size = rows["at"].size
    freq_key = np.array([round(f, 3) for f in rows["freq"].tolist()])
    by_site = np.lexsort((freq_key, rows["site"]))
    apart = (np.diff(rows["site"][by_site]) != 0) | (np.diff(freq_key[by_site]) != 0)
    site_starts = np.flatnonzero(np.r_[True, apart]) if size else np.zeros(0, dtype=np.int64)
    site_ends = np.r_[site_starts[1:], size]
    site_of = np.empty(size, dtype=np.int64)
    site_of[by_site] = np.repeat(np.arange(site_starts.size), site_ends - site_starts)
    fetched = [False] * site_starts.size
    bound = rows["bound"].tolist()
    refined = [0] * size  # how many of _BOUND_BLOCKS a row's bound has been through
    # How many it goes through before its carrier is scored: an omni carrier
    # is scored straight off the table bound, as the block bounds rarely spare
    # the scoring of one and cost more than they save; a sectored carrier goes
    # through every level. A site is read once the row its value comes from
    # has been through the first, if it needs any.
    levels = len(_BOUND_BLOCKS)
    need = [0 if omni else levels for omni in rows["omni"].tolist()]
    groups = [rows["matched"], rows["unmatched"]] if user_frequencies else [np.ones(size, dtype=bool)]
    group_of = (lambda t: 0 if t.get("frequency_matched") else 1) if user_frequencies else (lambda t: 0)
    picks: list[list[dict]] = [[] for _ in groups]
    columns = [_pick_columns([]) for _ in groups]  # _pick_columns(picks[g]), extended at each pick
    penalty: dict[int, float] = {}
    heaps: list[list] = [[] for _ in groups]
    orders: list[np.ndarray] = []  # per group, its heard rows site by site: a site's rows are a slice
    # Per group, a floor of each heard row's max_sim, over the picks its site has seen.
    floors = [np.zeros(size) for _ in groups]
    in_group = [member & rows["heard"] for member in groups]
    uid = iter(range(1 << 62))
    origin: set[int] = set()  # id() of each read carrier
    carriers: list[dict] = []

    # Heap entries: (-value, 0 unscored | 1 scored, tie, uid, kind, state). An
    # unscored entry sorts ahead of a scored one of equal value. Kinds:
    #   "site": a site not read yet; state [site, its heard rows in the group
    #     (None until needed: orders[g][start:end]), start, picks seen, the
    #     row its value comes from, end]; value its rows' bounds, each
    #     penalised by the row's floor in floors[g].
    #   "carrier": a carrier of a read site, deduped, not scored; state
    #     [tower, row, max_sim, picks seen]; value its row's bound, made fine
    #     before the carrier is scored.
    #   "tower": a scored tower (or broadcast row); state [tower, max_sim,
    #     picks seen, tie]; value as _mmr_order computes it.

    def members_of(g: int, state: list) -> np.ndarray:
        if state[1] is None:
            state[1] = orders[g][state[2] : state[5]]
        return state[1]

    def catch_up(g: int, states: list[list]) -> None:
        # Each tower's max of _diversity_similarity over the picks since its
        # ``seen``, as _mmr_order takes it. A max does not depend on the order
        # it is taken in, and a pick whose ceiling is at most the running max
        # cannot raise it: so the picks go highest ceiling first, each exactly,
        # and stop at the first ceiling the max already reaches. The value is
        # the same. One ceiling call covers every tower of ``states``.
        count = len(picks[g])
        late = [state for state in states if state[2] < count] if use_mmr else []
        if late:
            first = min(state[2] for state in late)
            new = picks[g][first:]
            if sum(count - state[2] for state in late) < _SCALAR_PAIRS:
                for state in late:
                    item, sim = state[0], state[1]
                    for p in picks[g][state[2] :]:
                        sim = max(sim, _diversity_similarity(item, p, DIVERSITY))
                    state[1] = sim
            else:
                ceiling = _similarity_ceiling(_pick_columns([state[0] for state in late]), columns[g], first, DIVERSITY)
                for i, state in enumerate(late):
                    ceiling[i, : state[2] - first] = -np.inf  # seen already
                order = np.argsort(-ceiling, axis=1, kind="stable")
                ranked = np.take_along_axis(ceiling, order, axis=1)
                for state, js, cs in zip(late, order.tolist(), ranked.tolist()):
                    item, sim = state[0], state[1]
                    for j, c in zip(js, cs):
                        if c <= sim:
                            break
                        sim = max(sim, _diversity_similarity(item, new[j], DIVERSITY))
                    state[1] = sim
        for state in states:
            state[2] = count

    def since(g: int, seen: int) -> dict:
        return {name: col[seen:] for name, col in columns[g].items()}

    def penalised(value: float, sim: float) -> float:
        return value * (1.0 - lam * sim) if use_mmr else value

    def push_towers(states: list[list]) -> None:
        by_group: dict[int, list[list]] = {}
        for state in states:
            by_group.setdefault(group_of(state[0]), []).append(state)
        for g, members in by_group.items():
            catch_up(g, members)
            for state in members:
                value = penalised(state[0].get("expected_area_km2") or 0.0, state[1])
                heapq.heappush(heaps[g], (-value, 1, state[3], next(uid), "tower", state))

    def tower_state(t: dict) -> list:
        return [t, 0.0, 0, (_sort_parts(t), t["_order"])]  # max_sim from the tower itself, against every pick

    def push_carrier(t: dict, state: list) -> None:
        # state[2] is a floor of the carrier's max_sim, from its row (see push_site).
        g = group_of(t)
        if use_mmr and state[3] < len(picks[g]):
            k = np.array([state[1]])
            floor = _similarity_floor(rows, k, picks[g][state[3] :], DIVERSITY, since(g, state[3]))
            state[2] = max(state[2], float(floor[0]))
        state[3] = len(picks[g])
        heapq.heappush(heaps[g], (-penalised(bound[state[1]], state[2]), 0, (), next(uid), "carrier", state))

    def refresh(g: int, sites: list[list], carriers_: list[list], towers_: list[list]) -> None:
        # Entries of group g brought up to date and pushed back: the rows of
        # the stale sites and carriers floored in one call, the towers caught
        # up in one more.
        count = len(picks[g])
        live = [state for state in sites if not fetched[state[0]]]
        stale = [state for state in live if state[3] < count]
        late = [state for state in carriers_ if state[3] < count]
        if use_mmr and (stale or late):
            parts = [members_of(g, state) for state in stale]
            members = np.concatenate(parts + [np.array([state[1] for state in late], dtype=np.int64)])
            seen = min(state[3] for state in stale + late)
            floor = _similarity_floor(rows, members, picks[g][seen:], DIVERSITY, since(g, seen))
            of_sites = members.size - len(late)
            at = members[:of_sites]
            floors[g][at] = np.maximum(floors[g][at], floor[:of_sites])  # each row is one site's
            for state in stale:
                state[3] = count
            for state, value in zip(late, floor[of_sites:].tolist()):
                state[2] = max(state[2], value)
                state[3] = count
        for state in live:
            push_site(g, state)
        for state in carriers_:
            push_carrier(state[0], state)
        push_towers(towers_)

    def push_site(g: int, state: list) -> None:
        if state[5] - state[2] == 1 and (not use_mmr or state[3] == len(picks[g])):
            # One row, fresh: its value as the arrays below would give it, in the same float operations.
            k = state[4]
            value = bound[k] * (1.0 - lam * float(floors[g][k])) if use_mmr else bound[k]
            heapq.heappush(heaps[g], (-value, 0, (), next(uid), "site", state))
            return
        members = members_of(g, state)
        if use_mmr and state[3] < len(picks[g]):
            floor = _similarity_floor(rows, members, picks[g][state[3] :], DIVERSITY, since(g, state[3]))
            floors[g][members] = np.maximum(floors[g][members], floor)
        state[3] = len(picks[g])
        values = rows["bound"][members]
        if use_mmr:
            values = values * (1.0 - lam * floors[g][members])
        top = int(np.argmax(values))
        state[4] = int(members[top])  # the row the site's value comes from
        heapq.heappush(heaps[g], (-float(values[top]), 0, (), next(uid), "site", state))

    def refine(members: np.ndarray, level: int) -> None:
        if not members.size:
            return
        finer = rows["finer"](members, _BOUND_BLOCKS[level])
        for k, value in zip(members.tolist(), finer.tolist()):
            if refined[k] == level:
                bound[k] = min(bound[k], value)
                refined[k] = level + 1
        rows["bound"][members] = np.minimum(rows["bound"][members], finer)

    def read_sites(g: int, states: list[list]) -> None:
        # Every rankable row of each site, deduped as the full list dedupes
        # them, at the place of each carrier's first row in lookup_cells'
        # order. Each carrier starts from its row's similarity floor in the
        # group the site was read in, as fresh as the site's (a carrier of the
        # other group starts at 0). One read of the file for every site.
        cells: dict[int, list] = {}
        for state in states:
            fetched[state[0]] = True
            cells[state[0]] = []
        everything = np.concatenate([by_site[site_starts[state[0]] : site_ends[state[0]]] for state in states])
        at = rows["at"][everything]
        row_of_id = dict(zip(cands.index.id[at].tolist(), zip(everything.tolist(), site_of[everything].tolist())))
        for row in cands.rows_of(at):
            k, site = row_of_id[row["id"]]
            cells[site].append((row_order_key(row, user_lat, user_lon), k, row))
        for state in states:
            site_cells = cells[state[0]]
            site_cells.sort(key=lambda cell: cell[0])
            seen: dict[tuple, dict] = {}
            first: dict[tuple, tuple] = {}
            source: dict[int, int] = {}
            for order_key, k, row in site_cells:
                t = _cellular_tower(row, user_lat, user_lon, radius_km, measurements, user_frequencies)
                if t is None:
                    continue
                key = _carrier_key(t)
                if key not in seen:
                    first[key] = (1, *order_key)
                if key not in seen or t["received_power_dbm"] > seen[key]["received_power_dbm"]:
                    seen[key] = t
                    source[id(t)] = k
            for key, t in seen.items():
                if measurements and not t["measured"]:
                    continue
                t["_order"] = first[key]
                t["direct_power_dbm_override"] = None
                t["direct_power_source"] = "model"
                t["measurement_quality"] = None
                k = source[id(t)]
                origin.add(id(t))
                if group_of(t) == g and in_group[g][k]:
                    push_carrier(t, [t, k, float(floors[g][k]), state[3]])
                else:
                    push_carrier(t, [t, k, 0.0, 0])

    def score_carriers(batch: list[list]) -> None:
        towers_ = [state[0] for state in batch]
        score_towers(towers_, *score_args, **score_kwargs, smooth_earth=True, records=False)
        if measurements:
            _apply_measurement_quality(towers_)
        if feedback is not None:
            apply_feedback(towers_, user_lat, user_lon, groups=feedback)
        carriers.extend(towers_)
        for state in batch:
            # On its carrier's floor of max_sim, which later picks only raise:
            # its exact max_sim is taken, over every pick, when it first comes
            # to the top. Most scored towers never do.
            t, scored = state[0], tower_state(state[0])
            value = penalised(t.get("expected_area_km2") or 0.0, state[2])
            heapq.heappush(heaps[group_of(t)], (-value, 1, scored[3], next(uid), "tower", scored))

    for i, t in enumerate(broadcast):
        t["_order"] = (0, i)
    push_towers([tower_state(t) for t in broadcast])
    # Every site with a heard row in the group, valued on its best row's table
    # bound (the first such row, as np.argmax takes it), in one heapify.
    for g in range(len(groups)):
        order = by_site[in_group[g][by_site]]
        orders.append(order)
        if not order.size:
            continue
        site = site_of[order]
        starts = np.flatnonzero(np.r_[True, site[1:] != site[:-1]])
        ends = np.r_[starts[1:], order.size]
        values = rows["bound"][order]
        part = np.repeat(np.arange(starts.size), ends - starts)
        best = np.maximum.reduceat(values, starts)[part]
        hits = np.flatnonzero((values == best) | (np.isnan(values) & np.isnan(best)))
        tops = hits[np.r_[True, part[hits][1:] != part[hits][:-1]]]
        entries = zip(
            site[starts].tolist(), starts.tolist(), ends.tolist(), order[tops].tolist(), values[tops].tolist()
        )
        heaps[g] += [(-value, 0, (), next(uid), "site", [s, None, a, 0, k, b]) for s, a, b, k, value in entries]
        heapq.heapify(heaps[g])

    def unrefined_site(state: list) -> bool:
        return refined[state[4]] == 0 and need[state[4]] > 0

    def refine_top(g: int) -> None:
        # Tightening needs no fresh penalty, and a numpy call costs far more
        # than a row: every site not yet bounded on blocks and every carrier
        # short of the bound it is scored on among the next entries at the top
        # is tightened in one call per block size, the rest set back as they were.
        heap = heaps[g]
        sites, carriers_, aside, rows_ = [], [], [], 0
        for _ in range(_REFINE_SCAN):
            if not heap or rows_ >= _REFINE_ROWS:
                break
            entry = heapq.heappop(heap)
            kind, state = entry[4], entry[5]
            if kind == "site" and fetched[state[0]]:
                continue  # read since it was pushed: nothing left to rank
            if kind == "site" and unrefined_site(state):
                sites.append(state)
                rows_ += state[5] - state[2]
            elif kind == "carrier" and refined[state[1]] < need[state[1]]:
                carriers_.append(state)
                rows_ += 1
            else:
                aside.append(entry)
        # Of a site, the rows whose value could still reach the best entry left
        # in the heap, and always the row its value comes from: the rest keep
        # their table bound until the site comes back to the top or is read.
        cut = max([-entry[0] for entry in aside] + [-heap[0][0] if heap else -math.inf])
        tops = []
        for state in sites:
            members = members_of(g, state)
            values = rows["bound"][members]
            if use_mmr:
                values = values * (1.0 - lam * floors[g][members])
            tops += [members[values >= cut], [state[4]]]
        own = np.array([state[1] for state in carriers_], dtype=np.int64)
        first = np.unique(np.concatenate([np.asarray(m, dtype=np.int64) for m in tops] + [own]))
        for level in range(levels):
            # A site needs only the first level before it is read; a carrier's
            # row goes on through the next levels it needs in the same pass.
            at_level = first if level == 0 else own
            refine(at_level[[refined[k] == level and need[k] > level for k in at_level.tolist()]], level)
        for entry in aside:
            heapq.heappush(heap, entry)
        refresh(g, sites, carriers_, [])

    def fresh(g: int, entry: tuple) -> bool:
        kind, state = entry[4], entry[5]
        seen = state[3] if kind != "tower" else state[2]
        return not use_mmr or seen == len(picks[g])

    def refresh_top(g: int) -> None:
        # The stale entry at the top up to date, with the next stale entries
        # of any kind at the top, in one pass (a site not yet bounded on
        # blocks is tightened first, and a read one dropped).
        heap = heaps[g]
        stale: dict[str, list] = {"site": [], "carrier": [], "tower": []}
        taken = 0
        while heap and taken < 2 * _CARRIERS_PER_BATCH:
            kind, state = heap[0][4], heap[0][5]
            if kind == "site" and fetched[state[0]]:
                heapq.heappop(heap)
                continue
            if fresh(g, heap[0]) or (kind == "site" and unrefined_site(state)):
                break
            stale[kind].append(heapq.heappop(heap)[5])
            taken += 1
        refresh(g, stale["site"], stale["carrier"], stale["tower"])

    for g in range(len(groups)):
        target = limit - sum(len(p) for p in picks[:g])
        heap = heaps[g]
        while len(picks[g]) < target and heap:
            top = heap[0]
            kind, state = top[4], top[5]
            if kind == "site":
                if fetched[state[0]]:
                    heapq.heappop(heap)
                elif unrefined_site(state):
                    refine_top(g)
                elif not fresh(g, top):
                    refresh_top(g)
                else:
                    # Read with the next fresh sites at the top that are ready to be read, in one pass.
                    batch = [heapq.heappop(heap)[5]]
                    while heap and len(batch) < _CARRIERS_PER_BATCH and heap[0][4] == "site":
                        if fetched[heap[0][5][0]]:
                            heapq.heappop(heap)
                        elif unrefined_site(heap[0][5]) or not fresh(g, heap[0]):
                            break
                        else:
                            batch.append(heapq.heappop(heap)[5])
                    read_sites(g, batch)
            elif kind == "carrier":
                if not fresh(g, top):
                    refresh_top(g)
                    continue
                # With the next fresh carriers at the top that are in the
                # same state: unrefined ones get the fine bound and go back,
                # refined ones are scored.
                if refined[state[1]] >= need[state[1]]:
                    batch = [heapq.heappop(heap)[5]]
                    while (
                        heap
                        and len(batch) < _CARRIERS_PER_BATCH
                        and heap[0][4] == "carrier"
                        and fresh(g, heap[0])
                        and refined[heap[0][5][1]] >= need[heap[0][5][1]]
                    ):
                        batch.append(heapq.heappop(heap)[5])
                    score_carriers(batch)
                else:
                    refine_top(g)
            elif not fresh(g, top):
                refresh_top(g)
            else:
                heapq.heappop(heap)
                picks[g].append(state[0])
                columns[g] = {name: np.append(col, _pick_columns([state[0]])[name]) for name, col in columns[g].items()}
                penalty[id(state[0])] = float(lam * state[1]) if use_mmr else 0.0
    head = [t for group in picks for t in group][:limit]

    # The detection records of the answer's carriers, scored on copies of the
    # towers: a setting swapped mid-search cannot take a scored carrier out of
    # its own answer.
    ours = [t for t in head if id(t) in origin]
    copies = [dict(t) for t in ours]
    score_towers(copies, *score_args, **score_kwargs, smooth_earth=True)
    for t, copy in zip(ours, copies):
        t["expected_detection_area"] = copy["expected_detection_area"]

    # site_id and site_channels over every scored tower, as the block comment
    # above says; only the answer's towers are stamped, the rest are dropped.
    _assign_sites(broadcast + sorted(carriers, key=lambda t: t["_order"]), DIVERSITY["site_radius_km"], stamped=head)
    for i, t in enumerate(head, 1):
        t["diversity_penalty"] = penalty[id(t)]
        t["rank"] = i
        t.pop("_order", None)
    if diagnostics is not None:
        diagnostics["ranking"] = "expected_area_mmr" if use_mmr else "expected_area"
        diagnostics["calibration_offset_db"] = calibration_offset_db
        diagnostics["calibrated_towers"] = calibrated_towers
    return head


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
    diagnostics: dict | None = None,
    cellular_rows: list[dict] | None = None,
) -> list:
    """
    Takes raw system records from Maprad/FCC, filters and ranks them
    for passive radar suitability.

    Every returned tower carries ``expected_area_km2``, ``best_azimuth_deg``
    and ``horizon_km`` from the bistatic detection-area model
    (services/tower_scoring.py), the site-diversity annotations (``site_id``,
    ``site_channels``, ``diversity_penalty``) and the direct-path provenance
    (``direct_power_source``, ``direct_power_dbm_override``,
    ``measurement_quality``), on top of every field it carried before. The
    shipped sort_order ranks on the detection area; nothing was renamed or
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
        diagnostics: Optional dict filled in with what this call actually did:
            ``ranking`` (which ordering ran), ``calibration_offset_db`` and
            ``calibrated_towers``. An out-parameter rather than a richer return
            type, because the towers list is what every caller already expects
            back; the routes copy these into the response ``query``.
        cellular_rows: Optional cell DB rows (schema v1 column names, as
            services/cell_sites.py reads them). None is the broadcast-only
            ranking, rows without a ``kind`` key. A list, even an empty one,
            stamps ``kind: "broadcast"`` on the broadcast rows and ranks each
            carrier as a ``kind: "cellular"`` row (see _cellular_towers) through
            the same radius, sensitivity and measurement filters, scoring,
            feedback, sort, diversity pass and limit. ``allowed_bands`` gates
            broadcast rows only.
    """
    effective_radius = radius_km if radius_km > 0 else DEFAULT_RADIUS_KM
    effective_limit = limit if limit > 0 else DEFAULT_LIMIT
    towers = []
    masts = _mast_heights(raw_systems)

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

            tx_height = _as_float(device.get("antennaHeight"))
            if not tx_height or tx_height <= 0:
                # Low-power FCC records often carry 0 while the full-power
                # station on the same mast carries the real height. Same
                # mast, same beam: the derating must not depend on which
                # record it came from.
                tx_height = _borrowed_mast_height(tower_lat, tower_lon, masts)
            pwr = received_power(eirp, dist, freq_val, tx_height, band)
            if pwr < SENSITIVITY_DBM:
                continue
            total_loss, excess_loss = path_loss(dist, freq_val, tx_height)
            underbeam = underbeam_loss(dist, tx_height, band)

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
                    # How the power was arrived at, so a reader can see why a
                    # tower 3 km away is not the loudest thing on the list.
                    "path_loss_db": round(total_loss, 1),
                    "excess_path_loss_db": round(excess_loss, 1),
                    "underbeam_loss_db": round(underbeam, 1),
                    "licence_type": licence.get("type") or "",
                    "licence_subtype": licence.get("subtype") or "",
                    "frequency_matched": freq_matched,
                    # Spectrum-analyser fields — populated when a measurement matched, None otherwise.
                    "measured": measurement is not None,
                    "snr_db": measurement["snr_db"] if measurement else None,
                    "score": measurement["score"] if measurement else None,
                    "power_db": measurement["power_db"] if measurement else None,
                    "obw_fraction": measurement["obw_fraction"] if measurement else None,
                    # The descriptors the fleet calibration is evaluated on,
                    # as the callsign lookup reports them. Private: the scorer
                    # reads it and the caller drops it before the response.
                    "_fleet_descriptors": _fleet_descriptors(device, band, dist),
                    # The antenna the terrain profile starts from, as the
                    # licence states it (the FCC client's radiation-centre
                    # heights). Private, and dropped with the descriptors.
                    "_antenna": {
                        "rcamsl_m": device.get("_fcc_rcamsl"),
                        "rcagl_m": device.get("_fcc_rcagl"),
                        "polarisation": device.get("polarisation"),
                    },
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

    # Carriers join after the broadcast-only dedupe and merge, which they must
    # not go through (they dedupe on their own key), and before everything the
    # two kinds share.
    if cellular_rows is not None:
        for t in towers:
            t["kind"] = "broadcast"
        if isinstance(cellular_rows, CellCandidates):
            if coverage_scorer is None:
                ranked = _rank_candidates(
                    towers,
                    cellular_rows,
                    user_lat,
                    user_lon,
                    effective_radius,
                    effective_limit,
                    measurements,
                    user_frequencies,
                    diagnostics,
                )
                if ranked is not None:
                    return ranked
            cellular_rows = cellular_rows.all_rows()
        towers.extend(
            _cellular_towers(cellular_rows, user_lat, user_lon, effective_radius, measurements, user_frequencies)
        )

    # When the SDR has provided measurements, only rank towers it can actually see.
    # Towers with no matching measurement are invisible to the radar — drop them.
    # (An empty measurements list means no scan data was sent; treat as no filter.)
    if measurements:
        # Keyed on `measured`, not `frequency_matched`: the latter is also set
        # by a hand-typed user frequency, which says nothing about what the SDR
        # can see and must not exempt a tower from this filter.
        towers = [t for t in towers if t["measured"]]

    # Direct-path provenance, on every tower whether or not anything measured
    # it: a client reading direct_power_source must not have to tell "the model"
    # from "the key is missing", and score_towers reads the override by name.
    for t in towers:
        t["direct_power_dbm_override"] = None
        t["direct_power_source"] = "model"
        t["measurement_quality"] = None

    # Before the scoring, which is what consumes the override. GET has no sweep
    # to calibrate from, so it stays on the model.
    calibration_offset_db, calibrated_towers = _calibrate_direct_power(towers) if measurements else (None, 0)

    # After the measured filter, not before it: scoring is the most expensive
    # step here and towers the SDR cannot see are about to be discarded.
    # score_towers() is per-tower, so the saving is real, and it never reads
    # one tower to score another. Carriers go through the same call: it
    # scores them on their own terms, never on the fleet calibration, and
    # makes one terrain decision for every row of the answer.
    try:
        calibration = load_calibration(_CALIBRATION_PATH)
    except Exception as exc:
        logger.warning("Fleet calibration unavailable: %s", exc)
        calibration = None
    try:
        score_towers(
            towers,
            user_lat,
            user_lon,
            _scoring_params(),
            calibration=calibration,
            rx_descriptors=RX_HARDWARE_DESCRIPTORS,
            cellular=None if cellular_rows is None else CELLULAR_SCORING,
        )
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
            t.setdefault("expected_detection_area", None)
    finally:
        for t in towers:
            t.pop("_fleet_descriptors", None)
            t.pop("_antenna", None)

    # Between the model and the feedback: the analyser says how well this node
    # heard the channel today, which discounts the modelled area before anything
    # fleet-wide or order-related is applied to it.
    if measurements:
        _apply_measurement_quality(towers)

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

    # Once over the whole list, before any split or truncation: site_channels
    # answers "how many channels does this mast carry in this search", which a
    # per-group or post-limit count would understate.
    _assign_sites(towers, DIVERSITY["site_radius_km"])
    for t in towers:
        t["diversity_penalty"] = 0.0

    towers.sort(key=_sort_key)

    ranks_on_area = _leads_with_expected_area(SORT_ORDER)
    use_mmr = bool(DIVERSITY["enabled"]) and ranks_on_area
    if use_mmr:
        if has_user_freqs:
            # The matched-first split survives the diversity pass: it decides
            # which towers a caller asked about, which is not a preference MMR
            # is entitled to trade away. Each group is diversified on its own.
            matched = [t for t in towers if t.get("frequency_matched")]
            unmatched = [t for t in towers if not t.get("frequency_matched")]
            head = _mmr_order(matched, DIVERSITY, effective_limit)
            towers = head + _mmr_order(unmatched, DIVERSITY, effective_limit - len(head))
        else:
            # Only the returned head is ordered; the rest is cut below anyway.
            towers = _mmr_order(towers, DIVERSITY, effective_limit)

    # Assign ranks
    for i, t in enumerate(towers[:effective_limit], 1):
        t["rank"] = i

    if diagnostics is not None:
        diagnostics["ranking"] = (
            "expected_area_mmr" if use_mmr else ("expected_area" if ranks_on_area else "sort_order")
        )
        diagnostics["calibration_offset_db"] = calibration_offset_db
        diagnostics["calibrated_towers"] = calibrated_towers

    return towers[:effective_limit]


# ── Licence lookup ───────────────────────────────────────────────────────────


def licence_record(system: dict, device: dict, lat: float, lon: float, radius_km: float) -> dict | None:
    """One licence row as the callsign lookup reports it; None when unusable or beyond ``radius_km``.

    Every field it shares with a process_and_rank row is parsed and rounded the
    same way, so a tower reads the same on both routes. Power and height are
    what the licence states and nothing more: no per-band placeholder EIRP, no
    assumed antenna gain, no height borrowed from another record on the mast.
    A source with licensed figures of its own (the FCC client's
    ``_fcc_licensed_*``, where an FM licence states only a vertical ERP) has
    them read in place of ``eirp`` and ``antennaHeight``.
    """
    freq_val = _as_float(device.get("frequency"))
    loc = device.get("location") or {}
    coords = parse_geom(loc.get("geom"))
    if freq_val is None or not math.isfinite(freq_val) or coords is None:
        return None

    tower_lat, tower_lon = coords
    dist = haversine(lat, lon, tower_lat, tower_lon)
    # Ranking's radius filter, written so a NaN distance is out as well.
    if not dist <= radius_km:
        return None

    eirp = _licensed_eirp_dbm(device)
    polarisation = device.get("polarisation")
    return {
        "callsign": device.get("callsign") or "",
        "name": loc.get("name") or "",
        "frequency_mhz": round(freq_val, 3),
        "band": classify_band(freq_val),
        "latitude": round(tower_lat, 6),
        "longitude": round(tower_lon, 6),
        "eirp_dbm": round(eirp, 1) if eirp is not None else None,
        "antenna_height_m": device.get("_fcc_licensed_haat", device.get("antennaHeight")),
        "polarisation": polarisation if polarisation in ("horizontal", "vertical", "circular") else None,
        "licence_type": (system.get("licence") or {}).get("type") or "",
        "distance_km": round(dist, 1),
    }


def _fleet_descriptors(device: dict, band: str | None, distance_km: float) -> dict:
    """The tower-side inputs of the fleet calibration, as the callsign lookup reports them."""
    eirp = _licensed_eirp_dbm(device)
    height = _as_float(device.get("_fcc_licensed_haat", device.get("antennaHeight")))
    polarisation = device.get("polarisation")
    return {
        "band": band,
        "tower_eirp_dbm": round(eirp, 1) if eirp is not None else None,
        "tower_antenna_height_m": height if height is not None and math.isfinite(height) and height > 0 else None,
        "tower_polarisation": polarisation if polarisation in ("horizontal", "vertical", "circular") else None,
        "baseline_km": distance_km,
    }


def _licensed_eirp_dbm(device: dict) -> float | None:
    """The EIRP the record states, read as eirp_dbm_from_device reads it, but None where that would assume a gain."""
    eirp = _as_float(device.get("_fcc_licensed_eirp", device.get("eirp")))
    if eirp is not None and eirp > 0:
        return watts_to_dbm(eirp)
    tp = _as_float(device.get("transmitPower"))
    gain = _as_float((device.get("antenna") or {}).get("gain"))
    if tp is not None and tp > 0 and gain is not None:
        return watts_to_dbm(tp) + gain
    return None
