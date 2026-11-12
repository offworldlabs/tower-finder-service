"""Fetch the Copernicus GLO-30 elevation tiles tower-finder reads, into the DEM directory.

For the operator. Terrain-aware scoring reads one-degree GLO-30 tiles from
<dem dir>/<name>.tif (see services/dem.py). This script downloads the tiles
that cover a bounding box, or a set of receiver points plus a margin, from the
public AWS bucket, and skips every tile already on disk with the size the
bucket reports. The data is the Copernicus DEM GLO-30 public release.

A cell GLO-30 does not list is open sea or land it has not released. The
GLO-90 bucket of the same product lists every land tile, released at 30 m or
not, so a cell in neither list is open sea: the script leaves an empty marker,
<dem dir>/<name>.sea, which the service reads as 0 m. Land GLO-30 has not
released gets no marker and stays missing, so a receiver whose paths need it
stays on smooth earth.

  python3 scripts/fetch_dem.py --point 33.7,-84.4 --point 34.1,-83.9 --margin-km 160
  python3 scripts/fetch_dem.py --bbox 32.5 33.5 -84.6 -84.5 --dry-run

A point with a negative latitude needs the `=` form: --point=-33.7,151.2.
--dem-dir defaults to TOWER_FINDER_DEM_DIR, else <TOWER_FINDER_RUNTIME_DIR or
data/runtime>/dem, which is the folder the service reads when it runs from this
checkout. A compose deployment keeps its runtime dir in the `runtime-data`
volume, so point --dem-dir (or TOWER_FINDER_DEM_DIR) at that volume's folder on
the host, or at the folder the service's TOWER_FINDER_DEM_DIR names. The script
prints the folder it resolved on its first line.

Stdlib only and no repo imports, so it runs from any checkout (scripts/ is not
a package and the image does not ship it). The tile naming rule mirrors
`Dem.tiles_for` in services/dem.py; a test keeps the two in agreement. Tiles
are about 45 MB each: data, never committed and never baked into the image.

Exit status: 1 when a tile or a tile list could not be fetched or a sea marker
written, else 0.
"""

from __future__ import annotations

import argparse
import http.client
import math
import os
import re
import sys
import urllib.request
from pathlib import Path

BUCKET_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
TILE_LIST_URL = f"{BUCKET_URL}/tileList.txt"
# GLO-90, the same product at 90 m: it lists every land tile, those GLO-30 has not released too.
LAND_LIST_URL = "https://copernicus-dem-90m.s3.amazonaws.com/tileList.txt"
KM_PER_DEG = 111.2
_COARSEST_SPACING_DEG = 1 / 360
DEFAULT_MARGIN_KM = 80.0
CHUNK_BYTES = 1 << 20
TIMEOUT_S = 60


def tile_name(lat_deg: int, lon_deg: int) -> str:
    ns = "N" if lat_deg >= 0 else "S"
    ew = "E" if lon_deg >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_deg):02d}_00_{ew}{abs(lon_deg):03d}_00_DEM"


