"""US cell sites from the FCC ULS Cellular (CL, Part 22, 850 MHz) licence records.

Input: <raw>/fcc/l_cell.zip, the weekly complete licence file of the FCC Universal Licensing
System (https://data.fcc.gov/download/pub/uls/complete/l_cell.zip). Its members are
pipe-delimited .dat tables, one record per line (UTF-8), streamed from the zip, never
extracted. Column positions follow the ULS public access SQL definitions
(public_access_database_definitions_sql_20250417.txt); codes follow uls_code_definitions.

Tables and joins, all on unique_system_identifier (USI):
- HD (licence header): call sign, licence status, grant and last action dates.
- EN (entity): only the record with entity type L (licensee) is used, and only its entity
  name, applicant type code and whether its first/last name fields are set. Contacts, phones,
  emails and addresses are never read. operator is the licensee's entity name, or None when
  the licensee is an individual (applicant type I, or a personal name in the first/last name
  fields), counted as "operator nulled: individual".
- LO (location) by (USI, location number): position and structure heights.
- AN (antenna) by (USI, location number, antenna number): height to tip, azimuth, beamwidth,
  tilt and maximum ERP.
- FR (frequency), streamed: one record per antenna and frequency range; each kept record is
  one row (one site x one antenna x one carrier block).

Table family: the file also holds L3/A3/F3, which the definitions name Leased Locations,
Leased Antenna and Leased Frequency. They are keyed by a lease's own USI (its HD record
carries the lease id as call sign, LL links it to the licence) and repeat the licensor's
sites for each spectrum lease: every lease location of an active lease has the call sign
and coordinates of an active licence's LO record, so reading them would only multiply the
same transmitters. They are not read; LO/AN/FR describe the licensed sites.

Rules:
- Active licences only: HD licence status A. FR records of any other status (C cancelled,
  E expired, T terminated, ...) count as "inactive licence".
- Downlink only: an FR record is kept when frequency_assigned lies in 869-894 MHz (base
  transmit). 824-849 MHz is the paired mobile transmit range, counted as "uplink frequency";
  any other value as "frequency outside the cellular block"; a blank one as "no frequency".
  No band-group filter is applied here.
- freq_mhz is the centre of [frequency_assigned, frequency_upper_band] and bandwidth_mhz
  their difference; without an upper edge above the lower one, freq_mhz is
  frequency_assigned and the bandwidth is 5 MHz, modelled. A frequency is never invented.
- eirp_dbm = 10*log10(ERP_W * 1000) + 2.15, from the FR record's power_erp (watts), else the
  antenna's maximum_erp. A value <= 0 is ignored. Without either: 61 dBm for 5 MHz or less,
  plus 10*log10(bandwidth / 5) above, modelled. The antennas state the non-PSD method
  (psd_nonpsd_methodology), so the ERP is read as watts, not watts per MHz.
- lat/lon from LO degrees, minutes, seconds and hemisphere (S and W negative). Blank minutes
  or seconds count as 0 and mark the position modelled. A location without degrees or
  hemisphere, or out of range, counts as "no location".
- height_m, above ground: AN height to tip, else LO height of the support structure, else
  LO overall structure height, else 30 m, modelled. A height <= 0 or above 650 m (taller
  than any US structure) is ignored. AN height_to_center_raat is not used: it is rarely
  filed, and as a radiation centre above average terrain it is not a height above ground.
- azimuth_deg, beamwidth_deg and tilt_deg from AN as filed, NULL when blank or out of range.
  An unknown azimuth stays NULL and is not modelled.
- radio is NULL: CL records do not state the air interface.
- site_id = "<call sign>:<location number>", cell_id = "<antenna number>:<FR frequency
  sequence id>": an antenna has one FR record per downlink block (each CL block is split in
  two ranges), so the antenna number alone is not unique. A second FR record for the same
  antenna and frequency range counts as "duplicate frequency record".
- updated = HD last action date, else grant date (yyyy-mm-dd). confidence = 0.6 for every
  row: licensed coordinates and filed powers, but filings can be decades old.
"""

from __future__ import annotations

import collections
import io
import math
import re
import zipfile
from collections.abc import Iterator
from pathlib import Path

SOURCE = "fcc_uls"
LICENCE = "US-Gov-PD"
ATTRIBUTION = (
    "Source: FCC Universal Licensing System (ULS), Cellular service records; US Government work, public domain"
)
CONFIDENCE = 0.6

DEFAULT_HEIGHT_M = 30.0
DEFAULT_BANDWIDTH_MHZ = 5.0
DEFAULT_EIRP_DBM = 61.0
MAX_HEIGHT_M = 650.0
DOWNLINK_MHZ = (869.0, 894.0)
UPLINK_MHZ = (824.0, 849.0)
DIPOLE_GAIN_DBI = 2.15
_MODELLED_ORDER = ("freq", "bandwidth", "eirp", "height", "azimuth", "position")

