"""Reading GLO-30 tiles: decoding, bilinear interpolation, seams between tiles, absent tiles."""

import importlib.util
import io
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from services.dem import Dem

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "fetch_dem.py"
_spec = importlib.util.spec_from_file_location("fetch_dem", _PATH)
fetch_dem = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_dem)


def write_tile(
    root,
    lat_deg,
    lon_deg,
    image,
    *,
    tile=16,
    order="<",
    deflate=True,
    predictor=3,
    georef=True,
    nodata=None,
    overviews=(),
):
    """A one-band Float32 tiled GeoTIFF of `image`, named and laid out as GLO-30 is.

    Edge tiles are padded to full size with a value no test reads back, so a
    reader that indexes into the padding shows it. `nodata` goes in GDAL_NODATA.
    Each of `overviews` follows as a reduced-resolution image in the same layout.
    """
    body = (b"II" if order == "<" else b"MM") + struct.pack(order + "HI", 42, 8)
    for level, picture in enumerate((image, *overviews)):
        height, width = picture.shape
        down, across = -(-height // tile), -(-width // tile)
        padded = np.full((down * tile, across * tile), 7777.0, np.float32)
        padded[:height, :width] = picture
        blocks = []
        for r in range(down):
            for c in range(across):
                block = padded[r * tile : (r + 1) * tile, c * tile : (c + 1) * tile]
                if predictor == 3:
                    # Byte planes, most significant first, then byte-wise differences along each row.
                    planes = block.astype(">f4").view(np.uint8).reshape(tile, tile, 4).transpose(0, 2, 1)
                    data = np.diff(planes.reshape(tile, -1), axis=1, prepend=np.uint8(0)).tobytes()
                else:
                    data = block.astype(order + "f4").tobytes()
                blocks.append(zlib.compress(data) if deflate else data)

        offsets = [0] * len(blocks)
        tags = [(254, 4, [1])] if level else []
        tags += [(256, 4, [width]), (257, 4, [height]), (258, 3, [32]), (259, 3, [8 if deflate else 1])]
        tags += [(277, 3, [1]), (284, 3, [1]), (317, 3, [predictor]), (322, 3, [tile]), (323, 3, [tile])]
        tags += [(324, 4, offsets), (325, 4, [len(b) for b in blocks]), (339, 3, [3])]
        if georef and not level:
            # PixelIsPoint: the tiepoint is the centre of sample (0, 0), on the cell's north-west corner.
            tags += [(33550, 12, [1 / width, 1 / height, 0.0]), (33922, 12, [0, 0, 0, lon_deg, lat_deg + 1, 0])]
            tags += [(34735, 3, [1, 1, 0, 1, 1025, 0, 1, 2])]
        if nodata is not None and not level:
            tags += [(42113, 2, f"{nodata}\0".encode())]

        def payload(kind, values):
            return values if kind == 2 else struct.pack(order + {3: "H", 4: "I", 12: "d"}[kind] * len(values), *values)

        extra_at = len(body) + 2 + 12 * len(tags) + 4
        position = extra_at + sum(len(payload(k, v)) for _, k, v in tags if len(payload(k, v)) > 4)
        for i, block in enumerate(blocks):
            offsets[i], position = position, position + len(block)
        entries, extra = b"", b""
        for tag, kind, values in tags:
            data = payload(kind, values)
            entries += struct.pack(order + "HHI", tag, kind, len(values))
            if len(data) <= 4:
                entries += data.ljust(4, b"\0")
            else:
                entries += struct.pack(order + "I", extra_at + len(extra))
                extra += data
        following = position if level < len(overviews) else 0
        body += struct.pack(order + "H", len(tags)) + entries + struct.pack(order + "I", following) + extra
        body += b"".join(blocks)
    ns, ew = "N" if lat_deg >= 0 else "S", "E" if lon_deg >= 0 else "W"
    name = f"Copernicus_DSM_COG_10_{ns}{abs(lat_deg):02d}_00_{ew}{abs(lon_deg):03d}_00_DEM.tif"
    (root / name).write_bytes(body)


def test_every_sample_reads_back_exactly_in_both_byte_orders_and_codecs_and_coarse_reads_take_the_overview(tmp_path):
    rng = np.random.default_rng(3)
    images = {}
    # Little-endian, DEFLATE, floating-point predictor, georeferenced; then
    # big-endian, uncompressed, no predictor, placed by its name alone. 24 x 20
    # in 16 x 16 tiles, so three of the four tiles are padded.
    for lon_deg, layout in ((20, {}), (21, dict(order=">", deflate=False, predictor=1, georef=False))):
        images[lon_deg] = rng.uniform(-400, 8800, (20, 24)).astype(np.float32)
        write_tile(tmp_path, 10, lon_deg, images[lon_deg], **layout)
    # 720 samples a degree, 100 row + column, with an overview of half as
    # many: sample (i, j) is 5000 m above the mean of the full samples it
    # covers, rows 2i and 2i+1 by columns 2j and 2j+1, and sits at their centre.
    i, j = np.mgrid[0:360, 0:360]
    rows, cols = np.mgrid[0:720, 0:720]
    write_tile(
        tmp_path, 10, 22, (100 * rows + cols).astype(np.float32), tile=256, overviews=[5000 + 200 * i + 2 * j + 50.5]
    )

    dem = Dem(tmp_path)
    rows, cols = np.mgrid[0:20, 0:24]
    for lon_deg, image in images.items():
        assert np.array_equal(dem.elevation(11 - rows / 20, lon_deg + cols / 24), image)
        # A tile with no overview answers a coarse read at full resolution.
        assert np.array_equal(dem.elevation(11 - rows / 20, lon_deg + cols / 24, spacing_deg=1 / 360), image)
    # 100.25 full rows down and 200.75 columns across: 100 * 100.25 + 200.75 at
    # full resolution, and 5000 m more where points 1/360 deg apart read the overview.
    lat, lon = 11 - 100.25 / 720, 22 + 200.75 / 720
    assert dem.elevation(lat, lon) == pytest.approx(10225.75, abs=1e-6)
    assert dem.elevation(lat, lon, spacing_deg=1 / 360) == pytest.approx(15225.75, abs=1e-6)


def test_a_point_between_samples_and_one_across_a_tile_seam_interpolate_bilinearly(tmp_path):
    rows, cols = np.mgrid[0:4, 0:4]
    write_tile(tmp_path, 10, 20, (100 * rows + cols**2).astype(np.float32))
    write_tile(tmp_path, 10, 21, (2000 + 100 * rows + cols).astype(np.float32))
    # The tile to the south has half the columns: a latitude-band edge, as south
    # of the equator, where the tile to the south is the one further from it.
    write_tile(tmp_path, 9, 20, np.array([[1000, 1010]] * 4, np.float32))
    dem = Dem(tmp_path)

    # Rows 1 and 2 weighted 3:1, columns 2 and 3 evenly: 104, 109 over 204, 209.
    assert dem.elevation(10.6875, 20.625) == 131.5
    # The same rows, the last column against the east tile's first: 109, 2100 over 209, 2200.
    assert dem.elevation(10.6875, 20.875) == 1129.5
    # The north tile's last row (301, 304 at 3:1 is 301.75) against the south
    # tile's first (1000, 1010 at 3:5 is 1006.25), weighted 1:3.
    assert dem.elevation(10.0625, 20.3125) == 830.125


def test_a_point_in_an_absent_tile_or_on_a_nodata_sample_reads_nan(tmp_path):
    image = np.full((4, 4), 55.0, np.float32)
    image[0, 0] = -9999
    write_tile(tmp_path, 10, 20, image, nodata=-9999)
    dem = Dem(tmp_path)

    present, absent, nodata = dem.elevation([10.5, 10.5, 11], [20.5, 21.5, 20])
    assert present == 55.0
    assert np.isnan(absent)
    assert np.isnan(nodata)
    assert dem.missing(10.2, 10.8, 20.2, 21.8) == ["Copernicus_DSM_COG_10_N10_00_E021_00_DEM"]


def test_fetch_gets_listed_tiles_not_already_on_disk_and_marks_only_the_open_sea(tmp_path):
    a, b, c, d = (f"Copernicus_DSM_COG_10_N10_00_E0{lon}_00_DEM" for lon in (20, 21, 22, 23))
    (tmp_path / f"{a}.tif").write_bytes(b"tile-a")
    # GLO-90 lists every land tile: c is land GLO-30 has not released, d is in neither list, so open sea.
    land = "".join(f"{name.replace('_COG_10_', '_COG_30_')}\n" for name in (a, b, c))
    root, root_90 = "https://copernicus-dem-30m.s3.amazonaws.com", "https://copernicus-dem-90m.s3.amazonaws.com"
    served = {
        f"{root}/tileList.txt": f"{a}\n{b}\n".encode(),
        f"{root_90}/tileList.txt": land.encode(),
        f"{root}/{a}/{a}.tif": b"tile-a",
        f"{root}/{b}/{b}.tif": b"tile-b-bytes",
    }
    calls = []

    def opener(request, timeout):
        calls.append((request.get_method(), request.full_url))
        body = served[request.full_url]
        resp = io.BytesIO(b"" if request.get_method() == "HEAD" else body)
        resp.headers = {"Content-Length": str(len(body))}
        return resp

    assert fetch_dem.main(["--bbox", "10.2", "10.8", "20.2", "23.8", "--dem-dir", str(tmp_path)], opener=opener) == 0
    assert calls == [
        ("GET", f"{root}/tileList.txt"),
        ("GET", f"{root_90}/tileList.txt"),
        ("HEAD", f"{root}/{a}/{a}.tif"),
        ("GET", f"{root}/{b}/{b}.tif"),
    ]
    assert (tmp_path / f"{b}.tif").read_bytes() == b"tile-b-bytes"
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{a}.tif", f"{b}.tif", f"{d}.sea"]
    # The sea reads 0 m and is not missing; the land GLO-30 has not released still is.
    dem = Dem(tmp_path)
    assert dem.elevation(10.5, 23.5) == 0.0
    assert dem.missing(10.2, 10.8, 20.2, 23.8) == [c]
    # A south edge just above a degree line still reads the tile to its south.
    assert fetch_dem.tiles_for_box(10.001, 10.5, 20.2, 20.4) == Dem(tmp_path).tiles_for(10.001, 10.5, 20.2, 20.4)
