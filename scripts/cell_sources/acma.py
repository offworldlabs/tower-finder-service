"""Australian cell sites from the ACMA Register of Radiocommunications Licences (RRL).

Input: acma/spectra_rrl.zip, the full RRL extract ACMA publishes for download
(one CSV per table; column definitions in DOC/cr_tables_oracle.sql inside the
zip). Members are streamed from the zip, never extracted, and only the columns
named below are read.

Joins, one output row per registered transmitter:

  device_details.csv   one row per device (one frequency, bandwidth and antenna)
    LICENCE_NO  -> licence.csv   SV_ID, SS_ID, STATUS, CLIENT_NO, DATE_OF_EFFECT
      CLIENT_NO -> client.csv    CLIENT_TYPE_ID, LICENCEE (no other client column)
    SITE_ID     -> site.csv      LATITUDE, LONGITUDE
    ANTENNA_ID  -> antenna.csv   GAIN, H_BEAMWIDTH

Carrier filter. The register has no "carrier" flag; the licence's service and
subservice say what the spectrum is for. A device is kept when:

  - its licence is a PTS apparatus licence (licence_service SV_ID 10 "PTS" or
    110 "PTS 900 MHz"; subservices PMTS Class B and C, whose devices are
    registered almost all as class of station FB, base station), or a spectrum
    licence (SV_ID 85) in a public mobile band, licence_subservice SS_ID 850
    1800 MHz, 854 2 GHz, 857 2.3 GHz, 861 3.4 GHz, 866 26 GHz, 868 800 MHz,
    870 700 MHz, 871 2.5 GHz, 872 2.5 GHz Mid Band Gap or 873 850/900 MHz,
    whose devices are the "Device Registration (Spectrum Licences)" records;
  - DEVICE_TYPE is "T" (each base station is registered as a T and an R record);
  - the licence STATUS is current: 1 Granted or 3 Granted (Under modification).

Spectrum licences outside those bands (area-wide licences "AWL - Standard" and
"AWL - FSS Only", the defence and 31 GHz bands) are dropped: they are local
private networks, earth stations and defence assignments. In the register this
was checked on, the filter keeps about 865,000 transmitters, about 99% of them
held by licensed carriers (Telstra, Optus, TPG/Vodafone and their joint venture,
NBN Co fixed wireless). The rest are private networks of rail and mining
operators and a few smaller network operators in the same bands, real base
stations of the same kind. No licensee name list is applied: licensee entities
change, and the register's industry category is blank or "Other" for several
carriers.

Units: FREQUENCY and BANDWIDTH are in Hz (converted to MHz). EIRP is in the
unit given by EIRP_UNIT (W in every record; mW, kW, dBW and dBm are also
accepted) and converted to dBm. It is used only when that gives 0 to 90 dBm:
a few records hold megawatts or a fraction of a milliwatt, and are counted as
"register eirp implausible, ignored". When EIRP is missing, not positive or
ignored it is derived from TRANSMITTER_POWER (with its unit) + antenna GAIN -
FEEDER_LOSS, the same formula the register's own EIRP follows (median
difference 0 dB on records that have all four), with a feeder loss that is
missing or outside 0 to 20 dB taken as 0 dB; a derived value outside 0 to 90
dBm falls to the default below. GAIN is in dBi. HEIGHT is the antenna height
in metres, AZIMUTH degrees (360 is stored as 0). tilt_deg is the size of TILT,
the downtilt, positive below the horizon: licensees register it with either
sign and base station antennas tilt down, so the sign is ignored; above 45
degrees it is NULL, counted as "tilt out of range". BANDWIDTH is the
"licensing bandwidth"; for 98.5% of these transmitters it is within 0.5 MHz
of the bandwidth in the emission designator. H_BEAMWIDTH of the antenna is the
horizontal 3 dB beamwidth in degrees. The azimuth is NULL (omnidirectional)
for an antenna of 360 degrees and for a device with ANTENNA_MULTI_MODE "Y",
which the register defines as identical directional antennas forming an
omnidirectional pattern. An azimuth of 0 (or 360) on an antenna with no
beamwidth (H_BEAMWIDTH 0, blank or no antenna record) is a placeholder and is
NULL too, counted as "azimuth nulled: placeholder 0", unless the same licence
registers that antenna at the same site at another azimuth (found in a first
pass over device_details.csv), which makes it the north sector of a sectored
array. In the register this was checked on, 8,183 transmitters read 0 on such
an antenna, nearly all colinear, omni, dipole, leaky feeder, DAS or unknown
types, and 113 of them sit in such a sector layout.

Defaults, flagged in `modelled`: bandwidth 5 MHz when missing; height 30 m when
missing or not positive; EIRP 61 dBm for a bandwidth up to 5 MHz plus
10*log10(bandwidth/5) above it when no plausible EIRP is registered or derived.
The frequency is never defaulted: a device without one is dropped.

radio is always NULL: the register has no technology field, and the emission
designator (for example 10M0W7D) does not tell LTE, NR and UMTS apart.

site_id is the ACMA SITE_ID. cell_id is the DEVICE_REGISTRATION_IDENTIFIER of a
spectrum licence device, or "EFL" + EFL_ID for an apparatus licence device: the
register documents these as the primary keys of the two kinds of record (the
first is not populated for apparatus licences), and the prefix keeps the two
numbering spaces apart. SDD_ID is not used: it increases strictly with row
order in the extract, which points to a per-extract row number. updated is the
device AUTHORISATION_DATE, else the licence DATE_OF_EFFECT.

Natural persons: client_type.csv type 7 "Person" is a natural person. Section 8
of the RRL licence forbids reproducing a natural-person licensee's client
information in derivatives, so such a transmitter is kept with operator NULL
and the name is never held in memory. Types 1 to 6 are organisations
(departments, agencies, governments, companies, community groups); their
LICENCEE name is the operator. No address, ABN or other client column is read.

Every row has confidence 0.85. Data licence: ACMA RRL licence terms;
attribution "Based on Australian Communications and Media Authority information".
"""