# Column positions (1-based, as in the SQL definitions) of the fields read from each table.
_HD = {"usi": 2, "call_sign": 5, "status": 6, "grant_date": 8, "last_action_date": 44}
_EN = {"usi": 2, "entity_type": 6, "entity_name": 8, "first_name": 9, "last_name": 11, "applicant_type": 24}
_LO = {
    "usi": 2,
    "location_number": 9,
    "lat_deg": 20,
    "lat_min": 21,
    "lat_sec": 22,
    "lat_dir": 23,
    "lon_deg": 24,
    "lon_min": 25,
    "lon_sec": 26,
    "lon_dir": 27,
    "support_height": 39,
    "overall_height": 40,
}
_AN = {
    "usi": 2,
    "antenna_number": 7,
    "location_number": 8,
    "height_to_tip": 11,
    "tilt": 15,
    "beamwidth": 17,
    "azimuth": 19,
    "maximum_erp": 38,
}
_FR = {
    "usi": 2,
    "call_sign": 5,
    "location_number": 7,
    "antenna_number": 8,
    "frequency_assigned": 11,
    "frequency_upper_band": 12,
    "power_erp": 17,
    "freq_seq_id": 27,
}
_US_DATE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_INDIVIDUAL = object()
_CREATED = re.compile(r"File Creation Date:\s+\w{3}\s+(\w{3})\s+(\d{1,2})\s+[\d:]+\s+\S+\s+(\d{4})")


def input_paths(raw_dir: Path) -> list[Path]:
    return [Path(raw_dir) / "fcc" / "l_cell.zip"]


def _member(zf: zipfile.ZipFile, name: str) -> str:
    for info in zf.infolist():
        if info.filename.rsplit("/", 1)[-1].lower() == name.lower():
            return info.filename
    raise ValueError(f"{zf.filename}: no {name} member")


def source_date(raw_dir: Path) -> str | None:
    """The file creation date stated in the zip's counts member, or None."""
    with zipfile.ZipFile(input_paths(raw_dir)[0]) as zf:
        try:
            text = zf.read(_member(zf, "counts")).decode("utf-8", "replace")
        except ValueError:
            return None
    match = _CREATED.search(text)
    if not match or match.group(1) not in _MONTHS:
        return None
    month, day, year = _MONTHS.index(match.group(1)) + 1, int(match.group(2)), match.group(3)
    return f"{year}-{month:02d}-{day:02d}"


def _records(zf: zipfile.ZipFile, table: str, columns: dict, stats: collections.Counter) -> Iterator[dict]:
    width = max(columns.values())
    with zf.open(_member(zf, f"{table}.dat")) as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="\n"):
            fields = line.rstrip("\r\n").split("|")
            if len(fields) < width or fields[0] != table:
                if line.strip():
                    stats[f"malformed {table} record"] += 1
                continue
            yield {name: fields[pos - 1].strip() for name, pos in columns.items()}


