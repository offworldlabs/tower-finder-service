"""Ground elevation from Copernicus GLO-30 tiles kept on local disk.

Each tile covers one degree and is a Cloud Optimized GeoTIFF of Float32 heights
in metres. That one layout needs only struct, zlib and numpy to read, so it is
decoded here rather than through rasterio or GDAL, which would add a native
library to the image for a single file format. A file outside that layout, or
one that cannot be read, is refused with a ValueError that names it and says
why, never guessed at.

AWS publishes the tiles without the south row and east column that the original
product shares with its neighbours. The samples of the tile named for (a, b)
are centred from latitude a+1 down to just above a, and from longitude b to
just short of b+1: the tile holds latitudes (a, a+1] and longitudes [b, b+1).
Interpolating past its last row or column therefore reads the first row of the
tile to the south or the first column of the tile to the east. Those seams are
the common case, not an edge one: an 80 km disk around a receiver crosses a
degree line almost anywhere. Columns also thin out with latitude (3600 a degree
below 50, down to 360 above 85), so each row is interpolated at the spacing of
the tile that holds it.

Every GLO-30 file also holds overviews, the image again at a half, a quarter and
an eighth of its resolution, each sample the mean of the block of samples it
covers. A caller whose points are far apart (the terrain profiles step 100 m,
against 30 m between GLO-30 samples) says how far, and is answered from the
coarsest overview whose samples are no further apart than its points, for a
fraction of the decoding; from the full resolution when no overview is that
fine, in a tile that has none, and where the overview would need a neighbouring
tile that cannot continue it.

A point whose tile is not on disk reads NaN. The open sea has no tiles, and
neither has land nobody fetched yet or that GLO-30 has not released, so a
missing file alone cannot tell sea level from a gap. scripts/fetch_dem.py tells
them apart from the bucket's lists and leaves an empty marker, <name>.sea, for a
cell that is open sea; such a cell reads 0 m at every point and is not missing.
The globe is not wrapped at the antimeridian: a point less than one sample west
of 180E would need the first column of a 180W tile, and reads NaN.
"""

import math
import os
import stat
import struct
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np


def dem_dir() -> Path:
    """The tile folder: TOWER_FINDER_DEM_DIR, else <runtime dir>/dem.

    Read on every call rather than at import, so a process that sets either
    variable after importing this module still gets the folder it named.
    """
    override = os.environ.get("TOWER_FINDER_DEM_DIR")
    if override:
        return Path(override)
    return Path(os.environ.get("TOWER_FINDER_RUNTIME_DIR", "data/runtime")) / "dem"


def tile_name(lat_deg: int, lon_deg: int) -> str:
    """The GLO-30 name of the one-degree cell whose south-west corner is given."""
    ns = "N" if lat_deg >= 0 else "S"
    ew = "E" if lon_deg >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_deg):02d}_00_{ew}{abs(lon_deg):03d}_00_DEM"


# The widest sample spacing GLO-30 uses, 360 columns a degree poleward of 85,
# and no overview spaced wider than this is read. No bilinear neighbour of a
# point is further from it than this.
_COARSEST_SPACING_DEG = 1 / 360

# How many images after the first one file may chain before the rest are
# ignored: GLO-30 has three, and a chain that loops back must still end.
_MAX_OVERVIEWS = 16

# How far a file's georeference may sit from the lattice its name implies.
# Far below a sample (2.8e-4 deg), far above the rounding in the doubles GDAL
# writes for 1/3600.
_LATTICE_TOLERANCE_DEG = 1e-9

# Callers compute positions in floating point, so a point meant to sit on a
# sample row lands a few ulps either side of it. Taken at face value it would
# put a weight of 1e-12 on the next row, which may be in a tile that is not on
# disk, and a query on a sample would read NaN for want of a value it does not
# need. In samples: 0.03 mm on the ground at GLO-30 spacing.
_SNAP_SAMPLES = 1e-6

# How many points elevation interpolates at a time, so that its working arrays
# stay the same size however many points one call is given (figures next to
# the block cache bound in Dem.__init__).
_CHUNK_POINTS = 65536

_NEW_SUBFILE_TYPE, _WIDTH, _LENGTH, _BITS_PER_SAMPLE, _COMPRESSION = 254, 256, 257, 258, 259
_SAMPLES_PER_PIXEL, _PLANAR_CONFIGURATION, _PREDICTOR = 277, 284, 317
_TILE_WIDTH, _TILE_LENGTH, _TILE_OFFSETS, _TILE_BYTE_COUNTS = 322, 323, 324, 325
_SAMPLE_FORMAT = 339
_PIXEL_SCALE, _TIEPOINT, _TRANSFORMATION, _GEO_KEYS = 33550, 33922, 34264, 34735
_GDAL_NODATA = 42113
# NewSubfileType bits: a reduced-resolution copy of the first image, a transparency mask.
_REDUCED, _MASK = 1, 4
_WANTED_TAGS = {
    _NEW_SUBFILE_TYPE,
    _WIDTH,
    _LENGTH,
    _BITS_PER_SAMPLE,
    _COMPRESSION,
    _SAMPLES_PER_PIXEL,
    _PLANAR_CONFIGURATION,
    _PREDICTOR,
    _TILE_WIDTH,
    _TILE_LENGTH,
    _TILE_OFFSETS,
    _TILE_BYTE_COUNTS,
    _SAMPLE_FORMAT,
    _PIXEL_SCALE,
    _TIEPOINT,
    _TRANSFORMATION,
    _GEO_KEYS,
    _GDAL_NODATA,
}
# TIFF field types the wanted tags come in. Rationals are not among them.
_FIELD_TYPES = {1: "u1", 2: "S1", 3: "u2", 4: "u4", 6: "i1", 7: "u1", 8: "i2", 9: "i4", 11: "f4", 12: "f8"}
_RASTER_TYPE_KEY = 1025
_PIXEL_IS_AREA, _PIXEL_IS_POINT = 1, 2


