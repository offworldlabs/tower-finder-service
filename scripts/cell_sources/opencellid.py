"""OpenCelliD cells in the United States, merged into one row per site and radio.

Source: OpenCelliD cell export files (https://opencellid.org), raw_dir/opencellid/*.csv and *.csv.gz, read only when
raw_dir/opencellid/READY exists (the builder checks input_paths). Columns, with or without the header line:

  radio, mcc, net, area, cell, unit, lon, lat, range, samples, changeable, created, updated, averageSignal

created and updated are unix seconds. OpenCelliD positions are estimates computed from contributed observations,
not mast coordinates, and the files hold no frequency, power, height or azimuth.

Rows kept: radio LTE or NR (any case) with mcc 310-316. Every other record is counted in `stats` by reason:
"not LTE/NR", "not a US MCC", "bad row" (too few columns or an unreadable radio, mcc, net, cell, lat or lon),
"bad position" (outside -90..90 / -180..180 or 0,0), "bad cell id" (outside the radio's cell identity range).

Clustering, keyed by (radio, mcc, mnc, node):
  LTE  the 28-bit E-UTRAN cell identity is eNB id (20 bits) + local cell id (8 bits): node = cell >> 8.
  NR   assumption: the 36-bit NR cell identity carries a 24-bit gNB id and a 12-bit local cell id, so
       node = cell >> 12. 3GPP lets an operator pick a gNB id length of 22-32 bits. A shorter real length splits
       one gNB into up to four sites; a longer one merges neighbouring gNBs into one site.

Site position: the sample-weighted median of latitude and of longitude, each taken separately, with weight
max(samples, 1). When the cumulative weight reaches exactly half, the two middle values are averaged. Outlier
rule: cells farther than 10 km from that first median are dropped (counted as "cells over 10 km from their site
median") and the median is recomputed from the rest; a site left with no cell is dropped (counted as "sites with
no cell within 10 km of their median"). `updated` is the newest updated date among the site's kept cells.

Snapping: the site goes to the nearest standing FCC ASR structure (see asr.py) within 250 m (haversine, grid
index), read from raw_dir/fcc/r_tower.zip.
  snapped        lat, lon = the structure; height_m = its height above ground; 'height' modelled; confidence 0.4
  not snapped    lat, lon = the median; height_m = 30; 'height' and 'position' modelled; confidence 0.25
A snapped site's height is always modelled: the structure height stands in for the antenna height. A structure
without a height, or under 3 m (counted as "ASR height under 3 m, defaulted"), gives height_m 30. Without
r_tower.zip nothing is snapped, and stats gets "no ASR file, positions not snapped" once.

Radio parameters: no band is known, so every site gets one default carrier per radio, flagged 'freq,bandwidth,eirp':
  LTE  2132.5 MHz, 10 MHz: the centre of AWS-1 / band 4 (2110-2155 MHz), inside band 66 (2110-2200 MHz);
       10 MHz matches the NTIA reference carriers (61 dBm up to 5 MHz, 64 dBm above 10 MHz).
  NR   3750 MHz, 100 MHz: inside the 3.7 GHz Service (3700-3980 MHz) of band n77 (3300-4200 MHz), the C-band
       mid-band layer of the US networks; the 100 MHz width is assumed (one full-width n77 carrier).
Both frequencies fall inside the builder's band groups (CELL2100, CELL3500).
EIRP per carrier and sector: 61 dBm at 5 MHz or less (NTIA LTE transmitter characteristics), plus 10*log10(bw/5)
above that, capped by the non-rural 47 CFR 27.50 base-station limit of the carrier's band: 1,000 W/MHz ERP for
617-652 and 698-806 MHz (ERP + 2.15 dB = EIRP), 1,640 W/MHz EIRP for 2110-2155, 3450-3550 and 3700-4140 MHz.
  LTE  64.0 dBm, cap 72.1 dBm (1,640 W/MHz x 10 MHz)
  NR   74.0 dBm, cap 82.1 dBm (1,640 W/MHz x 100 MHz)
azimuth_deg, beamwidth_deg and tilt_deg are NULL (one row per site, not per sector); cell_id is NULL.
operator: the network named for the MCC-MNC pair (table below), else "<mcc>-<mnc>".
site_id: "<radio>:<mcc>-<mnc>-<node>", with the MNC as three digits.

Licence: OpenCelliD data is CC BY-SA 4.0. Every row carries licence CC-BY-SA-4.0 and the database stores
ATTRIBUTION, which credits OpenCelliD, gives the licence URI (section 3(a)(1)(C) requires it) and names the
changes made here. Share-alike: a database adapted from these rows must be offered under CC BY-SA 4.0 or a
compatible licence.
"""