from __future__ import annotations

import csv
import io
import math
import struct
import zipfile
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

SOURCE = "acma"
LICENCE = "ACMA-RRL"
ATTRIBUTION = "Based on Australian Communications and Media Authority information"

COUNTRY = "au"
CONFIDENCE = 0.85
DEVICE_MEMBER = "device_details.csv"

PTS_SERVICES = frozenset({"10", "110"})
SPECTRUM_SERVICE = "85"
MOBILE_SPECTRUM_BANDS = frozenset({"850", "854", "857", "861", "866", "868", "870", "871", "872", "873"})
CURRENT_STATUSES = frozenset({"1", "3"})
NATURAL_PERSON = "7"
ORGANISATION_TYPES = frozenset({"1", "2", "3", "4", "5", "6"})

DEFAULT_BANDWIDTH_MHZ = 5.0
DEFAULT_HEIGHT_M = 30.0
DEFAULT_EIRP_DBM = 61.0
EIRP_RANGE_DBM = (0.0, 90.0)
FEEDER_LOSS_RANGE_DB = (0.0, 20.0)
TILT_MAX_DEG = 45.0

_LINEAR_DBM_OFFSET = {"mW": 0.0, "W": 30.0, "kW": 60.0}
_LOG_DBM_OFFSET = {"dBm": 0.0, "dBW": 30.0}
_UNIX_TIME_EXTRA = 0x5455

DEVICE_COLUMNS = (
    "LICENCE_NO",
    "DEVICE_REGISTRATION_IDENTIFIER",
    "EFL_ID",
    "AUTHORISATION_DATE",
    "FREQUENCY",
    "BANDWIDTH",
    "DEVICE_TYPE",
    "TRANSMITTER_POWER",
    "TRANSMITTER_POWER_UNIT",
    "SITE_ID",
    "ANTENNA_ID",
    "AZIMUTH",
    "HEIGHT",
    "TILT",
    "FEEDER_LOSS",
    "EIRP",
    "EIRP_UNIT",
    "ANTENNA_MULTI_MODE",
)


def input_paths(raw_dir: Path) -> list[Path]:
    return [Path(raw_dir) / "acma" / "spectra_rrl.zip"]


def source_date(raw_dir: Path) -> str:
    """Date of the device_details.csv member, in UTC when the zip records it."""
    with zipfile.ZipFile(input_paths(raw_dir)[0]) as zf:
        info = zf.getinfo(DEVICE_MEMBER)
    mtime = _unix_mtime(info.extra)
    if mtime is not None:
        return datetime.fromtimestamp(mtime, UTC).date().isoformat()
    return date(*info.date_time[:3]).isoformat()


