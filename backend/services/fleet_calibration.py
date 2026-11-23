"""Fleet calibration: the system constant retina fits over its fleet, and how to evaluate it.

retina measures a system constant S for each node it can: the per-node constant of the
radar equation, which holds the narrowbody RCS, the receiver gain, kTB with the noise
figure and the cancellation residue. It then fits one shrunk regression of S over the
descriptors a site can be given before any node runs there (the tower's band, EIRP,
antenna height and polarisation, the receiver-tower baseline, the receiver hardware)
and publishes the fit as a JSON document of kind ``retina-fleet-calibration``, schema 1.
This service reads it from ``<TOWER_FINDER_RUNTIME_DIR>/fleet_calibration.json``. The
document carries no node ids and no positions.

For a candidate's descriptors, encoded into columns x as `evaluate` describes:

    mean  = intercept_db + sum_j coefficient_db_j * x_j
    sigma = sqrt(tau_db^2 + x' C x + A)

where C (``covariance_db2``) is the fit's covariance over the intercept and every
column. ``tau_db`` is the node-to-node spread the descriptors cannot explain; sigma
widens it by the fit's own uncertainty. A descriptor the candidate lacks puts 0 in
every column of its term, which is the fleet average for that term, so a site
described by nothing gets the fleet-average prediction. A adds back what each such
term varies by across the fleet, so the band widens as the candidate says less.
retina evaluates the same document with the same arithmetic, and the two must agree
to 1e-9.

The ranking never reads it: ``expected_area_km2``, the best azimuth and the order stay
on the default constants whatever this file holds. It feeds only the per-class
detection-area figures reported beside them.

Another repo writes the file, so every field the schema pins is checked before
anything uses it. A missing, unreadable or off-schema document leaves the service on
its default constants, and each refused file version is logged once, not on every
request. Keys the schema does not pin are ignored, so retina can add fields without
breaking this reader. A document whose ``tau_at_bound`` is true is refused the same
way: its tau is the bound of the fit's search, not an estimate.
"""

import json
import logging
import math
import os
import reprlib
import threading
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

import numpy as np

logger = logging.getLogger(__name__)

CALIBRATION_FILENAME = "fleet_calibration.json"

_SCHEMA = 1
_KIND = "retina-fleet-calibration"
_TERM_KINDS = ("linear", "log10", "category")

# Relative tolerances on the covariance, against its largest entry and floored at a scale
# of 1 dB^2: asymmetry, and the negative eigenvalue (times the matrix size) a positive
# semi-definite matrix may show. They allow retina's rounding of every entry to 6
# decimals, which can leave a pair 1e-6 apart and a singular matrix slightly negative.
_SYMMETRY_TOLERANCE = 1e-6
_PSD_TOLERANCE = 1e-6

# retina's airframe classes, largest echo first.
CLASS_NAMES: tuple[str, ...] = (
    "military_transport",
    "widebody",
    "regional_jet",
    "narrowbody",
    "turboprop",
    "bizjet",
    "light_GA",
    "helicopter",
)

# Excess SNR per class in dB against narrowbody: retina's default class table, the one
# it uses before the fleet has measured its own. The calibration document carries the
# measured table in ``class_offsets_db``.
DEFAULT_CLASS_OFFSETS_DB: Mapping[str, float] = MappingProxyType(
    dict(zip(CLASS_NAMES, (1.14, 0.77, 0.26, 0.0, -0.11, -0.23, -0.43, -0.70), strict=True))
)

# The descriptor names retina's fit knows. Tower terms describe the illuminator and the
# geometry, so a candidate site has them; hardware terms describe the receiver and exist
# only once a node reports them. A document may name other terms: they are accepted and
# simply never get a descriptor here.
TOWER_TERMS: tuple[str, ...] = ("band", "tower_eirp_dbm", "tower_antenna_height_m", "tower_polarisation", "baseline_km")
HARDWARE_TERMS: tuple[str, ...] = (
    "sdr_model",
    "lna_state",
    "antenna_model",
    "antenna_polarisation",
    "local_adsb_receiver",
    "notch_dab",
    "notch_rf",
    "gain_reduction_ref_db",
    "gain_reduction_surv_db",
    "antenna_gain_dbi",
    "feedline_loss_db",
)

_MISSING = object()

_repr = reprlib.Repr()
_repr.maxstring = 40
_repr.maxother = 40