@dataclass(frozen=True)
class _Header:
    """One image of a file: the full-resolution one, or an overview of it."""

    width: int
    height: int
    tile_width: int
    tile_height: int
    tiles_across: int
    compression: int
    predictor: int
    byte_order: str
    nodata: float | None
    offsets: np.ndarray
    byte_counts: np.ndarray


# A cell marked as open sea has no file to read a layout from. It is laid out as
# a GLO-30 tile, 3600 rows a degree like every other with the same overviews,
# so the rows of a land tile to its west still line up with it at every
# resolution; every sample is 0 m and no block is read.
_SEA_LEVELS = tuple(
    _Header(
        width=size,
        height=size,
        tile_width=size,
        tile_height=size,
        tiles_across=1,
        compression=1,
        predictor=1,
        byte_order="<",
        nodata=None,
        offsets=np.zeros(1, np.int64),
        byte_counts=np.zeros(1, np.int64),
    )
    for size in (3600, 1800, 900, 450)
)


@dataclass(frozen=True)
class _Tile:
    name: str
    path: Path
    stamp: tuple[int, int]  # st_mtime_ns, st_size of the file the header came from
    levels: tuple[_Header, ...]  # the full-resolution image, then each overview this reader can use
    sea: bool = False  # the <name>.sea marker, read as 0 m everywhere


def _read_at(f, offset: int, size: int, path: Path) -> bytes:
    f.seek(offset)
    data = f.read(size)
    if len(data) != size:
        raise ValueError(f"{path}: truncated, {size} bytes wanted at offset {offset}, {len(data)} there")
    return data


def _first_directory(f, path: Path) -> tuple[str, int]:
    """The byte order, and where the first image file directory is: a COG's full-resolution image."""
    head = _read_at(f, 0, 8, path)
    order = {b"II": "<", b"MM": ">"}.get(head[:2])
    if order is None:
        raise ValueError(f"{path}: not a TIFF file")
    magic, first = struct.unpack(order + "HI", head[2:])
    if magic == 43:
        raise ValueError(f"{path}: BigTIFF is not supported")
    if magic != 42:
        raise ValueError(f"{path}: not a TIFF file")
    return order, first


def _read_tags(f, order: str, offset: int, path: Path) -> tuple[dict, int]:
    """The wanted tags of the directory at offset, and where the next one is (0 after the last).

    A COG's overviews follow its full-resolution image, finest first.
    """
    (count,) = struct.unpack(order + "H", _read_at(f, offset, 2, path))
    entries = _read_at(f, offset + 2, 12 * count + 4, path)
    tags = {}
    for tag, kind, n, inline in struct.iter_unpack(order + "HHI4s", entries[:-4]):
        if tag not in _WANTED_TAGS:
            continue
        item = _FIELD_TYPES.get(kind)
        # GDAL_NODATA is text; every other wanted tag is numbers, at least one.
        if item is None or (kind == 2) != (tag == _GDAL_NODATA) or n == 0:
            raise ValueError(
                f"{path}: tag {tag} holds {n} values of field type {kind}, which this reader does not take"
            )
        size = np.dtype(item).itemsize * n
        # TIFF stores a value of four bytes or fewer in the entry itself.
        raw = inline[:size] if size <= 4 else _read_at(f, struct.unpack(order + "I", inline)[0], size, path)
        tags[tag] = raw.split(b"\0", 1)[0].decode("ascii", "replace") if kind == 2 else np.frombuffer(raw, order + item)
    return tags, struct.unpack(order + "I", entries[-4:])[0]


def _raster_type(tags: dict, path: Path) -> int:
    """GeoKey 1025 from the GeoKeyDirectory; PixelIsArea when absent, as the GeoTIFF spec defaults it."""
    keys = tags.get(_GEO_KEYS)
    if keys is None:
        return _PIXEL_IS_AREA
    keys = np.asarray(keys, np.int64)
    if len(keys) < 4 or len(keys) < 4 + 4 * keys[3]:
        raise ValueError(f"{path}: GeoKeyDirectory is truncated")
    for key, location, _, value in keys[4 : 4 + 4 * int(keys[3])].reshape(-1, 4).tolist():
        if key == _RASTER_TYPE_KEY and location == 0:
            return value
    return _PIXEL_IS_AREA