def _num(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _iso(text: str) -> str | None:
    match = _US_DATE.fullmatch(text)
    return f"{match.group(3)}-{match.group(1)}-{match.group(2)}" if match else None


def _degrees(deg: str, minutes: str, seconds: str, hemisphere: str, letters: tuple, limit: float):
    """Decimal degrees and whether minutes/seconds were missing, or None when unusable.

    letters is (positive, negative) hemisphere, e.g. ("N", "S").
    """
    d, m, s = _num(deg), _num(minutes or "0"), _num(seconds or "0")
    if d is None or m is None or s is None or hemisphere not in letters:
        return None
    if not (0 <= d <= limit and 0 <= m < 60 and 0 <= s < 60):
        return None
    value = d + m / 60 + s / 3600
    if value > limit:
        return None
    return round(-value if hemisphere == letters[1] else value, 6), not (minutes and seconds)


def _position(lo: dict):
    lat = _degrees(lo["lat_deg"], lo["lat_min"], lo["lat_sec"], lo["lat_dir"], ("N", "S"), 90)
    lon = _degrees(lo["lon_deg"], lo["lon_min"], lo["lon_sec"], lo["lon_dir"], ("E", "W"), 180)
    if lat is None or lon is None:
        return None
    return lat[0], lon[0], lat[1] or lon[1]


def _licensee(en: dict):
    """The licensee's entity name; _INDIVIDUAL for a natural person, whose name is never kept."""
    if en["applicant_type"] == "I" or en["first_name"] or en["last_name"]:
        return _INDIVIDUAL
    return en["entity_name"] or None


def _height(antenna: dict | None, site: dict, stats: collections.Counter) -> float | None:
    candidates = [antenna["height_to_tip"]] if antenna else []
    for text in candidates + [site["support_height"], site["overall_height"]]:
        value = _num(text)
        if value is None:
            continue
        if 0 < value <= MAX_HEIGHT_M:
            return value
        stats["height out of range ignored"] += 1
    return None


def _angle(antenna: dict | None, field: str, low: float, high: float, stats: collections.Counter) -> float | None:
    value = _num(antenna[field]) if antenna else None
    if value is not None and not low <= value <= high:
        stats[f"{field} out of range ignored"] += 1
        return None
    return value


def _erp_w(text: str, stats: collections.Counter) -> float | None:
    value = _num(text)
    if value is not None and value <= 0:
        stats["ERP <= 0 ignored"] += 1
        return None
    return value


def iter_rows(raw_dir: Path, stats: collections.Counter) -> Iterator[dict]:
    with zipfile.ZipFile(input_paths(raw_dir)[0]) as zf:
        licences = {r["usi"]: r for r in _records(zf, "HD", _HD, stats)}
        active = {usi for usi, r in licences.items() if r["status"] == "A"}
        operators = {}
        for en in _records(zf, "EN", _EN, stats):
            if en["entity_type"] == "L" and en["usi"] in active:
                operators[en["usi"]] = _licensee(en)
        sites = {}
        for lo in _records(zf, "LO", _LO, stats):
            if lo["usi"] in active:
                sites[(lo["usi"], lo["location_number"])] = (lo, _position(lo))
        antennas = {}
        for an in _records(zf, "AN", _AN, stats):
            if an["usi"] in active:
                antennas[(an["usi"], an["location_number"], an["antenna_number"])] = an

        seen = set()
        for fr in _records(zf, "FR", _FR, stats):
            usi = fr["usi"]
            if usi not in licences:
                stats["no licence header"] += 1
                continue
            if usi not in active:
                stats["inactive licence"] += 1
                continue
            low = _num(fr["frequency_assigned"])
            if low is None:
                stats["no frequency"] += 1
                continue
            if not DOWNLINK_MHZ[0] <= low <= DOWNLINK_MHZ[1]:
                uplink = UPLINK_MHZ[0] <= low <= UPLINK_MHZ[1]
                stats["uplink frequency" if uplink else "frequency outside the cellular block"] += 1
                continue
            site, position = sites.get((usi, fr["location_number"]), (None, None))
            if position is None:
                stats["no location"] += 1
                continue
            high = _num(fr["frequency_upper_band"])
            key = (usi, fr["location_number"], fr["antenna_number"], low, high)
            if key in seen:
                stats["duplicate frequency record"] += 1
                continue
            seen.add(key)

            modelled = set()
            if high is not None and high > low:
                freq, bandwidth = round((low + high) / 2, 4), round(high - low, 4)
            else:
                if high is not None:
                    stats["upper band edge not above assigned ignored"] += 1
                freq, bandwidth = low, DEFAULT_BANDWIDTH_MHZ
                modelled.add("bandwidth")
            antenna = antennas.get((usi, fr["location_number"], fr["antenna_number"]))
            if antenna is None:
                stats["no antenna record"] += 1
            erp = _erp_w(fr["power_erp"], stats)
            if erp is None and antenna:
                erp = _erp_w(antenna["maximum_erp"], stats)
            if erp is not None:
                eirp = round(10 * math.log10(erp * 1000) + DIPOLE_GAIN_DBI, 2)
            else:
                eirp = round(DEFAULT_EIRP_DBM + 10 * math.log10(max(bandwidth, DEFAULT_BANDWIDTH_MHZ) / 5), 2)
                modelled.add("eirp")
            height = _height(antenna, site, stats)
            if height is None:
                height = DEFAULT_HEIGHT_M
                modelled.add("height")
            lat, lon, rough = position
            if rough:
                modelled.add("position")
            operator = operators.get(usi)
            if operator is _INDIVIDUAL:
                stats["operator nulled: individual"] += 1
                operator = None
            elif operator is None:
                stats["operator unknown: no licensee name"] += 1
            licence = licences[usi]
            antenna_number, seq = fr["antenna_number"], fr["freq_seq_id"]
            yield {
                "country": "us",
                "source": SOURCE,
                "licence": LICENCE,
                "site_id": f"{licence['call_sign'] or fr['call_sign']}:{fr['location_number']}",
                "cell_id": f"{antenna_number}:{seq}" if seq else antenna_number or None,
                "operator": operator,
                "radio": None,
                "lat": lat,
                "lon": lon,
                "freq_mhz": freq,
                "bandwidth_mhz": bandwidth,
                "eirp_dbm": eirp,
                "height_m": height,
                "azimuth_deg": _angle(antenna, "azimuth", 0, 360, stats),
                "beamwidth_deg": _angle(antenna, "beamwidth", 0, 360, stats),
                "tilt_deg": _angle(antenna, "tilt", -90, 90, stats),
                "modelled": ",".join(m for m in _MODELLED_ORDER if m in modelled),
                "confidence": CONFIDENCE,
                "updated": _iso(licence["last_action_date"]) or _iso(licence["grant_date"]),
            }
