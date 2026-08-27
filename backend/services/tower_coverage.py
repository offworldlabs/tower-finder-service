"""Coverage-area-added scoring for candidate illuminator towers.

Ranks a candidate receiver location by the n>=2 solve-coverage area it would
ADD to the current fleet, not by geometric dilution. Motivation: geometry
already supports ~23 m median position accuracy where n>=2 nodes cover a
point, but only a small fraction of the metro grid has any n>=2 coverage at
all — extent, not precision, is the limiter, so a candidate tower is worth
more the more previously-uncovered ground its beam would put under a second
(or third) node.

Deliberate simplification: the candidate is modeled with the fleet's default
Yagi wedge and legacy monostatic range (no max_bistatic_range declared) —
exactly how a node registering without those keys is scored. Every tower at
the same RX therefore shares one candidate footprint model, and the azimuth
sweep answers "where should the antenna point", not "which tower is better
built". All towers passed to one call get identical scores — that is correct
under this model and still usefully ranks fleets with coverage holes;
per-tower differentiation (fc, EIRP-dependent range) is a documented future
refinement.

Pure module: no fleet state of its own — the caller injects the fleet's node
geometries.

Ported from the monolith's services/tower_coverage.py. Two dependencies could
not come with it, so they are reimplemented here (see _point_in_beam and
NodeGeometry): the monolith imports `_point_in_beam` from retina_analytics and
the Yagi constants from its config.constants, and this service depends on
neither package. The geometric test is the same wedge test; what is missing is
the monolith's learned-FOV and observed-coverage-limit refinements, which are
fed by tracker state this service does not have.
"""

import math
from dataclasses import dataclass

from services.tower_ranking import haversine, initial_bearing

# Fleet Yagi defaults, copied from the monolith's config/constants.py. Kept as
# literals rather than config keys because they describe the hardware a node
# registers with, not anything an operator tunes through tower_config.json.
YAGI_BEAM_WIDTH_DEG = 42.0  # Half-power beamwidth (°) of the fleet Yagis
YAGI_MAX_RANGE_KM = 50.0  # Default Yagi max range (km)

_KM_PER_DEG_LAT = 111.2
_MIN_COS_LAT = 0.01  # floor for cos(rx_lat) so the lon step stays finite at the poles


@dataclass
class NodeGeometry:
    """One fleet node's siting and aim, as much of it as this scoring needs.

    A structural subset of the monolith's retina_analytics.NodeGeometry, so a
    real one can be passed here unchanged: every attribute read below exists on
    it with the same meaning. The reverse does not hold — the fields this one
    omits (altitudes, tx siting, learned fov, observed coverage_limit) are
    populated from tracker state that only the monolith keeps.
    """

    node_id: str
    rx_lat: float
    rx_lon: float
    beam_azimuth_deg: float
    beam_width_deg: float = YAGI_BEAM_WIDTH_DEG
    max_range_km: float = YAGI_MAX_RANGE_KM

    @property
    def footprint_radius_km(self) -> float:
        return self.max_range_km


def _point_in_beam(lat: float, lon: float, geo) -> bool:
    """Is the point inside the node's beam sector? Range + bearing, 2D.

    The monolith imports this from retina_analytics; reimplemented here because
    this service does not depend on that package. Same two theoretical checks
    in the same order (cheap radius first, then the half-beam angular test).

    Deliberately absent, because nothing in this service can supply their
    inputs: the learned-FOV branch (geo.fov replaces both checks) and the
    shrink-only observed-coverage prior (geo.coverage_limit). A geometry
    carrying either is therefore scored on its theoretical wedge alone, which
    over-counts a node whose real coverage has been observed to be smaller.

    Reads footprint_radius_km when the geometry offers one — the monolith's
    NodeGeometry derives it from the bistatic baseline — and falls back to
    max_range_km otherwise.
    """
    radius_km = getattr(geo, "footprint_radius_km", None)
    if radius_km is None:
        radius_km = geo.max_range_km
    if haversine(geo.rx_lat, geo.rx_lon, lat, lon) > radius_km:
        return False
    bearing = initial_bearing(geo.rx_lat, geo.rx_lon, lat, lon)
    angle_diff = abs((bearing - geo.beam_azimuth_deg + 180) % 360 - 180)
    return angle_diff <= geo.beam_width_deg / 2