def _check_lattice(tags: dict, path: Path, width: int, height: int, lat_deg: int, lon_deg: int) -> None:
    """Refuse a file whose samples are not where its name puts them.

    Samples are placed by the name alone: sample (0, 0) centred on the
    north-west corner of the cell, rows 1/height and columns 1/width apart. A
    georeference that disagreed would otherwise be ignored and the heights
    moved without a word.
    """
    if _TRANSFORMATION in tags:
        raise ValueError(f"{path}: georeferenced by a transformation matrix, which this reader does not take")
    scale, tie = tags.get(_PIXEL_SCALE), tags.get(_TIEPOINT)
    if scale is None and tie is None:
        return
    if scale is None or tie is None or len(scale) < 2 or len(tie) < 6:
        raise ValueError(f"{path}: has only part of a georeference (ModelPixelScale and ModelTiepoint go together)")
    raster_type = _raster_type(tags, path)
    if raster_type not in (_PIXEL_IS_AREA, _PIXEL_IS_POINT):
        raise ValueError(f"{path}: GTRasterTypeGeoKey {raster_type} is neither PixelIsArea nor PixelIsPoint")
    # PixelIsArea ties the corner of a pixel, PixelIsPoint its centre.
    centre = 0.5 if raster_type == _PIXEL_IS_AREA else 0.0
    i, j, _, x, y, _ = tie[:6].tolist()
    west = x + (centre - i) * scale[0]
    north = y - (centre - j) * scale[1]
    got = (north, west, height * scale[1], width * scale[0])
    want = (lat_deg + 1, lon_deg, 1.0, 1.0)
    if any(abs(g - w) > _LATTICE_TOLERANCE_DEG for g, w in zip(got, want)):
        raise ValueError(
            f"{path}: georeference puts sample (0, 0) at {north:.9f}, {west:.9f} spanning {got[2]:.9f} x "
            f"{got[3]:.9f} deg, not on the one-degree cell its name gives"
        )