# Resolved path -> (file version, validated document or None). A version is
# (st_ino, st_mtime_ns, st_ctime_ns, st_size), or ("unstatable", errno) for a file that
# exists but cannot be stat'ed. The inode and the change time catch a replace in place
# and a permission change, which leave the modification time and the size as they were.
# The lock keeps two requests that find the same new version from both reading it and
# both logging its refusal.
_cache: dict[str, tuple[tuple[object, ...], dict | None]] = {}
_lock = threading.Lock()


def load_calibration(path: Path) -> dict | None:
    """The validated calibration document at ``path``, or None to stay on the default constants.

    Cached per file version, (st_ino, st_mtime_ns, st_ctime_ns, st_size): an unchanged file
    is neither re-read nor re-validated, and a changed one is. A missing file is the normal uncalibrated state
    and logs at debug only. A valid document with ``tau_at_bound`` true is refused. An unreadable, malformed or
    refused file logs one warning per version, however often it is asked for. Never raises. The returned dict
    is shared by every caller, so read it and never change it.
    """
    path = Path(path)
    key = _resolved(path)
    with _lock:
        try:
            stat = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            _cache.pop(key, None)
            logger.debug("No fleet calibration at %s; using the default constants", path)
            return None
        except OSError as exc:
            version: tuple[object, ...] = ("unstatable", exc.errno)
            failure: str | None = f"cannot stat it: {exc}"
        else:
            version = _file_version(stat)
            failure = None
        cached = _cache.get(key)
        if cached is not None and cached[0] == version:
            return cached[1]
        document = None
        if failure is None:
            version, document, failure = _read(path, version)
        _cache[key] = (version, document)
    if failure is not None:
        logger.warning("Fleet calibration %s ignored (%s); using the default constants", path, failure)
    return document


def _read(path: Path, version: tuple[object, ...]) -> tuple[tuple[object, ...], dict | None, str | None]:
    """(version, document, None) for a valid file, else (version, None, why).

    The version comes from the open handle, so it always belongs to the bytes that were
    read, even when the file is replaced between the caller's stat and this open.
    """
    try:
        with open(path, "rb") as handle:
            stat = os.fstat(handle.fileno())
            raw = handle.read()
    except OSError as exc:
        return version, None, f"unreadable: {exc}"
    version = _file_version(stat)
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        return version, None, f"not valid JSON: {exc}"
    reason = validate_calibration(document)
    if reason is not None:
        return version, None, f"off schema {_SCHEMA}: {reason}"
    if document["tau_at_bound"]:
        return version, None, f"tau_at_bound: tau_db {document['tau_db']!r} is the bound of the fit's search"
    return version, document, None


def _file_version(stat: os.stat_result) -> tuple[object, ...]:
    return (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)


def _resolved(path: Path) -> str:
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):  # RuntimeError: a symlink loop
        return os.path.abspath(path)


# ── Validation ───────────────────────────────────────────────────────────────


def validate_calibration(document: object) -> str | None:
    """None when ``document`` is a schema-1 calibration document, else a one-line reason naming the field.

    Strict on every field the schema pins, silent on keys it does not. A number is an int
    or a float, never a bool, and finite.
    """
    if not isinstance(document, dict):
        return _bad("document", "an object", document)
    schema = document.get("schema", _MISSING)
    if not (_is_int(schema) and schema == _SCHEMA):
        return _bad("schema", str(_SCHEMA), schema)
    kind = document.get("kind", _MISSING)
    if not (isinstance(kind, str) and kind == _KIND):
        return _bad("kind", repr(_KIND), kind)
    fitted_on = document.get("fitted_on", _MISSING)
    if not isinstance(fitted_on, str):
        return _bad("fitted_on", "a string", fitted_on)
    window = document.get("window", _MISSING)
    if not (isinstance(window, list) and len(window) == 2 and all(isinstance(end, str) for end in window)):
        return _bad("window", "a list of 2 strings", window)
    if reason := _integer(document, "nodes", minimum=1) or _number(document, "intercept_db"):
        return reason
    terms = document.get("terms", _MISSING)
    if not isinstance(terms, list):
        return _bad("terms", "a list", terms)
    names: set[str] = set()
    for index, term in enumerate(terms):
        if reason := _term_reason(term, f"terms[{index}]", names):
            return reason
    size = 1 + sum(len(term["levels"]) if term["kind"] == "category" else 1 for term in terms)
    return (
        _covariance_reason(document.get("covariance_db2", _MISSING), size)
        or _number(document, "tau_db", minimum=0.0)
        or _boolean(document, "tau_at_bound")
        or _number(document, "effective_parameters")
        or _dropped_terms_reason(document.get("dropped_terms", _MISSING))
        or _number(document, "threshold_db")
        or _number(document, "processing_gain_db")
        or _class_offsets_reason(document.get("class_offsets_db", _MISSING))
    )