from __future__ import annotations

import collections
import csv
import gzip
import math
from array import array
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from cell_sources import asr

SOURCE = "opencellid"
LICENCE = "CC-BY-SA-4.0"
ATTRIBUTION = (
    "OpenCelliD Project (https://opencellid.org), licensed under CC BY-SA 4.0 "
    "(https://creativecommons.org/licenses/by-sa/4.0/). "
    "Changes: cells merged into sites, positions snapped to FCC ASR structures, radio parameters modelled"
)

COUNTRY = "us"
US_MCC = range(310, 317)
NODE_SHIFT = {"LTE": 8, "NR": 12}
CELL_ID_BITS = {"LTE": 28, "NR": 36}
OUTLIER_M = 10000.0
SNAP_RADIUS_M = 250.0
DEFAULT_HEIGHT_M = 30.0
ASR_HEIGHT_MIN_M = 3.0
CONFIDENCE_SNAPPED = 0.4
CONFIDENCE_ESTIMATED = 0.25

# (freq_mhz, bandwidth_mhz) of the default carrier per radio.
DEFAULT_CARRIER = {"LTE": (2132.5, 10.0), "NR": (3750.0, 100.0)}
NTIA_EIRP_DBM = 61.0
NTIA_WIDTH_MHZ = 5.0
ERP_TO_EIRP_DB = 2.15
# 47 CFR 27.50 non-rural base-station limits: (low MHz, high MHz, W per MHz, limit given as ERP).
EIRP_CAPS = (
    (617.0, 652.0, 1000.0, True),
    (698.0, 806.0, 1000.0, True),
    (2110.0, 2155.0, 1640.0, False),
    (3450.0, 3550.0, 1640.0, False),
    (3700.0, 4140.0, 1640.0, False),
)

# Networks by MCC and MNC: the brand column of "Mobile network codes in ITU region 3xx (North America)",
# https://en.wikipedia.org/wiki/Mobile_network_codes_in_ITU_region_3xx_(North_America), or its operator
# column where the brand is blank.
_NETWORKS = {
    "AT&T": {
        310: (16, 70, 80, 90, 150, 170, 280, 380, 410, 560, 670, 680, 950),
        311: (70, 90, 180, 190),
        312: (90, 680),
        313: (30, 210),
        314: (260,),
    },
    "FirstNet": {312: (670,), 313: tuple(range(100, 200, 10))},
    "Verizon": {
        310: (4, 5, 6, 10, 12, 13, 350, *range(590, 600), 820, *range(890, 900), 910),
        311: (12, 110, *range(270, 290), 390, 440, *range(480, 490), 590, 800, 810),
        312: (770,),
    },
    "T-Mobile": {
        310: (120, 160, 200, 210, 220, 230, 240, 250, 260, 270, 310, 490, 530, 660, 770, 800, 830),
        311: (260, 490, 870, 880, 882, 940),
        312: (190, 250, 530),
    },
    "Metro": {311: (660,)},
    "Virgin Mobile": {310: (53,)},
    "Sprint": {312: (240,)},
    "UScellular": {310: (66, 730), 311: (220, 225, 228, 229, 580, 588, 589)},
    "Dish": {311: (200,), 313: (340, 350, 360)},
    "C Spire": {311: (230, 630)},
    "GCI": {310: (50, 190, 430), 311: (370,)},
    "Cellcom": {310: (510, 600, 620), 311: (840, 850)},
    "Viaero": {310: (450, 740)},
    "Appalachian Wireless": {310: (750,), 312: (120, 130, 910)},
    "Carolina West Wireless": {310: (130,), 312: (470,)},
    "Union Wireless": {310: (20,)},
    "Cellular One": {310: (320,)},
    "Inland Cellular": {310: (580,), 313: (820,)},
    "Nex-Tech Wireless": {311: (100,), 312: (420,)},
    "Chat Mobility": {311: (410, 430), 312: (160, 170)},
    "Limitless Mobile": {310: (340, 690), 311: (600,), 312: (180,)},
    "IT&E Wireless": {310: (32, 110, 400, 480), 311: (120, 250)},
    "Docomo": {310: (370, 470)},
    "Liberty": {313: (790,)},
    "Claro": {313: (510,)},
}
OPERATORS = {(mcc, mnc): name for name, codes in _NETWORKS.items() for mcc, mncs in codes.items() for mnc in mncs}