def _image(tags: dict, order: str, path: Path, nodata: float | None = None) -> _Header:
    """The layout of one image, from its directory's tags; ValueError naming the file if this reader does not take it.

    ``nodata`` stands where the directory has no GDAL_NODATA of its own.
    """

    def one(tag: int, default: int | None = None) -> int:
        if tag in tags:
            return int(tags[tag][0])
        if default is None:
            raise ValueError(f"{path}: required tag {tag} is missing")
        return default

    if _TILE_WIDTH not in tags:
        raise ValueError(f"{path}: stored in strips, and this reader takes tiled files only")
    compression, predictor = one(_COMPRESSION, 1), one(_PREDICTOR, 1)
    problems = []
    if compression not in (1, 8):
        problems.append(f"Compression {compression} (only 1, none, and 8, DEFLATE)")
    if predictor not in (1, 3):
        problems.append(f"Predictor {predictor} (only 1, none, and 3, floating point)")
    if one(_SAMPLES_PER_PIXEL, 1) != 1:
        problems.append("more than one sample per pixel")
    if one(_PLANAR_CONFIGURATION, 1) != 1:
        problems.append("PlanarConfiguration other than 1")
    if np.any(np.asarray(tags.get(_BITS_PER_SAMPLE, [1])) != 32) or np.any(
        np.asarray(tags.get(_SAMPLE_FORMAT, [1])) != 3
    ):
        problems.append("samples other than 32-bit IEEE float")
    if problems:
        raise ValueError(f"{path}: unsupported layout: " + "; ".join(problems))

    width, height = one(_WIDTH), one(_LENGTH)
    tile_width, tile_height = one(_TILE_WIDTH), one(_TILE_LENGTH)
    if min(width, height, tile_width, tile_height) <= 0:
        raise ValueError(f"{path}: image or tile size is zero")
    across, down = -(-width // tile_width), -(-height // tile_height)
    offsets, byte_counts = tags.get(_TILE_OFFSETS), tags.get(_TILE_BYTE_COUNTS)
    if offsets is None or byte_counts is None or not len(offsets) == len(byte_counts) == across * down:
        raise ValueError(
            f"{path}: tile table does not fit a {width}x{height} image in {tile_width}x{tile_height} tiles"
        )

    stated = tags.get(_GDAL_NODATA)
    if stated is not None:
        try:
            nodata = float(stated.strip())
        except (AttributeError, ValueError):
            raise ValueError(f"{path}: GDAL_NODATA {stated!r} is not a number") from None
    return _Header(
        width=width,
        height=height,
        tile_width=tile_width,
        tile_height=tile_height,
        tiles_across=across,
        compression=compression,
        predictor=predictor,
        byte_order=order,
        nodata=nodata,
        offsets=offsets.astype(np.int64),
        byte_counts=byte_counts.astype(np.int64),
    )


def _read_levels(path: Path, lat_deg: int, lon_deg: int) -> tuple[_Header, ...]:
    """The file's full-resolution image, then each overview of it this reader can use, in the file's order.

    The first image must be one this reader takes, placed where the name says.
    An overview it does not take, or whose directory it cannot read, is left out
    and the full resolution answers in its place, so a file that reads at full
    resolution is never refused for its overviews.
    """
    with open(path, "rb") as f:
        order, offset = _first_directory(f, path)
        tags, offset = _read_tags(f, order, offset, path)
        full = _image(tags, order, path)
        _check_lattice(tags, path, full.width, full.height, lat_deg, lon_deg)
        levels = [full]
        seen: set[int] = set()
        while offset and offset not in seen and len(seen) < _MAX_OVERVIEWS:
            seen.add(offset)
            try:
                tags, offset = _read_tags(f, order, offset, path)
            except ValueError:
                break  # nothing after a directory that cannot be read can be found
            kind = int(tags[_NEW_SUBFILE_TYPE][0]) if _NEW_SUBFILE_TYPE in tags else 0
            if kind & (_REDUCED | _MASK) != _REDUCED:
                continue
            try:
                image = _image(tags, order, path, full.nodata)
            except ValueError:
                continue
            if image.width <= full.width and image.height <= full.height:
                levels.append(image)
    return tuple(levels)


def _level_for(tile: _Tile, lat_deg: int, spacing_deg: float) -> int:
    """Which of the tile's images to read for points ``spacing_deg`` degrees of arc apart: an index into its levels.

    The coarsest overview whose samples are no further apart than that, its
    columns measured at the cell's edge nearest the equator, where they are
    widest; 0, the full resolution, when none is. An overview spaced wider than
    _COARSEST_SPACING_DEG in latitude or longitude is never taken, so the
    neighbours a point reads stay inside the box tiles_for widens.
    """
    widest = math.cos(math.radians(min(abs(lat_deg), abs(lat_deg + 1))))
    best = 0
    for k, image in enumerate(tile.levels[1:], 1):
        if (
            max(1 / image.height, 1 / image.width) <= _COARSEST_SPACING_DEG
            and max(1 / image.height, widest / image.width) <= spacing_deg
            and image.height < tile.levels[best].height
        ):
            best = k
    return best


def _decode_block(tile: _Tile, level: int, index: int) -> np.ndarray:
    """One internal tile of the image ``tile.levels[level]`` as native float32, nodata already NaN.

    An edge block comes back cut to the image, without the padding the file
    stores it with.
    """
    h = tile.levels[level]
    name = f"block {index}" if level == 0 else f"block {index} of overview {level}"
    try:
        with open(tile.path, "rb") as f:
            st = os.fstat(f.fileno())
            if (st.st_mtime_ns, st.st_size) != tile.stamp:
                raise ValueError(f"{tile.path}: replaced while it was being read")
            raw = _read_at(f, int(h.offsets[index]), int(h.byte_counts[index]), tile.path)
    except OSError as exc:
        # Removed or made unreadable since the call found it.
        raise ValueError(f"{tile.path}: {name} cannot be read: {exc.strerror or exc}") from None
    if h.compression == 8:
        try:
            raw = zlib.decompress(raw)
        except zlib.error as exc:
            raise ValueError(f"{tile.path}: {name} does not inflate: {exc}") from None
    if len(raw) != h.tile_height * h.tile_width * 4:
        raise ValueError(f"{tile.path}: {name} holds {len(raw)} bytes, not {h.tile_width}x{h.tile_height}x4")
    rows = min(h.tile_height, h.height - index // h.tiles_across * h.tile_height)
    cols = min(h.tile_width, h.width - index % h.tiles_across * h.tile_width)
    if h.predictor == 3:
        # The floating-point predictor (libtiff fpAcc): each row was stored as
        # byte-wise differences, after its floats were split into four byte
        # planes, most significant first whatever the file's byte order. Undo
        # the differences with a running sum mod 256, interleave the planes
        # back into floats, and read them big-endian.
        planes = np.frombuffer(raw, np.uint8).reshape(h.tile_height, 4 * h.tile_width)
        planes = np.cumsum(planes, axis=1, dtype=np.uint8).reshape(h.tile_height, 4, h.tile_width)
        block = planes[:rows, :, :cols].transpose(0, 2, 1).copy().view(">f4")[..., 0]
    else:
        block = np.frombuffer(raw, h.byte_order + "f4").reshape(h.tile_height, h.tile_width)[:rows, :cols]
    block = block.astype(np.float32)
    if h.nodata is not None:
        # Compared as float32, the type the value was written in.
        block[block == np.float32(h.nodata)] = np.nan
    block.flags.writeable = False
    return block


def _snap(position: np.ndarray) -> np.ndarray:
    nearest = np.rint(position)
    return np.where(np.abs(position - nearest) <= _SNAP_SAMPLES, nearest, position)


def _position(offset_deg: np.ndarray, full: np.ndarray, size: np.ndarray, read: int) -> np.ndarray:
    """Where a point falls among an image's rows or columns, in samples from the first.

    ``offset_deg`` is the point's distance from the cell's north edge (rows)
    or west edge (columns); ``full`` and ``size`` are how many the tile holds a
    degree at full resolution and on image ``read``. An overview's sample k
    averages the full-resolution ones from k * full / size on, so it sits at
    their centre; a point before the first one reads it.
    """
    position = offset_deg * full
    if read:
        position = np.maximum((position + 0.5) * size / full - 0.5, 0.0)
    return _snap(position)


def _blend(v: np.ndarray, wy: np.ndarray, wx0: np.ndarray, wx1: np.ndarray) -> np.ndarray:
    """Bilinear blend of the four corners v (r0c0, r0c1, r1c0, r1c1), each row with its own column weight.

    Blended only where the far sample carries weight: an unread sample is NaN,
    and NaN * 0 is NaN.
    """
    rows = [np.where(wx > 0, v[2 * k] * (1 - wx) + v[2 * k + 1] * wx, v[2 * k]) for k, wx in enumerate((wx0, wx1))]
    return np.where(wy > 0, rows[0] * (1 - wy) + rows[1] * wy, rows[0])


def _sort_by(key: np.ndarray) -> np.ndarray:
    """A stable order of the non-negative keys, at least one. A call's few fit in 16 bits, where numpy sorts by counting."""
    return np.argsort(key.astype(np.uint16) if key.max() < 2**16 else key, kind="stable")


class _Call:
    """The tiles one elevation call has looked at, each checked on disk once, and the image of each it reads."""

    def __init__(self, dem: "Dem", spacing_deg: float):
        self.dem = dem
        self.spacing_deg = spacing_deg
        self.tiles: list[_Tile] = []
        self._levels: list[int] = []  # per tile, the level _level_for chose
        self._ids: dict[tuple[int, int], int] = {}
        self._fields: dict[tuple[str, int], np.ndarray] = {}

    def ids(self, lat_deg: np.ndarray, lon_deg: np.ndarray) -> np.ndarray:
        """Index into self.tiles of each cell's tile, -1 where it is not on disk."""
        if lat_deg.size == 0:
            return np.empty(0, np.int64)
        # Packed into one integer, non-negative for every cell a point on the
        # globe can ask for and below 72400, so the cells present are found by
        # counting rather than sorting.
        codes = (lat_deg + 100) * 400 + (lon_deg + 200)
        low = int(codes.min())
        codes = codes - low
        found = np.empty(int(codes.max()) + 1, np.int64)
        for code in np.flatnonzero(np.bincount(codes)).tolist():
            cell = ((code + low) // 400 - 100, (code + low) % 400 - 200)
            if cell not in self._ids:
                tile = self.dem._tile(*cell)
                self._ids[cell] = -1 if tile is None else len(self.tiles)
                if tile is not None:
                    self.tiles.append(tile)
                    self._levels.append(_level_for(tile, cell[0], self.spacing_deg))
                    self._fields.clear()
            found[code] = self._ids[cell]
        return found[codes]

    def level(self, ids: np.ndarray) -> np.ndarray:
        """Per id, the level of its tile this call reads. Every id must be a tile that is on disk."""
        return np.array(self._levels or [0], np.int64)[ids]

    def field(self, ids: np.ndarray, name: str, level: int = 0) -> np.ndarray:
        """One header field of the image at ``level`` per id, -1 for a tile without that image.

        Every id must be a tile that is on disk.
        """
        if (name, level) not in self._fields:
            values = [getattr(t.levels[level], name) if level < len(t.levels) else -1 for t in self.tiles]
            self._fields[name, level] = np.array(values or [0], np.int64)
        return self._fields[name, level][ids]

    def sea(self, ids: np.ndarray) -> np.ndarray:
        """Per id, whether the tile is a sea marker. Every id must be a tile that is on disk."""
        return np.array([t.sea for t in self.tiles] or [False])[ids]

    def lines_up(self, near: np.ndarray, far: np.ndarray, level: int, columns: bool = False) -> np.ndarray:
        """Per pair of ids, whether far's image at ``level`` continues the rows of near's, and its columns too if asked.

        It does when both tiles have that many rows (and columns) at full
        resolution and at ``level``, or when far is open sea with that level,
        where every sample is 0 m wherever it sits.
        """
        same = self.field(far, "height", level) > 0
        names = ("height", "width") if columns else ("height",)
        for name in names:
            same &= self.field(near, name) == self.field(far, name)
            same &= self.field(near, name, level) == self.field(far, name, level)
        return same | (self.sea(far) & (self.field(far, "height", level) > 0))

    def check_rows_line_up(self, west: np.ndarray, east: np.ndarray) -> None:
        # Column `width` of a tile is read from row r of the tile to its east,
        # which is the same latitude only if both tiles have as many rows.
        # GLO-30 has 3600 in every tile; anything else would misplace data.
        bad = np.flatnonzero(self.field(west, "height") != self.field(east, "height"))
        if bad.size:
            w, e = self.tiles[west[bad[0]]], self.tiles[east[bad[0]]]
            raise ValueError(f"{w.path} and {e.path}: neighbours across a meridian with different row counts")


class Dem:
    """Bilinear ground elevation over a folder of GLO-30 tiles, <root>/<tile_name>.tif.

    A cell with no tile but an empty <root>/<tile_name>.sea is open sea, 0 m;
    a tile next to a marker is read and the marker ignored. Files are checked on
    every call, so a tile fetched while the process runs is picked up and a
    replaced one is read afresh. Safe to share between threads.
    """

    def __init__(self, root: Path, *, cache_bytes: int = 256 * 2**20):
        if cache_bytes < 0:
            raise ValueError("cache_bytes cannot be negative")
        self.root = Path(root)
        self._cache_bytes = cache_bytes
        # Decoded blocks, least recently used first, keyed by (name, mtime_ns,
        # size, level, block index) so a replaced file never answers from its
        # old bytes, and bounded by the bytes they hold, cut to the image: at
        # most cache_bytes, 256 MiB by default. A call adds about 22 MiB of
        # working arrays at its peak, whatever its size, since it interpolates
        # _CHUNK_POINTS points at a time, to its result and the caller's
        # points, 24 bytes a point between them: an 80 km rx_horizon at the
        # scoring defaults (576k points) peaks about 53 MiB over the cache,
        # every array of its own included.
        # Points 100 m apart read GLO-30's first overview, 1800 rows a tile in
        # blocks of 1024 x 1024 float32 (4 MiB) or less. Below 70 degrees the
        # blocks within 150 km of a receiver, which hold its 80 km horizon and
        # the path to every tower of a radius-150 query, are at most 62 and
        # 161 MiB, so the default keeps a query's whole working set: a query
        # decodes each block it reads once, however many of its chunks read
        # it, and scoring the receiver again decodes nothing. A query whose
        # blocks do not all fit (a wider radius, or full-resolution reads)
        # decodes some of them again, and the heights come out the same.
        self._blocks: OrderedDict[tuple[str, int, int, int, int], np.ndarray] = OrderedDict()
        self._cached_bytes = 0
        # name -> ((mtime_ns, size), levels). A few hundred bytes each.
        self._headers: dict[str, tuple[tuple[int, int], tuple[_Header, ...]]] = {}
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.tif"

    def _sea_path(self, name: str) -> Path:
        return self.root / f"{name}.sea"

    def _stamp(self, path: Path) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return (st.st_mtime_ns, st.st_size) if stat.S_ISREG(st.st_mode) else None

    def _tile(self, lat_deg: int, lon_deg: int) -> _Tile | None:
        if not (-90 <= lat_deg < 90 and -180 <= lon_deg < 180):
            return None
        name = tile_name(lat_deg, lon_deg)
        path = self._path(name)
        try:
            stamp = self._stamp(path)
            if stamp is None:
                sea = self._sea_path(name)
                stamp = self._stamp(sea)
                return None if stamp is None else _Tile(name, sea, stamp, _SEA_LEVELS, sea=True)
            with self._lock:
                cached = self._headers.get(name)
            if cached is None or cached[0] != stamp:
                cached = (stamp, _read_levels(path, lat_deg, lon_deg))
                with self._lock:
                    self._headers[name] = cached
        except (FileNotFoundError, NotADirectoryError):
            # Removed between the stat and the header: not on disk, as a tile never fetched is.
            return None
        except OSError as exc:
            raise ValueError(f"{path}: cannot be read: {exc.strerror or exc}") from None
        return _Tile(name, path, stamp, cached[1])

    def _block(self, tile: _Tile, level: int, index: int) -> np.ndarray:
        key = (tile.name, *tile.stamp, level, index)
        with self._lock:
            block = self._blocks.get(key)
            if block is not None:
                self._blocks.move_to_end(key)
                return block
        # Decoded outside the lock, so other threads' cache hits do not queue
        # behind tens of milliseconds of inflating. Two threads missing the same
        # block both decode it, which costs time, not correctness.
        block = _decode_block(tile, level, index)
        with self._lock:
            if key not in self._blocks:
                self._blocks[key] = block
                self._cached_bytes += block.nbytes
            while self._cached_bytes > self._cache_bytes:
                self._cached_bytes -= self._blocks.popitem(last=False)[1].nbytes
        return block

    def elevation(self, lat, lon, *, spacing_deg: float = 0.0) -> np.ndarray:
        """Ground height in metres at each point, bilinear between samples.

        lat and lon broadcast together, and the result has their broadcast
        shape. NaN where a sample the point needs is in a tile not on disk, is
        nodata, or the point is off the globe. A sample whose weight is exactly
        zero is not needed: a point on a sample row reads no neighbour row.

        ``spacing_deg`` is how far apart the caller's points are, in degrees of
        arc. Each tile is read at the coarsest of its images whose samples are
        no further apart (see _level_for), the full resolution by default. An
        overview sample is the mean of a block of full-resolution samples and
        sits at that block's centre, so the overview's first row and column
        sit inside the cell; a point between them and the cell's north or west
        edge reads that row or column, and the tiles to the north and west are
        never read. A point whose neighbours lie in a tile without the same
        overview, or one laid out otherwise, reads the full resolution.

        Raises ValueError naming the file, and no other exception, for a tile
        on disk that cannot be opened, read, placed or decoded, or that is
        replaced or removed while the call reads it; a block that cannot be
        read is named too. The one case that reads NaN instead is a tile
        removed after the call finds it but before its header is read: it
        counts as not on disk.
        """
        lat, lon = np.broadcast_arrays(np.asarray(lat, dtype=np.float64), np.asarray(lon, dtype=np.float64))
        out = np.full(lat.size, np.nan)
        call = _Call(self, spacing_deg)
        for start in range(0, out.size, _CHUNK_POINTS):
            part = slice(start, start + _CHUNK_POINTS)
            self._interpolate(call, lat.flat[part], lon.flat[part], out[part])
        return out.reshape(lat.shape)

    def _interpolate(self, call: _Call, lat: np.ndarray, lon: np.ndarray, out: np.ndarray) -> None:
        """Write the elevation at each of the flat points lat, lon into out, which holds NaN."""
        # The tile that holds each point: latitudes (a, a+1], longitudes [b, b+1).
        at = np.flatnonzero((np.abs(lat) <= 90) & (np.abs(lon) <= 180))
        lat, lon = lat[at], lon[at]
        cell_lat = np.ceil(lat).astype(np.int64) - 1
        cell_lon = np.floor(lon).astype(np.int64)
        home = call.ids(cell_lat, cell_lon)
        held = home >= 0
        at, lat, lon, cell_lat, cell_lon, home = (x[held] for x in (at, lat, lon, cell_lat, cell_lon, home))
        level = call.level(home)
        for read in np.flatnonzero(np.bincount(level, minlength=1)).tolist():
            p = np.flatnonzero(level == read)
            points = lat[p], lon[p], cell_lat[p], cell_lon[p], home[p]
            values, coarse_failed = self._bilinear(call, read, *points)
            redo = np.flatnonzero(coarse_failed)
            if redo.size:
                values[redo] = self._bilinear(call, 0, *(x[redo] for x in points))[0]
            out[at[p]] = values

    def _bilinear(
        self,
        call: _Call,
        read: int,
        lat: np.ndarray,
        lon: np.ndarray,
        cell_lat: np.ndarray,
        cell_lon: np.ndarray,
        home: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """The elevation of each point on image ``read`` of its tiles, and whether that image failed it.

        ``home`` is the tile of each point, and has that image. A point fails
        when a neighbour it needs is in a tile whose image ``read`` does not
        continue its own (see _Call.lines_up); its height then reads NaN here.
        At full resolution that never happens. Most points have all four
        neighbours in one block of their own tile, and are read straight from
        it; the rest, on a block's last row or column or on open sea, go
        through _across.
        """
        height, width = call.field(home, "height", read), call.field(home, "width", read)
        fr = _position(cell_lat + 1 - lat, call.field(home, "height"), height, read)
        fc = _position(lon - cell_lon, call.field(home, "width"), width, read)
        r0, c0 = np.floor(fr).astype(np.int64), np.floor(fc).astype(np.int64)
        wy, wx = fr - r0, fc - c0
        tile_height, tile_width = call.field(home, "tile_height", read), call.field(home, "tile_width", read)
        block_row, block_col = r0 % tile_height, c0 % tile_width
        inside = (r0 + 1 < height) & (c0 + 1 < width) & (block_row + 1 < tile_height) & (block_col + 1 < tile_width)
        inside &= ~call.sea(home)
        values, failed = np.empty(lat.size), np.zeros(lat.size, bool)

        i = np.flatnonzero(inside)
        if i.size:
            block = r0[i] // tile_height[i] * call.field(home[i], "tiles_across", read) + c0[i] // tile_width[i]
            tile = home[i]
            order = _sort_by(tile * (int(block.max()) + 1) + block)
            i, tile, block, r, c = i[order], tile[order], block[order], block_row[i][order], block_col[i][order]
            v = np.empty((4, i.size))
            starts = np.flatnonzero(np.r_[True, (tile[1:] != tile[:-1]) | (block[1:] != block[:-1])])
            for s, e in zip(starts.tolist(), np.r_[starts[1:], i.size].tolist()):
                data = self._block(call.tiles[tile[s]], read, int(block[s]))
                for k, (dr, dc) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
                    v[k, s:e] = data[r[s:e] + dr, c[s:e] + dc]
            values[i] = _blend(v, wy[i], wx[i], wx[i])

        o = np.flatnonzero(~inside)
        if o.size:
            values[o], failed[o] = self._across(call, read, lon[o], cell_lat[o], cell_lon[o], home[o], r0[o], wy[o])
        return values, failed

    def _across(
        self,
        call: _Call,
        read: int,
        lon: np.ndarray,
        cell_lat: np.ndarray,
        cell_lon: np.ndarray,
        home: np.ndarray,
        r0: np.ndarray,
        wy: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """_bilinear for points whose neighbours may sit in other blocks or tiles, from their row r0 and its weight wy."""
        n = lon.size
        failed = np.zeros(n, bool)
        height = call.field(home, "height", read)
        wx = np.zeros((2, n))
        # Every sample to read, as (slot * n + point, tile, row, column), the
        # four slots being the corners r0c0, r0c1, r1c0, r1c1.
        wanted = []
        for k in (0, 1):
            p = np.arange(n) if k == 0 else np.flatnonzero(wy > 0)
            # Row index `height` is the first row of the tile to the south.
            south = r0[p] + k >= height[p]
            row = np.where(south, 0, r0[p] + k)
            row_lat = cell_lat[p] - south
            row_tile = call.ids(row_lat, cell_lon[p])
            here = row_tile >= 0
            if read:
                off = here & south & ~call.lines_up(home[p], row_tile, read)
                failed[p[off]] = True
                here &= ~off
            p, row, row_lat, row_tile = p[here], row[here], row_lat[here], row_tile[here]
            # Along the row at the spacing of the tile that holds the row.
            width = call.field(row_tile, "width", read)
            fc = _position(lon[p] - cell_lon[p], call.field(row_tile, "width"), width, read)
            c0 = np.floor(fc).astype(np.int64)
            wx[k, p] = fc - c0
            for j in (0, 1):
                q = np.arange(p.size) if j == 0 else np.flatnonzero(wx[k, p] > 0)
                col, tile = c0[q] + j, row_tile[q]
                # Column index `width` is the first column of the tile to the east.
                east = np.flatnonzero(col >= width[q])
                if east.size:
                    tile[east] = call.ids(row_lat[q][east], cell_lon[p][q][east] + 1)
                    col[east] = 0
                    present = east[tile[east] >= 0]
                    if read:
                        off = present[~call.lines_up(row_tile[q][present], tile[present], read, columns=True)]
                        failed[p[q][off]] = True
                        tile[off] = -1
                    else:
                        call.check_rows_line_up(row_tile[q][present], tile[present])
                wanted.append(((2 * k + j) * n + p[q], tile, row[q], col))
        v = self._gather(call, read, wanted, 4 * n).reshape(4, n)
        return _blend(v, wy, wx[0], wx[1]), failed

    def _gather(self, call: _Call, read: int, wanted: list, size: int) -> np.ndarray:
        """The sample of image ``read`` at each wanted (target, tile, row, column), NaN where its tile is absent, 0 on a sea cell.

        Samples are grouped by block, so each block is fetched once per chunk
        of points however many of them read it.
        """
        values = np.full(size, np.nan)
        target, tile, row, col = (np.concatenate(x) for x in zip(*wanted))
        present = tile >= 0
        present[present] = ~call.sea(tile[present])
        values[target[(tile >= 0) & ~present]] = 0.0
        if not present.any():
            return values
        target, tile, row, col = target[present], tile[present], row[present], col[present]
        tile_height, tile_width = call.field(tile, "tile_height", read), call.field(tile, "tile_width", read)
        block = (row // tile_height) * call.field(tile, "tiles_across", read) + col // tile_width
        order = _sort_by(tile * (int(block.max()) + 1) + block)
        target, tile, block = target[order], tile[order], block[order]
        row, col = row[order] % tile_height[order], col[order] % tile_width[order]
        starts = np.flatnonzero(np.r_[True, (tile[1:] != tile[:-1]) | (block[1:] != block[:-1])])
        for s, e in zip(starts.tolist(), np.r_[starts[1:], tile.size].tolist()):
            data = self._block(call.tiles[tile[s]], read, int(block[s]))
            values[target[s:e]] = data[row[s:e], col[s:e]]
        return values

    def tiles_for(self, lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> list[str]:
        """Every tile elevation may read for a point in the closed box, sorted.

        The box is widened by one coarsest sample spacing to the south and east,
        where a point's neighbours can sit in the next tile; north and west
        neighbours are never needed. Clamped to the globe, with no wrap at the
        antimeridian.
        """
        if not (lat_min <= lat_max and lon_min <= lon_max):
            raise ValueError(f"not a box: lat {lat_min}..{lat_max}, lon {lon_min}..{lon_max}")
        south, north = max(lat_min - _COARSEST_SPACING_DEG, -90.0), min(lat_max, 90.0)
        west, east = max(lon_min, -180.0), min(lon_max + _COARSEST_SPACING_DEG, 180.0)
        if south > north or west > east:
            return []
        lats = range(max(math.ceil(south) - 1, -90), min(math.ceil(north) - 1, 89) + 1)
        lons = range(max(math.floor(west), -180), min(math.floor(east), 179) + 1)
        return sorted(tile_name(a, b) for a in lats for b in lons)

    def tiles_along(self, lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> list[str]:
        """Every tile elevation may read for a point on the line from a to b, sorted.

        The line is straight in latitude and longitude, as a profile on the
        scoring's tangent plane is, and runs the short way round: b's longitude
        is taken within 180 degrees of a's, and points past the antimeridian
        wrap. A point reads the tiles ``tiles_for`` gives for it alone, and
        those change only where the latitude, the latitude one coarsest spacing
        further south, the longitude, or the longitude one spacing further east
        crosses a whole degree. So the line is looked at on each crossing,
        halfway between every two, and at both ends: every tile some point of
        it reads, and not the rest of the box around it.
        """
        lon_b = lon_a + (lon_b - lon_a + 180.0) % 360.0 - 180.0
        s = _COARSEST_SPACING_DEG
        cuts = {0.0, 1.0}
        for start, end in ((lat_a, lat_b), (lat_a - s, lat_b - s), (lon_a, lon_b), (lon_a + s, lon_b + s)):
            low, high = min(start, end), max(start, end)
            cuts.update((k - start) / (end - start) for k in range(math.floor(low) + 1, math.ceil(high)))
        cuts = sorted(cuts)
        names: set[str] = set()
        for t in cuts + [(u + v) / 2 for u, v in zip(cuts, cuts[1:])]:
            lat = lat_a + t * (lat_b - lat_a)
            lon = (lon_a + t * (lon_b - lon_a) + 180.0) % 360.0 - 180.0
            names.update(self.tiles_for(lat, lat, lon, lon))
        return sorted(names)

    def missing(self, lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> list[str]:
        """The tiles of tiles_for that are neither on disk under root nor marked as sea there.

        Existence only: a tile on disk that cannot be read is not listed here,
        and shows up instead as the ValueError elevation raises for it.
        """
        return self._absent(self.tiles_for(lat_min, lat_max, lon_min, lon_max))

    def missing_along(self, lat_a: float, lon_a: float, ends) -> list[str]:
        """The tiles of tiles_along from a to any of ``ends``, (lat, lon) pairs, not on disk or marked as sea, sorted.

        Each tile is looked for on disk once, however many of the lines pass over it.
        """
        names: set[str] = set()
        for lat_b, lon_b in ends:
            names.update(self.tiles_along(lat_a, lon_a, lat_b, lon_b))
        return self._absent(sorted(names))

    def _absent(self, names: list[str]) -> list[str]:
        return [n for n in names if self._stamp(self._path(n)) is None and self._stamp(self._sea_path(n)) is None]