def iter_rows(raw_dir: Path, stats: Counter) -> Iterator[dict]:
    with zipfile.ZipFile(input_paths(raw_dir)[0]) as zf:
        licences = {
            no: (sv, ss, status, client, effect)
            for no, sv, ss, status, client, effect in _read(
                zf, "licence.csv", ("LICENCE_NO", "SV_ID", "SS_ID", "STATUS", "CLIENT_NO", "DATE_OF_EFFECT")
            )
        }
        clients = {
            no: (kind, name if kind in ORGANISATION_TYPES else None)
            for no, kind, name in _read(zf, "client.csv", ("CLIENT_NO", "CLIENT_TYPE_ID", "LICENCEE"))
        }
        sites = _sites(zf)
        antennas = {
            ant: (_number(gain), _beamwidth(beam))
            for ant, gain, beam in _read(zf, "antenna.csv", ("ANTENNA_ID", "GAIN", "H_BEAMWIDTH"))
        }
        pointed = _pointed_without_beamwidth(zf, antennas)
        for device in _read(zf, DEVICE_MEMBER, DEVICE_COLUMNS):
            row = _row(device, licences, clients, sites, antennas, pointed, stats)
            if row is not None:
                yield row


def _pointed_without_beamwidth(zf: zipfile.ZipFile, antennas: dict) -> set[tuple[str, str, str]]:
    """(licence, site, antenna) of transmitters on an antenna with no beamwidth whose azimuth is not 0."""
    pointed = set()
    columns = ("LICENCE_NO", "SITE_ID", "ANTENNA_ID", "AZIMUTH", "DEVICE_TYPE", "ANTENNA_MULTI_MODE")
    for licence_no, site_id, antenna_id, azimuth, device_type, multi_mode in _read(zf, DEVICE_MEMBER, columns):
        if device_type == "T" and multi_mode != "Y" and antennas.get(antenna_id, (None, None))[1] is None:
            azimuth_deg = _number(azimuth)
            if azimuth_deg is not None and 0 < azimuth_deg < 360:
                pointed.add((licence_no, site_id, antenna_id))
    return pointed


def _row(device, licences, clients, sites, antennas, pointed, stats) -> dict | None:
    (
        licence_no,
        registration_id,
        efl_id,
        authorised,
        freq,
        bandwidth,
        device_type,
        power,
        power_unit,
        site_id,
        antenna_id,
        azimuth,
        height,
        tilt,
        feeder_loss,
        eirp,
        eirp_unit,
        multi_mode,
    ) = device
    licence = licences.get(licence_no)
    if licence is None:
        stats["licence not found"] += 1
        return None
    service, subservice, status, client_no, effective = licence
    if service == SPECTRUM_SERVICE and subservice not in MOBILE_SPECTRUM_BANDS:
        stats["spectrum licence outside mobile bands"] += 1
        return None
    if service != SPECTRUM_SERVICE and service not in PTS_SERVICES:
        stats["not a carrier base station"] += 1
        return None
    if device_type != "T":
        stats["receiver" if device_type == "R" else "not a transmitter"] += 1
        return None
    if status not in CURRENT_STATUSES:
        stats["licence not current"] += 1
        return None
    position = sites.get(site_id)
    if position is None:
        stats["site without position"] += 1
        return None
    freq_hz = _number(freq)
    if freq_hz is None or freq_hz <= 0:
        stats["no frequency"] += 1
        return None

    modelled = []
    bandwidth_hz = _number(bandwidth)
    if bandwidth_hz is not None and bandwidth_hz > 0:
        bandwidth_mhz = round(bandwidth_hz / 1e6, 6)
    else:
        bandwidth_mhz = DEFAULT_BANDWIDTH_MHZ
        modelled.append("bandwidth")
        stats["bandwidth defaulted"] += 1

    gain, beamwidth = antennas.get(antenna_id, (None, None))
    eirp_dbm = _dbm(_number(eirp), eirp_unit)
    if eirp_dbm is not None and not EIRP_RANGE_DBM[0] <= eirp_dbm <= EIRP_RANGE_DBM[1]:
        eirp_dbm = None
        stats["register eirp implausible, ignored"] += 1
    if eirp_dbm is None:
        power_dbm = _dbm(_number(power), power_unit)
        if power_dbm is not None and gain is not None:
            loss_db = _number(feeder_loss)
            if loss_db is not None and not FEEDER_LOSS_RANGE_DB[0] <= loss_db <= FEEDER_LOSS_RANGE_DB[1]:
                loss_db = None
                stats["feeder loss out of range ignored"] += 1
            derived = power_dbm + gain - (loss_db or 0.0)
            if EIRP_RANGE_DBM[0] <= derived <= EIRP_RANGE_DBM[1]:
                eirp_dbm = derived
                stats["eirp derived from power, gain and feeder loss"] += 1
        if eirp_dbm is None:
            eirp_dbm = DEFAULT_EIRP_DBM + 10 * math.log10(max(bandwidth_mhz, DEFAULT_BANDWIDTH_MHZ) / 5)
            modelled.append("eirp")
            stats["eirp defaulted"] += 1

    height_m = _number(height)
    if height_m is None or height_m <= 0:
        height_m = DEFAULT_HEIGHT_M
        modelled.append("height")
        stats["height defaulted"] += 1

    if beamwidth is None:
        stats["beamwidth missing"] += 1
    azimuth_deg = _number(azimuth)
    if multi_mode == "Y" or (beamwidth is not None and beamwidth >= 360):
        azimuth_deg = None
        stats["azimuth nulled: omni antenna"] += 1
    elif azimuth_deg is None or not 0 <= azimuth_deg <= 360:
        azimuth_deg = None
        stats["azimuth missing"] += 1
    elif azimuth_deg % 360 == 0 and beamwidth is None and (licence_no, site_id, antenna_id) not in pointed:
        azimuth_deg = None
        stats["azimuth nulled: placeholder 0"] += 1
    else:
        azimuth_deg %= 360

    tilt_deg = _number(tilt)
    if tilt_deg is None:
        stats["tilt missing"] += 1
    elif abs(tilt_deg) > TILT_MAX_DEG:
        tilt_deg = None
        stats["tilt out of range"] += 1
    else:
        tilt_deg = abs(tilt_deg)

    kind, name = clients.get(client_no, (None, None))
    operator = name or None
    if kind == NATURAL_PERSON:
        stats["operator nulled: natural person"] += 1
    elif operator is None:
        stats["operator nulled: unknown licensee"] += 1

    cell_id = registration_id or (f"EFL{efl_id}" if efl_id else None)
    if cell_id is None:
        stats["cell id missing"] += 1
    updated = _iso_date(authorised) or _iso_date(effective)
    if updated is None:
        stats["updated missing"] += 1

    return {
        "country": COUNTRY,
        "source": SOURCE,
        "licence": LICENCE,
        "site_id": site_id,
        "cell_id": cell_id,
        "operator": operator,
        "radio": None,
        "lat": position[0],
        "lon": position[1],
        "freq_mhz": round(freq_hz / 1e6, 6),
        "bandwidth_mhz": bandwidth_mhz,
        "eirp_dbm": round(eirp_dbm, 2),
        "height_m": height_m,
        "azimuth_deg": azimuth_deg,
        "beamwidth_deg": beamwidth,
        "tilt_deg": tilt_deg,
        "modelled": ",".join(modelled),
        "confidence": CONFIDENCE,
        "updated": updated,
    }