_RADIO, _MCC, _NET, _CELL, _LON, _LAT, _SAMPLES, _UPDATED = 0, 1, 2, 4, 6, 7, 9, 12
_MIN_COLUMNS = 13


def input_paths(raw_dir: Path) -> list[Path]:
    return [Path(raw_dir) / "opencellid" / "READY"]


def _csv_paths(raw_dir: Path) -> list[Path]:
    folder = Path(raw_dir) / "opencellid"
    return sorted(set(folder.glob("*.csv")) | set(folder.glob("*.csv.gz")))


def _records(raw_dir: Path) -> Iterator[list[str]]:
    for path in _csv_paths(raw_dir):
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            for record in csv.reader(fh):
                if not record or (record[0].strip().lower() == "radio"):
                    continue
                yield record


def _timestamp(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        return 0.0
    return value if 0 < value < 32503680000 else 0.0


def _iso_date(timestamp: float) -> str | None:
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat() if timestamp > 0 else None


def source_date(raw_dir: Path) -> str | None:
    newest = 0.0
    for record in _records(raw_dir):
        if len(record) >= _MIN_COLUMNS:
            newest = max(newest, _timestamp(record[_UPDATED]))
    return _iso_date(newest)


def modelled_eirp_dbm(freq_mhz: float, bandwidth_mhz: float) -> float:
    """NTIA 61 dBm at 5 MHz or less, +10*log10(bw/5) above, capped by the 47 CFR 27.50 limit of the band."""
    eirp = NTIA_EIRP_DBM + 10 * math.log10(max(bandwidth_mhz, NTIA_WIDTH_MHZ) / NTIA_WIDTH_MHZ)
    for low, high, watts_per_mhz, is_erp in EIRP_CAPS:
        if low <= freq_mhz <= high:
            cap = 10 * math.log10(watts_per_mhz * bandwidth_mhz * 1000) + (ERP_TO_EIRP_DB if is_erp else 0.0)
            eirp = min(eirp, cap)
    return round(eirp, 1)


def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights, strict=True))
    total = sum(weights)
    acc = 0.0
    for i, (value, weight) in enumerate(pairs):
        acc += weight
        if acc * 2 > total:
            return value
        if acc * 2 == total:
            return (value + pairs[i + 1][0]) / 2
    return pairs[-1][0]


def _read_clusters(raw_dir: Path, stats: collections.Counter) -> dict[tuple[str, int, int, int], array]:
    """Per cluster key, the kept cells as a flat array of (lat, lon, weight, updated) quadruples."""
    clusters: dict[tuple[str, int, int, int], array] = {}
    for record in _records(raw_dir):
        if len(record) < _MIN_COLUMNS:
            stats["bad row"] += 1
            continue
        radio = record[_RADIO].strip().upper()
        if radio not in NODE_SHIFT:
            stats["not LTE/NR"] += 1
            continue
        try:
            mcc = int(record[_MCC])
            if mcc not in US_MCC:
                stats["not a US MCC"] += 1
                continue
            mnc, cell = int(record[_NET]), int(record[_CELL])
            lat, lon = float(record[_LAT]), float(record[_LON])
        except ValueError:
            stats["bad row"] += 1
            continue
        if not 0 <= mnc <= 999:
            stats["bad row"] += 1
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            stats["bad position"] += 1
            continue
        if not 0 <= cell < 1 << CELL_ID_BITS[radio]:
            stats["bad cell id"] += 1
            continue
        try:
            weight = max(float(record[_SAMPLES]), 1.0)
        except ValueError:
            weight = 1.0
        if not math.isfinite(weight):
            weight = 1.0
        key = (radio, mcc, mnc, cell >> NODE_SHIFT[radio])
        cells = clusters.get(key)
        if cells is None:
            cells = clusters[key] = array("d")
        cells.extend((lat, lon, float(int(weight)), _timestamp(record[_UPDATED])))
    return clusters