def tiles_for_box(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> list[str]:
    if not (lat_min <= lat_max and lon_min <= lon_max):
        raise ValueError(f"not a box: lat {lat_min}..{lat_max}, lon {lon_min}..{lon_max}")
    south, north = max(lat_min - _COARSEST_SPACING_DEG, -90.0), min(lat_max, 90.0)
    west, east = max(lon_min, -180.0), min(lon_max + _COARSEST_SPACING_DEG, 180.0)
    if south > north or west > east:
        return []
    lats = range(max(math.ceil(south) - 1, -90), min(math.ceil(north) - 1, 89) + 1)
    lons = range(max(math.floor(west), -180), min(math.floor(east), 179) + 1)
    return sorted(tile_name(a, b) for a in lats for b in lons)


def tiles_for_points(points: list[tuple[float, float]], margin_km: float) -> list[str]:
    names: set[str] = set()
    for lat, lon in points:
        dlat = margin_km / KM_PER_DEG
        dlon = margin_km / (KM_PER_DEG * max(math.cos(math.radians(lat)), 0.01))
        names.update(tiles_for_box(lat - dlat, lat + dlat, lon - dlon, lon + dlon))
    return sorted(names)


def tile_url(name: str) -> str:
    return f"{BUCKET_URL}/{name}/{name}.tif"


def dem_dir() -> Path:
    override = os.environ.get("TOWER_FINDER_DEM_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("TOWER_FINDER_RUNTIME_DIR", "data/runtime")) / "dem"


def _remote_size(url: str, opener) -> int | None:
    try:
        with opener(urllib.request.Request(url, method="HEAD"), timeout=TIMEOUT_S) as resp:
            length = resp.headers.get("Content-Length")
    except (OSError, http.client.HTTPException):
        return None
    return int(length) if length is not None else None


def _download(url: str, path: Path, opener) -> int:
    """GET url into path via path + '.part'; the byte count, or an error with no file left behind."""
    part = path.with_name(path.name + ".part")
    try:
        with opener(urllib.request.Request(url, method="GET"), timeout=TIMEOUT_S) as resp:
            expected = resp.headers.get("Content-Length")
            got = 0
            with part.open("wb") as f:
                while chunk := resp.read(CHUNK_BYTES):
                    f.write(chunk)
                    got += len(chunk)
        if expected is None or got != int(expected):
            raise OSError(f"got {got} bytes, server announced {expected}")
        os.replace(part, path)
        return got
    except (OSError, http.client.HTTPException):
        part.unlink(missing_ok=True)
        raise


def _tile_list(url: str, resolution: int, opener) -> set[str]:
    """The tiles a bucket lists, named as GLO-30 names them. ValueError when the body is not such a list.

    Checked word by word because a sea marker is written for every cell that a
    list leaves out: an error page served as a list would mark land as sea.
    """
    with opener(urllib.request.Request(url, method="GET"), timeout=TIMEOUT_S) as resp:
        names = resp.read().decode().split()
    pattern = re.compile(rf"Copernicus_DSM_COG_{resolution}_[NS]\d\d_00_[EW]\d\d\d_00_DEM")
    if not names or not all(pattern.fullmatch(name) for name in names):
        raise ValueError("not a list of tile names")
    return {name.replace(f"_COG_{resolution}_", "_COG_10_") for name in names}


def fetch(names, dem_dir, *, opener=urllib.request.urlopen, dry_run=False, out=sys.stdout) -> int:
    """Fetch the tiles in `names` that the bucket lists and dem_dir lacks, and mark the sea. The exit code."""
    dem_dir = Path(dem_dir)
    print(f"dem dir {dem_dir.resolve()}", file=out)
    lists = []
    for url, resolution in ((TILE_LIST_URL, 10), (LAND_LIST_URL, 30)):
        try:
            lists.append(_tile_list(url, resolution, opener))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            print(f"error tile list {url}: {exc}", file=out)
            return 1
    listed, land = lists
    if not dry_run:
        dem_dir.mkdir(parents=True, exist_ok=True)
    status = 0
    for name in names:
        url, path = tile_url(name), dem_dir / f"{name}.tif"
        if name not in listed and name in land:
            print(f"no tile {name} (land, not released)", file=out)
        elif name not in listed and dry_run:
            print(f"would mark {name} as sea", file=out)
        elif name not in listed:
            try:
                (dem_dir / f"{name}.sea").touch()
                print(f"sea {name}", file=out)
            except OSError as exc:
                print(f"error {name}: {exc}", file=out)
                status = 1
        elif path.is_file() and _remote_size(url, opener) == path.stat().st_size:
            print(f"present {name}", file=out)
        elif dry_run:
            print(f"would fetch {url}", file=out)
        else:
            try:
                print(f"fetched {name} {_download(url, path, opener)}", file=out)
            except (OSError, http.client.HTTPException) as exc:
                print(f"error {name}: {exc}", file=out)
                status = 1
    return status


def _point(text: str) -> tuple[float, float]:
    try:
        lat, lon = (float(part) for part in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected LAT,LON, got {text!r}") from None
    return lat, lon


def main(argv=None, opener=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch the GLO-30 tiles tower-finder needs into the DEM directory.")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--bbox", nargs=4, type=float, metavar=("LAT_MIN", "LAT_MAX", "LON_MIN", "LON_MAX"))
    where.add_argument("--point", action="append", type=_point, metavar="LAT,LON", help="repeatable")
    parser.add_argument("--margin-km", type=float, default=DEFAULT_MARGIN_KM, help="around each --point (default 80)")
    parser.add_argument(
        "--dem-dir",
        type=Path,
        default=None,
        help="default: the folder the service reads when it runs from this checkout; for a compose deployment "
        "point it at the host folder of the runtime-data volume, or at the service's TOWER_FINDER_DEM_DIR",
    )
    parser.add_argument("--dry-run", action="store_true", help="say what would be fetched, write nothing")
    args = parser.parse_args(argv)
    try:
        names = tiles_for_box(*args.bbox) if args.bbox else tiles_for_points(args.point, args.margin_km)
    except ValueError as exc:
        parser.error(str(exc))
    return fetch(
        names,
        args.dem_dir or dem_dir(),
        opener=opener or urllib.request.urlopen,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
