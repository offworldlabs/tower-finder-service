"""FCC Antenna Structure Registration (ASR): standing structures and a nearest-structure index.

Source: the FCC weekly complete registrations file r_tower.zip (US federal agency data). Two members are read,
streamed from the zip: RA.dat (registrations) and CO.dat (coordinates), pipe-delimited without a header. EN.dat
(owner names, addresses, phones) and the other members are never opened.

Field positions (0-based), from the FCC "ASR SQL table definitions" (asr_sql_table_definitions_3.txt) and the
codes in pubacc_asr_codes_data_elem.pdf:

  RA   3 registration_number, 4 unique_system_identifier, 8 status_code, 13 date_dismantled,
       28 height_of_structure, 30 overall_height_above_ground (both metres)
  CO   4 unique_system_identifier, 5 coordinate_type,
       6-9 latitude degrees, minutes, seconds, N/S; 11-14 longitude degrees, minutes, seconds, E/W

RA and CO are joined on unique_system_identifier, the registration key in every ASR table.

Standing structures: status C (Constructed) or G (Granted), without a dismantlement date. Granted registrations
are kept because a structure can stand without its construction notification on file; a granted but unbuilt
structure costs at most the snap distance. Dropped: A (Canceled), I (Dismantled), T (Terminated), D, N, O, P, R,
W and blank. In a recent weekly file this keeps 160,462 of 198,069 registrations (142,025 C and 18,441 G, less 4
without valid coordinates).

Coordinates: coordinate_type T (Tower), one per registration. A (Array) rows locate the elements of an antenna
array and are ignored. Degrees, minutes and seconds with the hemisphere letter give decimal degrees (S and W
negative), rounded to 6 decimals. ASR coordinates are NAD83 (FCC Form 854, items 28-29); the NAD83 to WGS84
difference (about 1-2 m in the conterminous US) is ignored.

Height: height_of_structure, "the highest point of the supporting structure only (WITHOUT appurtenances)" (Form 854
instructions). Cellular antennas are mounted on the structure, so this is the ASR figure closest to their height.
When it is blank or zero, overall_height_above_ground (including antennas and lighting) is used; when both are
missing the height is None.
"""

from __future__ import annotations

import collections
import io
import math
import zipfile
from pathlib import Path

EARTH_RADIUS_M = 6371008.8
STANDING_STATUS = frozenset({"C", "G"})
TOWER_COORDINATES = "T"
GRID_DEG = 0.01

Structure = tuple[float, float, float | None, str]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _lines(zf: zipfile.ZipFile, member: str):
    with zf.open(member) as raw:
        for line in io.TextIOWrapper(raw, encoding="latin-1", newline=""):
            yield line.rstrip("\r\n").split("|")


def _metres(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def _degrees(deg: str, minutes: str, seconds: str, hemisphere: str, limit: float) -> float | None:
    try:
        d, m, s = int(deg), int(minutes or 0), float(seconds or 0)
    except ValueError:
        return None
    if hemisphere not in ("N", "S", "E", "W") or not (0 <= m < 60 and 0 <= s < 60):
        return None
    value = d + m / 60 + s / 3600
    if not 0 <= value <= limit:
        return None
    return round(-value if hemisphere in ("S", "W") else value, 6)


def load_structures(zip_path: Path, stats: collections.Counter | None = None) -> list[Structure]:
    """Standing structures as (lat, lon, height_m, registration_number), from RA.dat and CO.dat.

    `stats`, when given, receives the counts per registration status and the drops.
    """
    stats = collections.Counter() if stats is None else stats
    standing: dict[str, tuple[str, float | None]] = {}
    with zipfile.ZipFile(zip_path) as zf:
        for p in _lines(zf, "RA.dat"):
            if len(p) < 31 or p[0] != "RA":
                stats["RA bad line"] += 1
                continue
            stats["registrations read"] += 1
            status = p[8]
            if status not in STANDING_STATUS:
                stats[f"dropped status {status or 'blank'}"] += 1
            elif p[13]:
                stats["dropped dismantled date"] += 1
            else:
                standing[p[4]] = (p[3], _metres(p[28]) or _metres(p[30]))
                stats[f"kept status {status}"] += 1
        structures: list[Structure] = []
        for p in _lines(zf, "CO.dat"):
            if len(p) < 15 or p[5] != TOWER_COORDINATES or p[4] not in standing:
                continue
            reg, height = standing.pop(p[4])
            lat = _degrees(p[6], p[7], p[8], p[9], 90.0)
            lon = _degrees(p[11], p[12], p[13], p[14], 180.0)
            if lat is None or lon is None or (lat == 0 and lon == 0):
                stats["dropped bad coordinates"] += 1
                continue
            structures.append((lat, lon, height, reg))
    stats["dropped no tower coordinates"] += len(standing)
    stats["structures kept"] += len(structures)
    return structures


class StructureIndex:
    """Nearest structure within a radius, through a uniform latitude/longitude grid (no antimeridian wrap)."""

    def __init__(self, structures: list[Structure], grid_deg: float = GRID_DEG):
        self._grid_deg = grid_deg
        self._grid: dict[tuple[int, int], list[Structure]] = collections.defaultdict(list)
        for s in structures:
            self._grid[(math.floor(s[0] / grid_deg), math.floor(s[1] / grid_deg))].append(s)
        self._size = len(structures)

    def __len__(self) -> int:
        return self._size

    def nearest(self, lat: float, lon: float, radius_m: float) -> tuple[float, Structure] | None:
        """(distance_m, structure) of the nearest structure at most `radius_m` away, or None."""
        if radius_m < 0 or not self._size:
            return None
        dlat = math.degrees(radius_m / EARTH_RADIUS_M)
        dlon = dlat / max(math.cos(math.radians(min(89.9, abs(lat) + dlat))), 1e-6)
        g = self._grid_deg
        best: tuple[float, Structure] | None = None
        for gy in range(math.floor((lat - dlat) / g), math.floor((lat + dlat) / g) + 1):
            for gx in range(math.floor((lon - dlon) / g), math.floor((lon + dlon) / g) + 1):
                for s in self._grid.get((gy, gx), ()):
                    d = haversine_m(lat, lon, s[0], s[1])
                    if d <= radius_m and (best is None or d < best[0]):
                        best = (d, s)
        return best