def _site(cells: array, stats: collections.Counter) -> tuple[float, float, float] | None:
    """(lat, lon, newest updated) of a cluster, after the outlier rule; None when no cell is left."""
    lats, lons, weights, updated = list(cells[0::4]), list(cells[1::4]), list(cells[2::4]), list(cells[3::4])
    lat, lon = _weighted_median(lats, weights), _weighted_median(lons, weights)
    keep = [i for i in range(len(lats)) if asr.haversine_m(lat, lon, lats[i], lons[i]) <= OUTLIER_M]
    if len(keep) < len(lats):
        stats["cells over 10 km from their site median"] += len(lats) - len(keep)
        if not keep:
            stats["sites with no cell within 10 km of their median"] += 1
            return None
        lats, lons = [lats[i] for i in keep], [lons[i] for i in keep]
        weights, updated = [weights[i] for i in keep], [updated[i] for i in keep]
        lat, lon = _weighted_median(lats, weights), _weighted_median(lons, weights)
    return lat, lon, max(updated)


def _load_index(raw_dir: Path, stats: collections.Counter) -> asr.StructureIndex | None:
    zip_path = Path(raw_dir) / "fcc" / "r_tower.zip"
    if not zip_path.exists():
        stats["no ASR file, positions not snapped"] += 1
        return None
    index = asr.StructureIndex(asr.load_structures(zip_path))
    stats["ASR structures for snapping"] = len(index)
    return index


def iter_rows(raw_dir: Path, stats: collections.Counter) -> Iterator[dict]:
    raw_dir = Path(raw_dir)
    index = _load_index(raw_dir, stats)
    clusters = _read_clusters(raw_dir, stats)
    for key in sorted(clusters):
        radio, mcc, mnc, node = key
        site = _site(clusters.pop(key), stats)
        if site is None:
            continue
        lat, lon, newest = site
        hit = index.nearest(lat, lon, SNAP_RADIUS_M) if index is not None else None
        modelled = ["freq", "bandwidth", "eirp", "height"]
        if hit is not None:
            lat, lon, height, _ = hit[1]
            confidence = CONFIDENCE_SNAPPED
            stats["sites snapped to an ASR structure"] += 1
            if height is not None and height < ASR_HEIGHT_MIN_M:
                stats["ASR height under 3 m, defaulted"] += 1
                height = None
        else:
            lat, lon, height = round(lat, 6), round(lon, 6), None
            confidence = CONFIDENCE_ESTIMATED
            stats["sites at an estimated position"] += 1
            modelled.append("position")
        if height is None:
            height = DEFAULT_HEIGHT_M
        freq, bandwidth = DEFAULT_CARRIER[radio]
        yield {
            "country": COUNTRY,
            "source": SOURCE,
            "licence": LICENCE,
            "site_id": f"{radio}:{mcc}-{mnc:03d}-{node}",
            "cell_id": None,
            "operator": OPERATORS.get((mcc, mnc), f"{mcc}-{mnc:03d}"),
            "radio": radio,
            "lat": lat,
            "lon": lon,
            "freq_mhz": freq,
            "bandwidth_mhz": bandwidth,
            "eirp_dbm": modelled_eirp_dbm(freq, bandwidth),
            "height_m": height,
            "azimuth_deg": None,
            "beamwidth_deg": None,
            "tilt_deg": None,
            "modelled": ",".join(modelled),
            "confidence": confidence,
            "updated": _iso_date(newest),
        }