def _term_reason(term: object, where: str, names: set[str]) -> str | None:
    if not isinstance(term, dict):
        return _bad(where, "an object", term)
    name = term.get("name", _MISSING)
    if not (isinstance(name, str) and name):
        return _bad(f"{where}.name", "a non-empty string", name)
    if name in names:
        return f"{where}.name: {_describe(name)} names an earlier term too"
    names.add(name)
    kind = term.get("kind", _MISSING)
    if not (isinstance(kind, str) and kind in _TERM_KINDS):
        return _bad(f"{where}.kind", " or ".join(map(repr, _TERM_KINDS)), kind)
    if reason := (
        _number(term, "prior_mean_db", where)
        or _number(term, "prior_sd_db", where, minimum=0.0)
        or _integer(term, "nodes", where, minimum=0)
    ):
        return reason
    if kind != "category":
        return (
            _number(term, "centre", where)
            or _number(term, "coefficient_db", where)
            or _number(term, "spread", where, minimum=0.0)
        )
    reference = term.get("reference", _MISSING)
    if not isinstance(reference, str):
        return _bad(f"{where}.reference", "a string", reference)
    levels = term.get("levels", _MISSING)
    if not (isinstance(levels, list) and levels):
        return _bad(f"{where}.levels", "a non-empty list", levels)
    seen: set[str] = set()
    for index, level in enumerate(levels):
        at = f"{where}.levels[{index}]"
        if not isinstance(level, dict):
            return _bad(at, "an object", level)
        value = level.get("value", _MISSING)
        if not isinstance(value, str):
            return _bad(f"{at}.value", "a string", value)
        if value == reference:
            return f"{at}.value: {_describe(value)} is the reference level"
        if value in seen:
            return f"{at}.value: {_describe(value)} names an earlier level too"
        seen.add(value)
        if reason := _number(level, "centre", at) or _number(level, "coefficient_db", at):
            return reason
    return None


def _covariance_reason(matrix: object, size: int) -> str | None:
    if not (isinstance(matrix, list) and len(matrix) == size):
        return _bad("covariance_db2", f"{size} rows (the intercept, then {size - 1} term columns)", matrix)
    for i, row in enumerate(matrix):
        if not (isinstance(row, list) and len(row) == size):
            return _bad(f"covariance_db2[{i}]", f"a row of {size} entries", row)
        for j, entry in enumerate(row):
            if not _is_number(entry):
                return _bad(f"covariance_db2[{i}][{j}]", "a finite number", entry)
    values = np.array(matrix, dtype=float)
    # Entries near the float limit overflow in the arithmetic below. That has to end in a
    # refusal (the comparisons are written so inf and nan fail them), never in a warning
    # or a raise.
    with np.errstate(all="ignore"):
        asymmetry = np.abs(values - values.T)
        i, j = np.unravel_index(int(np.argmax(asymmetry)), asymmetry.shape)
        if not asymmetry[i, j] <= _SYMMETRY_TOLERANCE * max(1.0, float(np.max(np.abs(values)))):
            return f"covariance_db2: not symmetric at [{i}][{j}] ({float(values[i, j])!r} vs {float(values[j, i])!r})"
        try:
            smallest = float(np.linalg.eigvalsh(values)[0])
        except np.linalg.LinAlgError:
            return "covariance_db2: its eigenvalues did not converge"
        floor = -size * _PSD_TOLERANCE * max(1.0, float(np.max(np.abs(values))))
    if not smallest >= floor:
        return f"covariance_db2: not positive semi-definite (smallest eigenvalue {smallest:.6g})"
    return None


def _dropped_terms_reason(dropped: object) -> str | None:
    if not isinstance(dropped, dict):
        return _bad("dropped_terms", "an object of term name to reason", dropped)
    for name, reason in dropped.items():
        if not isinstance(name, str):
            return _bad("dropped_terms", "string keys", name)
        if not isinstance(reason, str):
            return _bad(f"dropped_terms.{name}", "a string", reason)
    return None


def _class_offsets_reason(offsets: object) -> str | None:
    if not isinstance(offsets, dict):
        return _bad("class_offsets_db", "an object", offsets)
    for name in CLASS_NAMES:
        if reason := _number(offsets, name, "class_offsets_db"):
            return reason
    return None


def _number(obj: dict, key: str, where: str = "", minimum: float | None = None) -> str | None:
    value = obj.get(key, _MISSING)
    if _is_number(value) and (minimum is None or value >= minimum):
        return None
    expected = "a finite number" if minimum is None else f"a finite number >= {minimum:g}"
    return _bad(_field(where, key), expected, value)