def annotate_coverage_added(
    towers: list[dict],
    rx_lat: float,
    rx_lon: float,
    geometries: dict,
    *,
    grid_step_km: float = 3.0,
    beam_width_deg: float = YAGI_BEAM_WIDTH_DEG,
    max_range_km: float = YAGI_MAX_RANGE_KM,
    n_azimuths: int = 12,
) -> None:
    """Stamp coverage-added fields onto every tower dict, in place.

    Grids only the candidate's own reachable disk around (rx_lat, rx_lon) —
    cells outside it cannot change what the candidate adds, since the fleet's
    existing coverage there is irrelevant to this candidate. For each surviving
    cell, existing fleet coverage is counted once via `_point_in_beam`, then a
    set of candidate boresights is swept to find the azimuth that newly covers
    the most n==1 cells (upgrading them to n>=2). Computed once per call and
    stamped identically onto every tower — never recomputed per tower.

    No-op when `towers` or `geometries` is empty; the tower dicts are then
    left without these keys (the ranking sort treats a missing key as 0).
    """
    if not towers or not geometries:
        return

    cos_lat = max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT)
    lat_step_deg = grid_step_km / _KM_PER_DEG_LAT
    lon_step_deg = grid_step_km / (_KM_PER_DEG_LAT * cos_lat)
    # Steps-per-radius cancels the 111.2/cos(lat) scaling, so one integer
    # bound covers both axes of the bounding box.
    n_steps = math.ceil(max_range_km / grid_step_km)

    # Precompute once: (bearing_from_rx, existing_fleet_count) per in-range cell.
    cells: list[tuple[float, int]] = []
    for i in range(-n_steps, n_steps + 1):
        cell_lat = rx_lat + i * lat_step_deg
        for j in range(-n_steps, n_steps + 1):
            cell_lon = rx_lon + j * lon_step_deg
            dist = haversine(rx_lat, rx_lon, cell_lat, cell_lon)
            if dist > max_range_km:
                continue
            bearing = initial_bearing(rx_lat, rx_lon, cell_lat, cell_lon)
            existing = sum(1 for geo in geometries.values() if _point_in_beam(cell_lat, cell_lon, geo))
            cells.append((bearing, existing))

    cell_area_km2 = grid_step_km**2
    half_beam = beam_width_deg / 2.0

    best_azimuth = 0.0
    best_area_n2 = -1.0
    best_area_n3 = 0.0
    for k in range(n_azimuths):
        az = k * 360.0 / n_azimuths
        n2_count = 0
        n3_count = 0
        for bearing, existing in cells:
            angle_diff = abs((bearing - az + 180) % 360 - 180)
            if angle_diff > half_beam:
                continue
            if existing == 1:
                n2_count += 1
            elif existing == 2:
                n3_count += 1
        area_n2 = n2_count * cell_area_km2
        area_n3 = n3_count * cell_area_km2
        # Ascending az means the first cell of any tie already holds the
        # lowest azimuth, so only area_n2 / area_n3 need comparing.
        if area_n2 > best_area_n2 or (area_n2 == best_area_n2 and area_n3 > best_area_n3):
            best_azimuth = az
            best_area_n2 = area_n2
            best_area_n3 = area_n3

    added_km2 = round(max(best_area_n2, 0.0), 0)
    n3_km2 = round(max(best_area_n3, 0.0), 0)
    for tower in towers:
        tower["coverage_area_added_km2"] = added_km2
        tower["coverage_area_n3_km2"] = n3_km2
        tower["coverage_best_azimuth_deg"] = float(best_azimuth)