def _read(zf: zipfile.ZipFile, member: str, columns: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    """Yield the named columns of each record of a CSV member, stripped."""
    with zf.open(member) as fh:
        reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8-sig", newline=""))
        header = [h.strip().upper() for h in next(reader, [])]
        missing = [c for c in columns if c not in header]
        if missing:
            raise ValueError(f"{member}: missing columns {', '.join(missing)}")
        index = [header.index(c) for c in columns]
        for record in reader:
            if record:
                yield tuple(record[i].strip() if i < len(record) else "" for i in index)


def _sites(zf: zipfile.ZipFile) -> dict[str, tuple[float, float]]:
    sites = {}
    for site_id, lat, lon in _read(zf, "site.csv", ("SITE_ID", "LATITUDE", "LONGITUDE")):
        lat_f, lon_f = _number(lat), _number(lon)
        if site_id and lat_f is not None and lon_f is not None and (lat_f, lon_f) != (0.0, 0.0):
            if -90 <= lat_f <= 90 and -180 <= lon_f <= 180:
                sites[site_id] = (lat_f, lon_f)
    return sites


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _beamwidth(text: str) -> float | None:
    """H_BEAMWIDTH in degrees, or None when it is missing or outside 0 to 360."""
    value = _number(text)
    return value if value is not None and 0 < value <= 360 else None


def _dbm(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    if unit in _LOG_DBM_OFFSET:
        return value + _LOG_DBM_OFFSET[unit]
    if unit in _LINEAR_DBM_OFFSET and value > 0:
        return 10 * math.log10(value) + _LINEAR_DBM_OFFSET[unit]
    return None


def _iso_date(text: str) -> str | None:
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _unix_mtime(extra: bytes) -> int | None:
    """Modification time from the zip's extended timestamp field, if present."""
    pos = 0
    while pos + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, pos)
        body = extra[pos + 4 : pos + 4 + size]
        if tag == _UNIX_TIME_EXTRA and len(body) >= 5 and body[0] & 1:
            return struct.unpack_from("<i", body, 1)[0]
        pos += 4 + size
    return None
