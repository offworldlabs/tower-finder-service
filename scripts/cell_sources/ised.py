"""Canadian cell sites from the ISED Terrestrial Spectrum Licence Site Data Extract.

Input: <raw>/ised/Site_Data_Extract_FX.zip from
https://www.ic.gc.ca/engineering/SMS_TAFL_Files/Site_Data_Extract_FX.zip, one CSV member
(61 quoted columns, UTF-8 with a BOM, some header names ending in "*"). Spectrum licensees upload
one record per transmitter (one sector x one carrier) as a licence condition. Columns and units
follow ISED's "Terrestrial Spectrum Licence Site Data Extract - Field Descriptions" (October 2024)
and its site data upload format. The member is streamed, never extracted.

Personal data: the contact name, business telephone and email address columns are never read.
Only the columns in _COLUMNS are taken from a record. operator is the licensee name, a company.

One row per transmitter:
- radio from Radio Technology: GSM -> GSM, CDMA -> CDMA, HSPA -> UMTS, LTE -> LTE, 5GNR and
  5GDSS -> NR. Anything else (Other, Wi-Fi, WiMax, blank) is dropped.
- freq_mhz = TX frequency, the downlink centre in MHz. A record without one is dropped. No band
  filter is applied here.
- Coordinates outside a box around Canada (lat 40..84, lon -142..-50) are dropped.
- site_id = "<account number>:<lat 5 dp>,<lon 5 dp>", one operator's mast. cell_id = Cell
  Identification (CGI, ECGI or NCGI), else Physical Cell ID, else Record Identification.
- The extract keeps every upload of a transmitter, oldest first, and an older upload can differ
  (antenna count, power, height, and after a re-survey or renumbering the coordinates or cell ID).
  An upload batch is account number + upload date. A record is dropped as "superseded by a newer
  upload" when a newer batch of the same account has a record with the same Cell Identification
  and TX frequency, or the same Record Identification and TX frequency (Physical Cell IDs repeat
  across sites and are not used for this). Of the remaining records sharing (site_id, cell_id,
  freq_mhz) only the one with the latest upload date is kept, the later record on a tie; the others
  count as "duplicate transmitter". This takes two passes over the file.
- Records whose field count differs from the header's are dropped as "malformed row".
- bandwidth_mhz = Bandwidth (MHz). Blank or <= 0: 5 MHz, modelled. A row is one carrier, and
  licensees file the licensed block as the bandwidth (9 to 10 MHz for most GSM records) but the
  power of one carrier, so a bandwidth wider than one carrier of its radio (CARRIER_MAX_MHZ: NR 100,
  LTE 20, UMTS 5, CDMA 1.25, GSM 0.2) is cut to that width, modelled, and counted as "wider than one
  carrier, reduced". The EIRP bound and the default EIRP below are taken on this carrier bandwidth.
- height_m = TX antenna height (centre above ground, m). Blank or <= 0: 30 m, modelled.
- tilt_deg = the size of the TX elevation angle, the downtilt, positive below the horizon: ISED
  documents the angle as negative below the horizontal, but licensees file it with either sign
  (whole upload batches are all positive) and base station antennas tilt down, so the sign is
  ignored. Above 45 degrees it is implausible: NULL, counted as "tilt out of range".
- Omni rule. The omni indicator is D (directional) or N (non-directional), and the records do not
  always agree with it: about 23,000 cellular records marked D have a beamwidth of 300 degrees or
  more (omni antennas, often with OMNI in the model name), and about 11,000 marked N have a
  sector beamwidth and azimuth. An antenna is omni when its horizontal beamwidth is 300 degrees or
  more, or when it has no beamwidth and is marked N. azimuth_deg is NULL for an omni antenna, and
  otherwise the TX azimuth (NULL when blank or outside 0..360). beamwidth_deg is the TX
  horizontal beamwidth as given.
- TX power unit. The field is documented in dBm (TCP, or TRP for active antennas) and most
  licensees use dBm (40 to 52 is typical), but some upload batches are in watts: values such as
  103.09, 155.618 or 6.325, which as dBm would mean megawatts and as watts match the usual dBm
  settings (103.09 W = 50.1 dBm). A batch (account number + upload date) uses one unit, so a
  batch where at least 1% of the positive values exceed 60 (1 kW if read as dBm, beyond any
  cellular transmitter) is read as watts, and every other batch as dBm. In the November 2026 file
  the dBm batches have at most 0.03% of values above 60 and the watts batches 5% or more. In a dBm
  batch a value above 60 is discarded. Values <= 0 (placeholders such as -0.1 and 0) are discarded.
- eirp_dbm = TX power (dBm) + TX antenna gain (dBi) - TX line loss (dB, 0 when blank). A gain
  above 30 dBi is implausible for a base station antenna: counted as "antenna gain implausible,
  ignored" and taken as missing. A result outside 0 to 65.16 + 10*log10(carrier bandwidth) dBm
  (above the most permissive 47 CFR 27.50 base-station density; for a carrier of 1 MHz or less the
  limit is 65.16 dBm in total) is counted as "eirp implausible, ignored".
  Without a usable power, gain or EIRP: 61 dBm for a carrier of 5 MHz or less, plus
  10*log10(carrier bandwidth / 5) above, modelled.
- confidence = 0.9 for every row. updated = Date of Last Modification (yyyy-mm-dd), else None.
"""

