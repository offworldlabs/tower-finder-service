"""Build backend/data/us_canada_australia_borders.geojson from the Natural Earth
admin-0 countries shapefile.

Source data: Natural Earth 1:10m Cultural Vectors, admin 0 countries
https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-admin-0-countries/

The shapefile is downloaded automatically if not already present locally.
It is not committed to this repo — it is ~5MB of source data that can be
re-downloaded at any time.

Run from the repo root:
  pip install geopandas
  python scripts/build_borders_geojson.py
"""

import urllib.request
import zipfile
from pathlib import Path

import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
SHAPEFILE_DIR = ROOT / "natural_earth_data"
SHAPEFILE = SHAPEFILE_DIR / "ne_10m_admin_0_countries.shp"
OUTPUT = ROOT / "backend" / "data" / "us_canada_australia_borders.geojson"

DOWNLOAD_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
ADMIN_NAMES = ["United States of America", "Canada", "Australia"]


def _download_shapefile() -> None:
    SHAPEFILE_DIR.mkdir(exist_ok=True)
    zip_path = SHAPEFILE_DIR / "ne_10m_admin_0_countries.zip"
    print(f"Downloading Natural Earth data from {DOWNLOAD_URL} ...")
    urllib.request.urlretrieve(DOWNLOAD_URL, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(SHAPEFILE_DIR)
    zip_path.unlink()
    print("Download complete.")


def main() -> None:
    if not SHAPEFILE.exists():
        _download_shapefile()

    gdf = gpd.read_file(SHAPEFILE)
    gdf = gdf[gdf["ADMIN"].isin(ADMIN_NAMES)][["ADMIN", "geometry"]]
    OUTPUT.unlink(missing_ok=True)
    gdf.to_file(OUTPUT, driver="GeoJSON")
    print(f"Written {OUTPUT.stat().st_size / 1e6:.1f} MB → {OUTPUT}")


if __name__ == "__main__":
    main()