def _integer(obj: dict, key: str, where: str = "", minimum: int = 0) -> str | None:
    value = obj.get(key, _MISSING)
    if _is_int(value) and value >= minimum:
        return None
    return _bad(_field(where, key), f"an integer >= {minimum}", value)


def _boolean(obj: dict, key: str) -> str | None:
    value = obj.get(key, _MISSING)
    return None if isinstance(value, bool) else _bad(key, "true or false", value)


def _field(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _bad(field: str, expected: str, value: object) -> str:
    if value is _MISSING:
        return f"{field}: missing, expected {expected}"
    return f"{field}: expected {expected}, got {_describe(value)}"


def _describe(value: object) -> str:
    """A short, single-line picture of a JSON value for a log line."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return _repr.repr(value)
    if isinstance(value, list):
        return f"a list of {len(value)}"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


# ── Evaluation ───────────────────────────────────────────────────────────────


def evaluate(document: Mapping, descriptors: Mapping[str, object]) -> tuple[float, float]:
    """The fleet's predicted system constant and its prediction sigma, (mean_db, sigma_db).

    ``document`` must have passed `validate_calibration`. The columns are the intercept's 1,
    then one per linear or log10 term and one per category level, in document order:

    * linear: value - centre, for an int or float that is finite (a bool is not a number);
    * log10: log10(value) - centre, for such a number above 0;
    * category level: (1 if the value is that level, else 0) - the level's centre. A
      category value is a string as given, a bool as "true" / "false", an int as its
      decimal string.

    Any other value, a missing key and None included, is absent, and so, for the mean, is a
    category value that is neither the reference nor a level. An absent term puts 0 in all
    of its columns, and adds to sigma^2 = tau^2 + x'Cx what the term varies by across the
    fleet, from its coefficients b and their block of C (two absent terms' cross terms are
    left out):

    * linear or log10: (b^2 + C_jj) * spread^2, spread being the term's SD over the fleet;
    * category: b'Pb + tr(P C_LL), P = diag(p) - pp' over the level centres p, which are
      the shares of the fleet holding each level; a value no fitted node held (neither
      the reference nor a level) adds the term's prior_sd_db^2 on top.
    """
    covariance = np.asarray(document["covariance_db2"], dtype=float)
    columns: list[float] = []
    coefficients: list[float] = []
    absent_variance = 0.0  # what the absent terms add to sigma^2
    for term in document["terms"]:
        value = descriptors.get(term["name"])
        first = 1 + len(columns)  # the term's first column in the covariance
        if term["kind"] == "category":
            levels = term["levels"]
            chosen = _category_value(value)
            known = chosen is not None and (chosen == term["reference"] or any(chosen == lv["value"] for lv in levels))
            for level in levels:
                columns.append(float(chosen == level["value"]) - level["centre"] if known else 0.0)
                coefficients.append(level["coefficient_db"])
            if not known:
                shares = np.array([level["centre"] for level in levels], dtype=float)
                beta = np.array([level["coefficient_db"] for level in levels], dtype=float)
                mix = np.diag(shares) - np.outer(shares, shares)
                block = covariance[first : first + len(levels), first : first + len(levels)]
                absent_variance += float(beta @ mix @ beta) + float(np.trace(mix @ block))
                if chosen is not None:
                    prior_sd = float(term["prior_sd_db"])
                    absent_variance += prior_sd * prior_sd
        else:
            encoded = _numeric_value(term["kind"], value)
            columns.append(0.0 if encoded is None else encoded - term["centre"])
            coefficients.append(term["coefficient_db"])
            if encoded is None:
                coefficient, spread = float(term["coefficient_db"]), float(term["spread"])
                absent_variance += (coefficient * coefficient + float(covariance[first, first])) * spread * spread
    mean = float(document["intercept_db"]) + sum(c * x for c, x in zip(coefficients, columns, strict=True))
    x = np.array([1.0, *columns])
    quadratic = float(x @ covariance @ x)
    tau = float(document["tau_db"])
    return float(mean), math.sqrt(tau * tau + max(quadratic + absent_variance, 0.0))


def _numeric_value(kind: str, value: object) -> float | None:
    if not _is_number(value):
        return None
    if kind == "linear":
        return float(value)
    return math.log10(value) if value > 0 else None


def _category_value(value: object) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        try:
            return str(value)
        except ValueError:  # beyond the interpreter's int-to-str digit limit
            return None
    return value if isinstance(value, str) else None