from __future__ import annotations

import collections
import csv
import functools
import io
import math
import re
import zipfile
from collections.abc import Iterator
from datetime import date
from operator import itemgetter
from pathlib import Path

from cell_sources import CARRIER_MAX_MHZ

SOURCE = "ised"
LICENCE = "OGL-Canada-2.0"
ATTRIBUTION = (
    "Contains information licensed under the Open Government Licence - Canada (ISED Spectrum Management "
    "System, Site Data Extract; the licence of this extract is not confirmed by ISED)"
)
CONFIDENCE = 0.9

RADIO = {"GSM": "GSM", "CDMA": "CDMA", "HSPA": "UMTS", "LTE": "LTE", "5GNR": "NR", "5GDSS": "NR"}
DEFAULT_HEIGHT_M = 30.0
DEFAULT_BANDWIDTH_MHZ = 5.0
DEFAULT_EIRP_DBM = 61.0
OMNI_BEAMWIDTH_DEG = 300.0
TILT_MAX_DEG = 45.0
DBM_MAX = 60.0
GAIN_MAX_DBI = 30.0
# 47 CFR 27.50: 3280 W/MHz EIRP (65.16 dBm/MHz), the rural base-station limit, is the most permissive
# density it allows, so a filing above it is an error.
EIRP_MIN_DBM = 0.0
EIRP_DENSITY_MAX_DBM_PER_MHZ = 65.16
WATT_BATCH_SHARE = 0.01
LAT_RANGE = (40.0, 84.0)
LON_RANGE = (-142.0, -50.0)

# The only columns read. contact_name, business_telephone and email_address are never among them.
_COLUMNS = (
    "upload_date",
    "account_number",
    "licensee_name",
    "technology",
    "cell_id",
    "physical_id",
    "latitude",
    "longitude",
    "date_last_changed",
    "record_id",
    "tx_frequency",
    "bandwidth",
    "tx_power",
    "tx_ant_height",
    "tx_ant_omni_indicator",
    "tx_ant_horiz_beamwidth",
    "tx_ant_azimuth",
    "tx_ant_elevation_angle",
    "tx_ant_gain",
    "tx_line_loss",
)
(
    _UPLOAD,
    _ACCOUNT,
    _LICENSEE,
    _TECH,
    _CELL,
    _PCI,
    _LAT,
    _LON,
    _CHANGED,
    _RECORD,
    _FREQ,
    _BW,
    _POWER,
    _HEIGHT,
    _OMNI,
    _BEAM,
    _AZ,
    _ELEV,
    _GAIN,
    _LOSS,
) = range(len(_COLUMNS))
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def input_paths(raw_dir: Path) -> list[Path]:
    return [Path(raw_dir) / "ised" / "Site_Data_Extract_FX.zip"]


def _member(zf: zipfile.ZipFile) -> zipfile.ZipInfo:
    members = [i for i in zf.infolist() if i.filename.lower().endswith(".csv")]
    if not members:
        raise ValueError(f"{zf.filename}: no CSV member")
    return members[0]


def source_date(raw_dir: Path) -> str:
    with zipfile.ZipFile(input_paths(raw_dir)[0]) as zf:
        y, m, d = _member(zf).date_time[:3]
    return f"{y:04d}-{m:02d}-{d:02d}"


def _num(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _records(path: Path, stats: collections.Counter) -> Iterator[tuple[int, tuple[str, ...]]]:
    """(record number, the _COLUMNS values stripped) for each well-formed record."""
    with zipfile.ZipFile(path) as zf, zf.open(_member(zf)) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
        header = [name.strip().rstrip("*").strip().lower() for name in next(reader, [])]
        missing = [c for c in _COLUMNS if c not in header]
        if missing:
            raise ValueError(f"{path}: missing columns {', '.join(missing)}")
        pick = itemgetter(*(header.index(c) for c in _COLUMNS))
        width = len(header)
        for number, record in enumerate(reader):
            if len(record) != width:
                stats["malformed row"] += 1
                continue
            yield number, tuple(v.strip() for v in pick(record))


def _transmitter(rec: tuple[str, ...], stats: collections.Counter):
    """(radio, lat, lon, freq, site_id, cell_id) or None when the record is dropped."""
    radio = RADIO.get(rec[_TECH].upper().replace("-", "").replace(" ", ""))
    if radio is None:
        stats["non-cellular technology"] += 1
        return None
    freq = _num(rec[_FREQ])
    if freq is None or freq <= 0:
        stats["no tx frequency"] += 1
        return None
    lat, lon = _num(rec[_LAT]), _num(rec[_LON])
    if lat is None or lon is None or not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
        stats["bad coordinates"] += 1
        return None
    site_id = f"{rec[_ACCOUNT]}:{lat:.5f},{lon:.5f}"
    cell_id = rec[_CELL] or rec[_PCI] or rec[_RECORD] or None
    return radio, lat, lon, freq, site_id, cell_id


@functools.cache
def _upload_day(upload: str) -> int:
    """yyyymmdd of an upload date, 0 when malformed; one shared int per date."""
    return int(upload.replace("-", "")) if _ISO_DATE.fullmatch(upload) else 0


def _upload_key(rec: tuple[str, ...], number: int) -> int:
    return _upload_day(rec[_UPLOAD]) * 100_000_000 + number


def _successor_keys(rec: tuple[str, ...], freq: float) -> list[int]:
    """Keys under which a newer upload of the same account replaces this record."""
    keys = []
    if rec[_CELL]:
        keys.append(hash(("cell", rec[_ACCOUNT], rec[_CELL], freq)))
    if rec[_RECORD]:
        keys.append(hash(("record", rec[_ACCOUNT], rec[_RECORD], freq)))
    return keys


def _first_pass(path: Path):
    """Latest upload per transmitter and per successor key, and the upload batches whose TX power is in watts."""
    latest: dict[int, int] = {}
    newest: dict[int, int] = {}
    batches: dict[tuple[str, str], list[int]] = collections.defaultdict(lambda: [0, 0])
    scratch: collections.Counter = collections.Counter()
    for number, rec in _records(path, scratch):
        tx = _transmitter(rec, scratch)
        if tx is None:
            continue
        key = hash((tx[4], tx[5] or "", tx[3]))
        order = _upload_key(rec, number)
        if latest.get(key, -1) < order:
            latest[key] = order
        day = _upload_day(rec[_UPLOAD])
        for successor in _successor_keys(rec, tx[3]):
            if newest.get(successor, -1) < day:
                newest[successor] = day
        power = _num(rec[_POWER])
        if power is not None and power > 0:
            batch = batches[(rec[_ACCOUNT], rec[_UPLOAD])]
            batch[0] += 1
            batch[1] += power > DBM_MAX
    watts = {k for k, (positive, high) in batches.items() if high >= WATT_BATCH_SHARE * positive}
    return latest, newest, watts


def _tx_power_dbm(rec: tuple[str, ...], watts: set, stats: collections.Counter) -> float | None:
    power = _num(rec[_POWER])
    if power is None or power <= 0:
        stats["no tx power"] += 1
        return None
    if (rec[_ACCOUNT], rec[_UPLOAD]) in watts:
        stats["tx power in watts"] += 1
        return 10 * math.log10(power) + 30
    if power > DBM_MAX:
        stats["implausible tx power"] += 1
        return None
    return power


def _row(rec, tx, watts, stats) -> dict:
    radio, lat, lon, freq, site_id, cell_id = tx
    modelled = []
    bandwidth = _num(rec[_BW])
    if bandwidth is None or bandwidth <= 0:
        stats["no bandwidth"] += 1
        bandwidth = DEFAULT_BANDWIDTH_MHZ
        modelled.append("bandwidth")
    if bandwidth > CARRIER_MAX_MHZ[radio]:
        stats["wider than one carrier, reduced"] += 1
        bandwidth = CARRIER_MAX_MHZ[radio]
        if "bandwidth" not in modelled:
            modelled.append("bandwidth")
    power = _tx_power_dbm(rec, watts, stats)
    gain = _num(rec[_GAIN])
    if gain is not None and gain > GAIN_MAX_DBI:
        stats["antenna gain implausible, ignored"] += 1
        gain = None
    elif power is not None and gain is None:
        stats["no antenna gain"] += 1
    eirp = None
    if power is not None and gain is not None:
        eirp = power + gain - (_num(rec[_LOSS]) or 0.0)
        if not EIRP_MIN_DBM <= eirp <= EIRP_DENSITY_MAX_DBM_PER_MHZ + 10 * math.log10(max(bandwidth, 1.0)):
            stats["eirp implausible, ignored"] += 1
            eirp = None
    if eirp is None:
        eirp = DEFAULT_EIRP_DBM + max(0.0, 10 * math.log10(bandwidth / DEFAULT_BANDWIDTH_MHZ))
        modelled.append("eirp")
    height = _num(rec[_HEIGHT])
    if height is None or height <= 0:
        stats["no antenna height"] += 1
        height = DEFAULT_HEIGHT_M
        modelled.append("height")
    beamwidth = _num(rec[_BEAM])
    azimuth = None
    if (beamwidth is not None and beamwidth >= OMNI_BEAMWIDTH_DEG) or (beamwidth is None and rec[_OMNI] == "N"):
        stats["omni antenna"] += 1
    else:
        azimuth = _num(rec[_AZ])
        if azimuth is None or not 0 <= azimuth <= 360:
            stats["no azimuth"] += 1
            azimuth = None
        else:
            azimuth %= 360
    elevation = _num(rec[_ELEV])
    tilt = None
    if elevation is None:
        stats["no tilt"] += 1
    elif abs(elevation) > TILT_MAX_DEG:
        stats["tilt out of range"] += 1
    else:
        tilt = abs(elevation)
    changed = rec[_CHANGED]
    try:
        updated = date.fromisoformat(changed).isoformat() if _ISO_DATE.fullmatch(changed) else None
    except ValueError:
        updated = None
    if updated is None:
        stats["no change date"] += 1
    return {
        "country": "ca",
        "source": SOURCE,
        "licence": LICENCE,
        "site_id": site_id,
        "cell_id": cell_id,
        "operator": rec[_LICENSEE] or None,
        "radio": radio,
        "lat": lat,
        "lon": lon,
        "freq_mhz": freq,
        "bandwidth_mhz": bandwidth,
        "eirp_dbm": round(eirp, 2),
        "height_m": height,
        "azimuth_deg": azimuth,
        "beamwidth_deg": beamwidth,
        "tilt_deg": tilt,
        "modelled": ",".join(modelled),
        "confidence": CONFIDENCE,
        "updated": updated,
    }


def iter_rows(raw_dir: Path, stats: collections.Counter) -> Iterator[dict]:
    path = input_paths(raw_dir)[0]
    latest, newest, watts = _first_pass(path)
    for number, rec in _records(path, stats):
        tx = _transmitter(rec, stats)
        if tx is None:
            continue
        day = _upload_day(rec[_UPLOAD])
        if any(newest[key] > day for key in _successor_keys(rec, tx[3])):
            stats["superseded by a newer upload"] += 1
            continue
        if latest[hash((tx[4], tx[5] or "", tx[3]))] != _upload_key(rec, number):
            stats["duplicate transmitter"] += 1
            continue
        yield _row(rec, tx, watts, stats)
