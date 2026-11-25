"""Expected-detection-area scoring for candidate illuminator towers.

Ranks a broadcast tower by the ground area, in km^2, over which a target of
``target_rcs_m2`` at ``target_alt_km`` would be detected with at least
``snr_min_db`` after coherent processing — not by how loudly the receiver hears
the tower itself. The two disagree in the case that matters most: a megawatt
transmitter a few km away is the loudest signal in the band and close to the
worst illuminator on the list, because its direct path floods the surveillance
channel (DPI) and the bistatic geometry it offers is a thin sliver around the
baseline. Received power alone ranks it first; this model ranks it where it
belongs.

The terms, per 2 km cell of a disk around the receiver:
  * target path: EIRP (plus the band's offset) and the two-way spreading loss
    ``1/(R_t^2 R_r^2)`` with the ``lambda^2 sigma / (4 pi)^3`` bistatic radar
    constant;
  * noise floor: kTB with the receiver noise figure, raised by the direct-path
    signal that survives ``cancellation_db`` of ECA/CLEAN suppression, which is
    what makes a near tower expensive;
  * processing gain ``B * T`` for the band's bandwidth and CPI, which is why a
    6 MHz TV channel buys ~18 dB over an FM carrier;
  * a radio-horizon check the plain FSPL model lacks: beyond the 4/3-earth
    horizon the tower pays 20 dB plus 0.5 dB/km;
  * a bistatic-angle cut: past ``max_bistatic_angle_deg`` the range ellipse is
    too flat to resolve, so those cells count for nothing.

With GLO-30 terrain tiles on disk (``services/dem.py``, ``services/terrain.py``)
two of those terms read the real ground. The receiver's terrain horizon hides
every cell where a target would sit behind a ridge, and the ITU-R P.526
delta-Bullington diffraction loss over the profile from the tower's antenna to
the receiver's takes the place of the smooth-earth horizon loss, in the direct
path and in the threshold alike. A query gets both for every tower or neither:
towers ranked against each other on two models would be ranked on the model as
much as on the towers, so where a tile under the receiver's square or under
any tower's path is missing, unreadable or holds no height, every tower is
scored on the smooth earth exactly as without tiles.

The receiver is a Yagi, so the answer depends on where it points: the model
sweeps ``n_azimuths`` boresights and reports the best one. That azimuth is the
second output, and it is actionable — it is where the operator aims.

Per aircraft class (``class_detection_areas``), at that same boresight: a
class's RCS offset in dB moves the one threshold, so each class gets its own
area and 50 % range (walking ``grid_km`` rings over the cells in the beam, the
outer edge of the last ring at least half detected before the first ring under
half). The default mode keeps every constant above, so an offset of 0 is the
ranking's own area. The fleet mode swaps the link budget for the fleet
calibration's predicted system constant, which already holds the receiver's
gain, noise and direct-path residue, with a low/high band at one prediction
sigma either side. The constants were fitted with a free-space path and no
terrain term, so each fleet node's own path, horizon and terrain included, is
inside them too: the fleet mode charges no horizon or terrain loss, and
score_towers counts its figures over the smooth earth's cells whatever the
tiles say, at the boresight the ranking chose. Neither mode changes the three
ranking outputs.

A cellular carrier (``score_towers`` given a ``CellularScoring``) goes through
the same model on a finer grid of its own, with its own bandwidth, the always-on
share of its radio and its sector pattern. The terrain is frequency-blind and
applies to it as to a broadcast tower, in the same all-or-nothing decision.
Its class figures are always the default mode: the fleet calibration was
fitted on broadcast towers and says nothing about a carrier.

No config I/O of its own. The caller resolves every knob into a
``ScoringParams`` and passes it in; ``services/tower_ranking.py`` builds one
from tower_config.json. The one thing read from disk is the terrain, through a
reader for the folder ``dem_dir()`` names that lives as long as the process.

Ported from a validated pure-Python reference (same equation, same knobs, same
defaults) and vectorised with numpy, because the reference takes ~13 s for the
200 towers a metro search returns and this runs inside the request.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import numpy as np

from services.dem import Dem, dem_dir
from services.fleet_calibration import CLASS_NAMES, DEFAULT_CLASS_OFFSETS_DB, evaluate
from services.terrain import Horizon, TowerEnd, diffraction_losses_db, rx_horizon

logger = logging.getLogger(__name__)

C_M_S = 299_792_458.0

# Flat-earth tangent plane around the receiver. Good to a few metres over the
# 80 km disk this model grids, which is far below the 2 km cell size.
KM_PER_DEG = 111.2
_MIN_COS_LAT = 0.01  # floor for cos(rx_lat) so the lon scaling stays finite at the poles

# Used when the upstream record carries no usable antenna height. A broadcast
# mast is not 0 m, and treating an unknown height as ground level would put
# every such tower past its own radio horizon and score it 0.
DEFAULT_ANTENNA_HEIGHT_M = 100.0

# Per-band processing: effective bandwidth for the ambiguity function, and the
# coherent processing interval. FM is a ~100 kHz carrier integrated for a
# second; a DVB-T/ATSC channel is 6 MHz at half that.
DEFAULT_BAND_PARAMS: dict[str, dict[str, float]] = {
    "FM": {"bw_hz": 100e3, "cpi_s": 1.0},
    "VHF": {"bw_hz": 6e6, "cpi_s": 0.5},
    "UHF": {"bw_hz": 6e6, "cpi_s": 0.5},
}

# A soft prior on the bands, in dB of EIRP, replacing the hard band tier the
# ranking used to apply. Zero everywhere until it is fitted from fleet data:
# a placeholder that shifts nothing is honest, an invented number is not.
DEFAULT_BAND_OFFSET_DB: dict[str, float] = {"VHF": 0.0, "UHF": 0.0, "FM": 0.0}

DEFAULT_RX_HEIGHT_M = 10.0

# Floor for any range that goes into a log. Every R here already carries the
# target altitude under the square root, so this only bites a config with
# target_alt_km at 0 — which validate_config rejects, but a caller constructing
# ScoringParams directly does not go through it.
_MIN_RANGE_M = 1.0

# Floor for the direct-path distance, in km, when the tower sits on top of the
# receiver: fspl(0) is -inf and would make the DPI term infinite.
_MIN_DIRECT_KM = 0.05

_LTE_MAX_BANDWIDTH_MHZ = 20.0  # LTE's widest channel (3GPP TS 36.101); a wider row with no radio is NR


@dataclass(frozen=True)
class ScoringParams:
    """Every knob of the detection-area model, resolved by the caller.

    Frozen so a params value cannot be edited from under a request in flight;
    the two dict fields are copied on the way in by whoever builds it from
    config (see tower_ranking.apply_config).
    """

    cancellation_db: float = 50.0  # DPI suppression an ECA/CLEAN surveillance channel achieves
    target_rcs_m2: float = 10.0
    snr_min_db: float = 13.0
    max_bistatic_angle_deg: float = 150.0  # beyond this the range ellipse is too flat to resolve
    target_alt_km: float = 3.0
    rx_height_m: float = DEFAULT_RX_HEIGHT_M
    # With terrain tiles: the receiver's antenna stands rx_height_m above the
    # ground fitted to the surface within this distance of it, and nothing
    # within it, nor within it of a tower, is an obstacle (services/terrain.py).
    terrain_near_km: float = 0.5
    grid_km: float = 2.0
    max_range_km: float = 80.0
    n_azimuths: int = 24
    yagi_hpbw_deg: float = 42.0  # fleet Yagi half-power beamwidth, as in tower_coverage.py
    yagi_front_to_back_db: float = 20.0
    noise_figure_db: float = 5.0
    # Overwritten at call time from receiver.rx_antenna_gain_dbi, which is the
    # one number the two models (this and the FSPL link budget) must share.
    rx_gain_dbi: float = 6.0
    band_params: dict[str, dict[str, float]] = field(
        default_factory=lambda: {band: dict(p) for band, p in DEFAULT_BAND_PARAMS.items()}
    )
    band_offset_db: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BAND_OFFSET_DB))


@dataclass(frozen=True)
class CellularScoring:
    """The knobs a cellular carrier is scored with on top of ``ScoringParams``.

    Resolved by the caller from backend/config/cellular_config.json, where each
    value's source is documented in tower_ranking. A carrier's bandwidth,
    radio, azimuth and beamwidth come from its own row; these are the shared
    parts and the fallbacks.

    The grid is its own: a cellular footprint is a few km across, and the
    broadcast grid's 2 km cells (with every cell within 2 km of the receiver
    dropped) can only call it 0 or a multiple of 4 km^2.
    """

    grid_km: float
    max_range_km: float
    cpi_s: float
    reference_share_db: dict[str, float]
    unknown_radio_share_db: float
    beamwidth_deg: float
    max_attenuation_db: float


class TowerScore(NamedTuple):
    """What the model says about one tower, in the units the response uses."""

    expected_area_km2: float
    best_azimuth_deg: float
    horizon_km: float


@dataclass(frozen=True)
class FleetPrediction:
    """The fleet calibration's figures for one tower."""

    system_constant_db: float  # S_pred, from the calibration evaluator
    prediction_sigma_db: float  # its prediction sigma
    threshold_db: float  # the document's T
    processing_gain_db: float  # the document's G_proc
    eirp_dbm: float  # the tower EIRP the system constant is referenced to


@dataclass(frozen=True, eq=False)
class TowerTerrain:
    """What the terrain tiles say about one tower, in place of the smooth earth."""

    visible: np.ndarray  # per cell of the grid: a target there clears the receiver's terrain horizon
    loss_db: float  # diffraction loss toward the tower, where horizon_loss_db goes otherwise


def radio_horizon_km(h_tx_m: float, h_rx_m: float = DEFAULT_RX_HEIGHT_M) -> float:
    """Line-of-sight range in km over a 4/3-earth, for two antenna heights.

    Heights are floored at 1 m rather than trusted: an upstream record with a
    null, 0 or negative antennaHeight would otherwise give a 3.57 km horizon
    for the receiver alone and score every tower 0.
    """
    return 3.57 * (math.sqrt(max(h_tx_m, 1.0)) + math.sqrt(max(h_rx_m, 1.0)))


def horizon_loss_db(d_km: float, h_tx_m: float, h_rx_m: float = DEFAULT_RX_HEIGHT_M) -> float:
    """Extra path loss beyond the radio horizon: a blunt diffraction proxy.

    20 dB at the horizon, then 0.5 dB per further km. Nothing below the horizon
    pays anything, which is the only property the ranking leans on: a tower the
    receiver cannot see must not outrank one it can at equal EIRP.
    """
    over = d_km - radio_horizon_km(h_tx_m, h_rx_m)
    if over <= 0:
        return 0.0
    return 20.0 + 0.5 * over


def fspl_db(d_km: float, f_mhz: float) -> float:
    """Free-space path loss in dB. Same form as tower_ranking.fspl."""
    return 20 * math.log10(d_km * 1000) + 20 * math.log10(f_mhz * 1e6) - 147.55


@dataclass(frozen=True)
class CellGrid:
    """The receiver's surveillance disk, and the Yagi pattern over it.

    Everything here is tower-independent — cell offsets are relative to the
    receiver, and the pattern and the beam only need a cell's bearing — so it
    is built once per distinct set of geometry knobs and reused for every tower
    and every request. That is the whole reason 200 towers score in well under
    a second: the per-tower work is one (n_azimuths x n_cells) comparison.
    """

    x_km: np.ndarray  # east offset of each cell from the receiver
    y_km: np.ndarray  # north offset
    r_r_m: np.ndarray  # receiver-to-cell slant range, target altitude included
    r_r_db: np.ndarray  # 20 log10(r_r_m), the receiver's half of every tower's spreading loss
    to_rx_x_km: np.ndarray  # -x_km and -y_km: from each cell towards the receiver
    to_rx_y_km: np.ndarray
    to_rx_km: np.ndarray  # np.hypot(to_rx_x_km, to_rx_y_km), every tower's bistatic angle needs it
    bearing_deg: np.ndarray  # bearing of each cell from the receiver, in [0, 360)
    azimuths_deg: np.ndarray  # the boresights swept
    pattern_db: np.ndarray  # (n_azimuths, n_cells) Yagi response, relative to boresight gain
    in_beam: np.ndarray  # (n_azimuths, n_cells) within half the beamwidth of the boresight
    cell_area_km2: float
    # Rings of grid_km around the receiver, for the 50 % range. Only rings that
    # hold a cell are listed, innermost first; ``ring`` indexes into the other two.
    ring: np.ndarray  # per cell: its ring
    beam_ring_cells: np.ndarray  # (n_azimuths, n_rings) how many of a ring's cells each beam holds
    ring_outer_km: np.ndarray  # per ring: its outer edge, capped at max_range_km


def _yagi_pattern_db(off_boresight_deg, hpbw_deg: float, front_to_back_db: float):
    """Yagi response relative to boresight: quadratic main lobe, floored at F/B."""
    a = np.abs((np.asarray(off_boresight_deg) + 180.0) % 360.0 - 180.0)
    return np.maximum(-12.0 * (a / hpbw_deg) ** 2, -front_to_back_db)


def sector_gain_db(bearing_from_tower_deg, azimuth_deg: float, beamwidth_deg: float, max_attenuation_db: float):
    """A sector antenna's horizontal response relative to boresight, in dB.

    3GPP TR 36.942 v15.0.0, the pattern behind its Figure 4.1: A(theta) = -min(12 (theta /
    theta_3dB)^2, A_m), theta the angle off the sector's azimuth, wrapped so
    350 and 10 degrees are 20 apart. Scalar or array in, same shape out.
    """
    theta = np.abs((np.asarray(bearing_from_tower_deg, dtype=float) - azimuth_deg + 180.0) % 360.0 - 180.0)
    return -np.minimum(12.0 * (theta / beamwidth_deg) ** 2, max_attenuation_db)


@lru_cache(maxsize=8)
def _build_cell_grid(
    grid_km: float,
    max_range_km: float,
    target_alt_km: float,
    n_azimuths: int,
    yagi_hpbw_deg: float,
    yagi_front_to_back_db: float,
) -> CellGrid:
    n = int(max_range_km / grid_km)
    offsets = np.arange(-n, n + 1, dtype=float) * grid_km
    x, y = (a.ravel() for a in np.meshgrid(offsets, offsets, indexing="ij"))

    ground_km = np.hypot(x, y)
    # The cell under the receiver is dropped (R_r -> the target altitude alone,
    # a division by nearly nothing), as is anything outside the disk.
    keep = (ground_km <= max_range_km) & (ground_km >= grid_km)
    x, y, ground_km = x[keep], y[keep], ground_km[keep]

    r_r_m = np.maximum(np.hypot(ground_km, target_alt_km) * 1000.0, _MIN_RANGE_M)
    bearing_deg = np.degrees(np.arctan2(x, y)) % 360.0
    azimuths = np.arange(n_azimuths, dtype=float) * 360.0 / n_azimuths
    pattern = _yagi_pattern_db(bearing_deg[None, :] - azimuths[:, None], yagi_hpbw_deg, yagi_front_to_back_db)
    # The view a node measures its own range in: at most half the beamwidth
    # off boresight, edge included, the angle wrapped to [-180, 180).
    in_beam = np.abs((bearing_deg[None, :] - azimuths[:, None] + 180.0) % 360.0 - 180.0) <= yagi_hpbw_deg / 2

    # Ring j holds the cells with (j-1)*grid_km < ground <= j*grid_km. The
    # epsilon keeps a cell that sits on an edge in the inner ring when the
    # division rounds a hair above the integer.
    ring_j = np.ceil(ground_km / grid_km - 1e-9).astype(np.int64)
    ring_ids, ring = np.unique(ring_j, return_inverse=True)
    n_rings = ring_ids.size
    beam_slot = np.arange(n_azimuths)[:, None] * n_rings + ring[None, :]

    return CellGrid(
        x_km=x,
        y_km=y,
        r_r_m=r_r_m,
        r_r_db=20 * np.log10(r_r_m),
        to_rx_x_km=-x,
        to_rx_y_km=-y,
        to_rx_km=np.hypot(-x, -y),
        bearing_deg=bearing_deg,
        azimuths_deg=azimuths,
        pattern_db=pattern,
        in_beam=in_beam,
        cell_area_km2=grid_km**2,
        ring=ring,
        beam_ring_cells=np.bincount(beam_slot[in_beam], minlength=n_azimuths * n_rings).reshape(n_azimuths, n_rings),
        ring_outer_km=np.minimum(ring_ids * grid_km, max_range_km),
    )


def cell_grid_for(params: ScoringParams) -> CellGrid:
    """The (cached) grid for these params. Build it once, before the tower loop."""
    return _build_cell_grid(
        float(params.grid_km),
        float(params.max_range_km),
        float(params.target_alt_km),
        int(params.n_azimuths),
        float(params.yagi_hpbw_deg),
        float(params.yagi_front_to_back_db),
    )


class _TowerModel(NamedTuple):
    """One tower run through the model: its score, and what the class figures reuse."""

    score: TowerScore
    best: int  # the pattern_db row the score was taken at
    base_db: np.ndarray | None  # per cell, pattern excluded; None on an empty grid
    countable: np.ndarray | None  # per cell: whether a detection there counts at all
    resolvable: np.ndarray | None  # per cell: inside the bistatic-angle cut, what countable is without terrain
    threshold_db: float  # the detection threshold at the best row
    eirp_eff: float
    lam_m: float
    hloss_db: float
    cells: np.ndarray | None = None  # the grid cells the per-cell fields are on; None: every cell


def _evaluate_tower(
    tower_x_km: float,
    tower_y_km: float,
    eirp_dbm: float,
    freq_mhz: float,
    band: str,
    antenna_height_m: float,
    params: ScoringParams,
    grid: CellGrid,
    direct_power_dbm_override: float | None,
    terrain: TowerTerrain | None,
    processing: tuple[float, float, float] | None = None,
    sector: tuple[float, float, float] | None = None,
    *,
    score_only: bool = False,
    below_db: float | None = None,
) -> _TowerModel:
    """The model for one tower, shared by the ranking area and the class figures.

    ``processing`` and ``sector`` are a cellular carrier's, as
    ``expected_detection_area`` describes them; both None is a broadcast tower.
    ``score_only`` works out the score alone, on the cells that can reach a
    threshold, and leaves the per-cell fields None; the score is the same.
    ``below_db`` keeps the per-cell fields of only the countable cells whose
    base reaches the best row's threshold less ``below_db`` (``cells``, with
    ``resolvable`` None): all a threshold at least that low can count.
    """
    if processing is None:
        bp = params.band_params[band]
        bw_hz = float(bp["bw_hz"])
        cpi_s = float(bp["cpi_s"])
    else:
        bw_hz, cpi_s, share_db = (float(value) for value in processing)
    eirp_eff = float(eirp_dbm) + float(params.band_offset_db.get(band, 0.0))

    lam_m = C_M_S / (freq_mhz * 1e6)
    noise_dbm = -174.0 + 10 * math.log10(bw_hz) + params.noise_figure_db
    gproc_db = 10 * math.log10(bw_hz * cpi_s)
    if processing is not None:
        gproc_db += share_db
    const_db = 10 * math.log10(lam_m**2 * params.target_rcs_m2 / (4 * math.pi) ** 3)

    d_tower_km = math.hypot(tower_x_km, tower_y_km)
    tower_bearing_deg = math.degrees(math.atan2(tower_x_km, tower_y_km)) % 360.0
    if terrain is None:
        hloss_db = horizon_loss_db(d_tower_km, antenna_height_m, params.rx_height_m)
    else:
        hloss_db = terrain.loss_db

    if direct_power_dbm_override is None:
        direct_iso_dbm = eirp_eff - fspl_db(max(d_tower_km, _MIN_DIRECT_KM), freq_mhz) - hloss_db
        if sector is not None:
            # The receiver sits at the reverse of the tower's bearing from it.
            direct_iso_dbm += float(sector_gain_db((tower_bearing_deg + 180.0) % 360.0, *sector))
    else:
        direct_iso_dbm = float(direct_power_dbm_override)

    if grid.x_km.size == 0:
        score = TowerScore(0.0, 0.0, round(radio_horizon_km(antenna_height_m, params.rx_height_m), 1))
        return _TowerModel(score, 0, None, None, None, math.nan, eirp_eff, lam_m, hloss_db)

    # Both vectors point away from the cell: towards the tower, and towards the
    # receiver. Taking either the other way round computes 180 - beta, which
    # inverts the cut — it would then discard the forward-scatter cells and
    # keep the unresolvable baseline ones.
    to_tx_x = tower_x_km - grid.x_km
    to_tx_y = tower_y_km - grid.y_km
    to_rx_x, to_rx_y = grid.to_rx_x_km, grid.to_rx_y_km
    r_t_m = np.maximum(np.sqrt(to_tx_x * to_tx_x + to_tx_y * to_tx_y + params.target_alt_km**2) * 1000.0, _MIN_RANGE_M)

    # Per azimuth: the surviving direct path raises the noise floor.
    dpi_dbm = (
        direct_iso_dbm
        + params.rx_gain_dbi
        + _yagi_pattern_db(tower_bearing_deg - grid.azimuths_deg, params.yagi_hpbw_deg, params.yagi_front_to_back_db)
        - params.cancellation_db
    )
    n_eff_dbm = 10 * np.log10(10 ** (noise_dbm / 10) + 10 ** (dpi_dbm / 10))
    threshold_db = params.snr_min_db + n_eff_dbm + hloss_db - eirp_eff - params.rx_gain_dbi - const_db - gproc_db

    if score_only or below_db is not None:
        counts = _reach_counts(to_tx_x, to_tx_y, r_t_m, threshold_db, params, grid, terrain, sector)
    else:
        # Bistatic angle at the target, in the horizontal plane as the reference does.
        dot = to_rx_x * to_tx_x + to_rx_y * to_tx_y
        cos_beta = dot / (grid.to_rx_km * np.hypot(to_tx_x, to_tx_y) + 1e-9)
        beta_deg = np.degrees(np.arccos(np.clip(cos_beta, -1.0, 1.0)))
        resolvable = beta_deg <= params.max_bistatic_angle_deg
        # The one per-cell mask both the ranking count and the class counts apply.
        countable = resolvable if terrain is None else resolvable & terrain.visible

        # Everything about a cell that does not depend on where the antenna points.
        base_db = -20 * np.log10(r_t_m) - grid.r_r_db
        if sector is not None:
            # The cell's bearing from the tower: to_tx points from the cell to it.
            base_db = base_db + sector_gain_db(np.degrees(np.arctan2(-to_tx_x, -to_tx_y)) % 360.0, *sector)

        # Detection test, rearranged so the only (azimuth x cell) array is the one
        # comparison: pattern + base >= threshold(azimuth). Taken over the
        # countable cells whose base alone reaches the lowest threshold: the
        # pattern is at most 0 dB, so no other cell is detected at any azimuth
        # (adding a non-positive float never rounds up), and the counts are the
        # ones the whole grid gives.
        reach = countable & (base_db >= threshold_db.min())
        counts = np.count_nonzero((grid.pattern_db[:, reach] + base_db[reach]) >= threshold_db[:, None], axis=1)

    # argmax takes the first maximum and the azimuths ascend, so a tie picks the
    # lowest azimuth — the reference's tie-break, and stable across runs.
    best = int(np.argmax(counts))
    score = TowerScore(
        expected_area_km2=float(counts[best] * grid.cell_area_km2),
        best_azimuth_deg=float(grid.azimuths_deg[best]),
        horizon_km=round(radio_horizon_km(antenna_height_m, params.rx_height_m), 1),
    )
    if score_only:
        return _TowerModel(score, best, None, None, None, float(threshold_db[best]), eirp_eff, lam_m, hloss_db)
    if below_db is not None:
        floor_db = threshold_db[best] - below_db
        cells, base_db = _countable_cells(to_tx_x, to_tx_y, r_t_m, floor_db, params, grid, terrain, sector)
        countable = np.ones(cells.size, dtype=bool)
        return _TowerModel(
            score, best, base_db, countable, None, float(threshold_db[best]), eirp_eff, lam_m, hloss_db, cells
        )
    return _TowerModel(
        score,
        best,
        base_db,
        countable,
        resolvable,
        float(threshold_db[best]),
        eirp_eff,
        lam_m,
        hloss_db,
    )


def _within_cut(cos_beta: np.ndarray, max_angle_deg: float) -> np.ndarray:
    """degrees(arccos(clip(cos_beta, -1, 1))) <= max_angle_deg, the arccos taken only where it can matter.

    For a cut strictly between 0 and 180 degrees, a cosine more than 1e-9 above
    (below) the cut's lies more than 5e-8 degrees inside (outside) it, far past
    the error of either side; the ones closer go through the expression itself.
    """
    if not 0.0 < max_angle_deg < 180.0:
        return np.degrees(np.arccos(np.clip(cos_beta, -1.0, 1.0))) <= max_angle_deg
    edge = math.cos(math.radians(max_angle_deg))
    within = cos_beta >= edge
    close = np.flatnonzero(np.abs(cos_beta - edge) <= 1e-9)
    if close.size:
        within[close] = np.degrees(np.arccos(np.clip(cos_beta[close], -1.0, 1.0))) <= max_angle_deg
    return within


def _countable_cells(
    to_tx_x: np.ndarray,
    to_tx_y: np.ndarray,
    r_t_m: np.ndarray,
    floor_db: float,
    params: ScoringParams,
    grid: CellGrid,
    terrain: TowerTerrain | None,
    sector: tuple[float, float, float] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """The countable cells whose base reaches ``floor_db``, and their base, as _evaluate_tower works them out.

    The arrays are the whole grid's, as _evaluate_tower has them. Every value
    goes through the same float operations as on the whole grid, only on fewer
    cells. A cell left out is not countable, or its base misses ``floor_db``:
    the pattern is at most 0 dB, so no threshold at or above ``floor_db``
    detects it at any azimuth (adding a non-positive float never rounds up).
    """
    # Only a cell whose base, sector left out (it is at most 0 dB), reaches the
    # floor can count: (R_t R_r)^2 <= 10^(-floor / 10), taken 1e-6 wide (4e-6
    # dB, far above the float error of either side).
    cells = np.flatnonzero((r_t_m * grid.r_r_m) ** 2 <= 10 ** (-floor_db / 10) * (1 + 1e-6))
    base_db = -20 * np.log10(r_t_m[cells]) - grid.r_r_db[cells]
    if sector is not None:
        bearing = np.degrees(np.arctan2(-to_tx_x[cells], -to_tx_y[cells])) % 360.0
        base_db = base_db + sector_gain_db(bearing, *sector)
    keep = base_db >= floor_db
    if terrain is not None:
        keep &= terrain.visible[cells]
    cells, base_db = cells[keep], base_db[keep]
    to_x, to_y = to_tx_x[cells], to_tx_y[cells]
    dot = grid.to_rx_x_km[cells] * to_x + grid.to_rx_y_km[cells] * to_y
    cos_beta = dot / (grid.to_rx_km[cells] * np.hypot(to_x, to_y) + 1e-9)
    keep = _within_cut(cos_beta, params.max_bistatic_angle_deg)
    return cells[keep], base_db[keep]


def _reach_counts(
    to_tx_x: np.ndarray,
    to_tx_y: np.ndarray,
    r_t_m: np.ndarray,
    threshold_db: np.ndarray,
    params: ScoringParams,
    grid: CellGrid,
    terrain: TowerTerrain | None,
    sector: tuple[float, float, float] | None,
) -> np.ndarray:
    """The per-azimuth counts of _evaluate_tower's detection test, on the cells that can pass it.

    The counts the whole grid gives: see _countable_cells, with the lowest threshold as the floor.
    """
    cells, base_db = _countable_cells(to_tx_x, to_tx_y, r_t_m, threshold_db.min(), params, grid, terrain, sector)
    detected = grid.pattern_db[:, cells]
    detected += base_db
    return np.add.reduce(detected >= threshold_db[:, None], axis=1, dtype=np.int64)


def expected_detection_area(
    tower_x_km: float,
    tower_y_km: float,
    eirp_dbm: float,
    freq_mhz: float,
    band: str,
    antenna_height_m: float,
    params: ScoringParams,
    grid: CellGrid | None = None,
    direct_power_dbm_override: float | None = None,
    terrain: TowerTerrain | None = None,
    *,
    processing: tuple[float, float, float] | None = None,
    sector: tuple[float, float, float] | None = None,
) -> TowerScore:
    """Detection area and best boresight for one tower, in km^2 and degrees.

    ``tower_x_km`` / ``tower_y_km`` are east/north offsets from the receiver.
    ``direct_power_dbm_override`` replaces the modelled direct-path power at the
    receiver *before* the antenna pattern is applied — a measured direct path
    (phase 2) says what the DPI really is, where the model only guesses. It
    changes the noise floor, not the target path: the energy that reaches the
    target is still the licensed EIRP. ``terrain``, on ``grid``'s cells, swaps
    the smooth earth for the tiles: only visible cells count, and its loss
    stands where ``horizon_loss_db`` would.

    ``processing`` is ``(bw_hz, cpi_s, reference_share_db)`` for a transmitter
    whose band has no single bandwidth (a cellular carrier): it replaces the
    band_params lookup, and the share scales the B*T gain, because only the
    always-on reference part of the waveform is integrated. ``sector`` is
    ``(azimuth_deg, beamwidth_deg, max_attenuation_db)`` of a sector antenna:
    its pattern weights the direct path towards the receiver and the
    illumination of every cell. Both None (a broadcast tower) is the model as
    it always was.
    """
    if grid is None:
        grid = cell_grid_for(params)
    return _evaluate_tower(
        tower_x_km,
        tower_y_km,
        eirp_dbm,
        freq_mhz,
        band,
        antenna_height_m,
        params,
        grid,
        direct_power_dbm_override,
        terrain,
        processing,
        sector,
    ).score


def _areas_and_ranges(
    cells_db: np.ndarray,
    countable: np.ndarray,
    thresholds_db: np.ndarray,
    grid: CellGrid,
    row: int,
    cells: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cell count, 50 % range and whether it crossed, per threshold, on one pattern row.

    The count takes every cell, the range only the cells in that row's beam: a
    node measures its range from the aircraft in its view, and over whole rings
    the Yagi's back lobe would end the walk early. A ring's share is its
    counted beam cells over all its beam cells, so a cell that is not countable
    reads as not detected, and a ring with no beam cell is skipped. The range
    is the outer edge of the last ring before the first one under half (0.0
    when there is none), or of the last ring when none falls under half.
    ``cells_db`` and ``countable`` are on ``cells`` (None: every cell); a cell
    left out reads as not detected, so only cells no threshold detects may be.
    """
    n_thr, n_rings = thresholds_db.size, grid.ring_outer_km.size
    order = np.argsort(thresholds_db, kind="stable")
    # A cell is counted at threshold t when cells_db >= t: with the thresholds
    # sorted, exactly the first `cleared` of them, ties included (side="right").
    cleared = np.searchsorted(thresholds_db[order], cells_db[countable], side="right")
    # One bin per ring, in the beam or not, and thresholds cleared.
    ring, in_beam = (grid.ring, grid.in_beam[row]) if cells is None else (grid.ring[cells], grid.in_beam[row][cells])
    slot = (ring[countable] * 2 + in_beam[countable]) * (n_thr + 1) + cleared
    by_slot = np.bincount(slot, minlength=n_rings * 2 * (n_thr + 1)).reshape(n_rings, 2, n_thr + 1)
    # Sorted threshold p counts the cells that cleared more than p of them.
    at_least = np.cumsum(by_slot[..., ::-1], axis=2)[..., ::-1][..., 1:]
    counts = np.empty(n_thr, dtype=np.int64)
    counts[order] = at_least.sum(axis=(0, 1))
    beam_hits = np.empty((n_thr, n_rings), dtype=np.int64)
    beam_hits[order] = at_least[:, 1, :].T

    beam_cells = grid.beam_ring_cells[row]
    held = beam_cells > 0
    under_half = held & (2 * beam_hits < beam_cells)  # integers, so exactly half is not under
    crossed = under_half.any(axis=1)
    first = np.argmax(under_half, axis=1)
    # Per ring, the outer edge of the last ring up to it that holds a beam
    # cell; the edges grow outward, so that is a running maximum.
    edge_km = np.maximum.accumulate(np.where(held, grid.ring_outer_km, 0.0))
    inner_edge_km = np.where(first > 0, edge_km[first - 1], 0.0)
    range_km = np.where(crossed, inner_edge_km, edge_km[-1])
    return counts, range_km, crossed


def class_detection_areas(
    tower_x_km: float,
    tower_y_km: float,
    eirp_dbm: float,
    freq_mhz: float,
    band: str,
    antenna_height_m: float,
    params: ScoringParams,
    class_offsets_db: Mapping[str, float],
    fleet: FleetPrediction | None = None,
    grid: CellGrid | None = None,
    direct_power_dbm_override: float | None = None,
    terrain: TowerTerrain | None = None,
) -> tuple[TowerScore, dict[str, dict]]:
    """The tower's score, and each aircraft class's area and 50 % range at its boresight.

    The score is exactly what ``expected_detection_area`` returns for the same
    arguments, ``terrain`` included; the classes are evaluated on that score's
    best azimuth only, over the same countable cells, and in the default mode
    with the same loss.
    ``class_offsets_db`` maps a class to its RCS offset in dB (narrowbody 0).

    Without ``fleet``, a class is the model's own threshold lowered by its
    offset, i.e. ``target_rcs_m2`` scaled by ``10**(offset/10)``; the low/high
    figures are None. With ``fleet``, the threshold is the calibration's
    ``T - S - offset - EIRP - 20 log10(lambda) - G_proc + 30 log10(4 pi)``,
    with S the predicted system constant (low and high at S minus and plus the
    prediction sigma) and EIRP ``fleet.eirp_dbm``, the one S is defined against.
    The ranking's ``eirp_dbm`` and band offset stay out of it: the first can be a
    placeholder power, and the calibration fits the band itself. S already
    holds the receiver gain, noise and direct-path residue, so none of those
    enter it either. Nor does the horizon loss, or the diffraction loss
    ``terrain`` puts in its place: retina measures S against a
    free-space link, so each node's own path to its tower, horizon included,
    is inside its S, and charging it here would count it twice. The ranking
    and the default mode still pay it.

    The areas count every cell. ``range_50pct_km`` walks the rings over the
    cells inside the beam only, within ``yagi_hpbw_deg / 2`` of the boresight,
    because retina measures a node's range from the aircraft in its view.
    ``range_50pct_bound`` is "crossed" when a ring under half detected ended
    the range, and "grid_edge" when the grid ran out first, so the range is a
    lower bound.

    ``fleet`` and ``terrain`` together count the fleet thresholds over the cells
    the tiles leave visible, at the tiles' boresight, for a comparison;
    score_towers counts a fleet record over the smooth earth's cells.
    """
    if grid is None:
        grid = cell_grid_for(params)
    model = _evaluate_tower(
        tower_x_km,
        tower_y_km,
        eirp_dbm,
        freq_mhz,
        band,
        antenna_height_m,
        params,
        grid,
        direct_power_dbm_override,
        terrain,
    )
    return model.score, _class_figures(model, grid, class_offsets_db, fleet, model.countable)


def _class_figures(
    model: _TowerModel,
    grid: CellGrid,
    class_offsets_db: Mapping[str, float],
    fleet: FleetPrediction | None,
    countable: np.ndarray | None,
) -> dict[str, dict]:
    """Each class's figures at the model's best row, over the ``countable`` cells (see class_detection_areas).

    Without ``fleet`` the thresholds are the model's own, its loss already in
    them; with it they are the calibration's, which charge no loss toward the
    tower.
    """
    names = list(class_offsets_db)
    n = len(names)

    if grid.x_km.size == 0:
        empty = None if fleet is None else 0.0
        return {
            name: {
                "area_km2": 0.0,
                "range_50pct_km": 0.0,
                "range_50pct_bound": "grid_edge",
                "area_km2_low": empty,
                "area_km2_high": empty,
                "range_50pct_km_low": empty,
                "range_50pct_km_high": empty,
            }
            for name in names
        }

    offsets_db = np.array([float(class_offsets_db[name]) for name in names], dtype=float)
    if fleet is None:
        thresholds_db = model.threshold_db - offsets_db
    else:
        s_pred, sigma = fleet.system_constant_db, fleet.prediction_sigma_db
        s_db = np.array([s_pred, s_pred - sigma, s_pred + sigma])  # mid, low, high
        thresholds_db = (
            fleet.threshold_db
            - s_db[:, None]
            - offsets_db[None, :]
            - fleet.eirp_dbm
            - 20 * math.log10(model.lam_m)
            - fleet.processing_gain_db
            + 30 * math.log10(4 * math.pi)
        ).ravel()

    pattern_db = grid.pattern_db[model.best] if model.cells is None else grid.pattern_db[model.best, model.cells]
    cells_db = pattern_db + model.base_db
    counts, range_km, crossed = _areas_and_ranges(cells_db, countable, thresholds_db, grid, model.best, model.cells)

    classes: dict[str, dict] = {}
    for i, name in enumerate(names):
        low = high = None
        if fleet is not None:
            low, high = n + i, 2 * n + i
        classes[name] = {
            "area_km2": float(counts[i] * grid.cell_area_km2),
            "range_50pct_km": float(range_km[i]),
            "range_50pct_bound": "crossed" if crossed[i] else "grid_edge",
            "area_km2_low": None if low is None else float(counts[low] * grid.cell_area_km2),
            "area_km2_high": None if high is None else float(counts[high] * grid.cell_area_km2),
            "range_50pct_km_low": None if low is None else float(range_km[low]),
            "range_50pct_km_high": None if high is None else float(range_km[high]),
        }
    return classes


def _antenna_height_m(value) -> float:
    """A usable mast height, whatever the upstream record carried.

    antennaHeight arrives absent, null, 0 or (rarely) negative. Any of those
    would put the tower past its own horizon and score it 0, which reads as
    "this tower is useless" rather than "we do not know how tall it is".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_ANTENNA_HEIGHT_M
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_ANTENNA_HEIGHT_M
    return float(value)


# One tile reader per folder for the life of the process, so the blocks one
# request decodes are still cached for the next. Dem guards its own caches.
_DEMS: dict[Path, Dem] = {}
_DEMS_LOCK = threading.Lock()


def _terrain_source() -> Dem:
    """The shared reader for the tile folder dem_dir() names at this moment."""
    root = dem_dir().absolute()
    with _DEMS_LOCK:
        if root not in _DEMS:
            _DEMS[root] = Dem(root)
        return _DEMS[root]


def _receiver_view(horizon: Horizon, params: ScoringParams, grid: CellGrid) -> np.ndarray:
    """Per cell, whether a target there clears the receiver's terrain horizon.

    The target flies ``target_alt_km`` above the receiver's reference surface,
    the plane R_r and R_t put it over, which the horizon takes in metres above
    sea level.
    """
    target_alt_m = horizon.rx_ground_m + params.target_alt_km * 1000.0
    visible = horizon.visible(grid.bearing_deg, np.hypot(grid.x_km, grid.y_km), target_alt_m)
    visible.setflags(write=False)
    return visible


def _stated_m(value) -> float | None:
    """A height a record states: a positive, finite number of metres; None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _tower_end(tower: dict, height_m: float) -> TowerEnd:
    """The tower's end of its terrain profile, from the radiation centre and polarisation of its private ``_antenna``.

    The licence's RCAMSL where it states one; else its RCAGL above the tiles'
    height at the tower; else the mast height the smooth earth uses, above
    that same height. HAAT, the usual antenna_height_m, is a height above the
    average terrain around the tower, not above its own ground, and is only
    the last resort here. Circular, elliptical or unstated polarisation counts
    as horizontal.
    """
    antenna = tower.get("_antenna") or {}
    rcagl_m = _stated_m(antenna.get("rcagl_m"))
    return TowerEnd(
        lat=float(tower["latitude"]),
        lon=float(tower["longitude"]),
        freq_mhz=float(tower["frequency_mhz"]),
        altitude_m=_stated_m(antenna.get("rcamsl_m")),
        height_m=height_m if rcagl_m is None else rcagl_m,
        vertical=antenna.get("polarisation") == "vertical",
    )


class _Scored(NamedTuple):
    """A tower the model can score, and the grid and carrier terms it is scored with."""

    tower: dict
    height_m: float
    grid: CellGrid
    processing: tuple[float, float, float] | None  # a carrier's (see expected_detection_area); None: broadcast
    sector: tuple[float, float, float] | None


def _query_terrain(
    rx_lat: float, rx_lon: float, scored: list[_Scored], params: ScoringParams, reach_km: float
) -> list[TowerTerrain] | None:
    """The terrain of each tower in ``scored``, on its own grid, in order; None to score them all on smooth earth.

    Terrain for one tower means terrain for all of them, and it needs every
    tile under the receiver's square, rx +- ``reach_km`` (the range of the
    widest grid scored), and under the path to each tower, on disk or marked
    as sea, plus a height at every point the horizon and the profiles read.
    Absent tiles are found before a height is read and named in one INFO line.
    Any failure to read or work out the terrain is one WARNING naming it. The
    horizon and the profiles share the receiver's reference surface and the
    near zone ``terrain_near_km``; the horizon is read out to ``reach_km`` and
    masks the cells of each grid scored.
    """
    try:
        dem = _terrain_source()
        reach_lat = reach_km / KM_PER_DEG
        reach_lon = reach_km / (KM_PER_DEG * max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT))
        absent = set(dem.missing(rx_lat - reach_lat, rx_lat + reach_lat, rx_lon - reach_lon, rx_lon + reach_lon))
        ends = [(float(item.tower["latitude"]), float(item.tower["longitude"])) for item in scored]
        absent.update(dem.missing_along(rx_lat, rx_lon, ends))
        if absent:
            logger.info("Terrain tiles not on disk, scoring every tower on smooth earth: %s", ", ".join(sorted(absent)))
            return None
        near_km = params.terrain_near_km
        horizon = rx_horizon(dem, rx_lat, rx_lon, params.rx_height_m, reach_km, near_km=near_km)
        losses = None
        if horizon is not None:
            towers = [_tower_end(item.tower, item.height_m) for item in scored]
            losses = diffraction_losses_db(
                dem, rx_lat, rx_lon, horizon.rx_ground_m, params.rx_height_m, towers, near_km=near_km
            )
        if losses is None:
            logger.info("Terrain tiles hold no height where the query reads them, scoring every tower on smooth earth")
            return None
        # One view per grid: the broadcast towers share one, the carriers another.
        views: dict[int, np.ndarray] = {}
        for item in scored:
            if id(item.grid) not in views:
                views[id(item.grid)] = _receiver_view(horizon, params, item.grid)
        return [TowerTerrain(views[id(item.grid)], loss_db) for item, loss_db in zip(scored, losses, strict=True)]
    except Exception as exc:
        logger.warning("Terrain unreadable, scoring every tower on smooth earth: %s", exc)
        return None


def tower_area_bounds(
    towers: list[dict],
    rx_lat: float,
    rx_lon: float,
    params: ScoringParams,
    cellular: CellularScoring,
    *,
    terrain: bool = False,
    block: int = 0,
) -> np.ndarray:
    """carrier_area_bounds of cellular tower rows, from the fields score_towers reads of them."""
    cos_lat = max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT)
    lat = np.array([float(t["latitude"]) for t in towers])
    lon = np.array([float(t["longitude"]) for t in towers])
    bandwidth = np.array([float(t["bandwidth_mhz"]) for t in towers])
    share = []
    for t in towers:
        radio = t.get("radio")
        if radio is None and float(t["bandwidth_mhz"]) > _LTE_MAX_BANDWIDTH_MHZ:
            radio = "NR"
        share.append(cellular.reference_share_db.get(radio, cellular.unknown_radio_share_db))
    azimuth = [t.get("azimuth_deg") for t in towers]
    return carrier_area_bounds(
        ((lon - rx_lon + 180.0) % 360.0 - 180.0) * KM_PER_DEG * cos_lat,
        (lat - rx_lat) * KM_PER_DEG,
        np.array([float(t["eirp_dbm"]) for t in towers]),
        np.array([float(t["frequency_mhz"]) for t in towers]),
        bandwidth * 1e6,
        np.array(share),
        np.array([_antenna_height_m(t.get("antenna_height_m")) for t in towers]),
        np.array([np.nan if a is None else float(a) for a in azimuth]),
        np.array([_positive_number(t.get("beamwidth_deg")) or cellular.beamwidth_deg for t in towers]),
        np.array([float(params.band_offset_db.get(t.get("band") or "", 0.0)) for t in towers]),
        params,
        cellular,
        terrain=terrain,
        position_km=1e-9,
        rel=1e-12,
        block=block,
    )


def receiver_square_lacks_tiles(rx_lat: float, rx_lon: float, reach_km: float) -> bool:
    """Whether _query_terrain is bound to score a query at (rx_lat, rx_lon) of reach ``reach_km`` on the smooth earth.

    True when a tile under the receiver's square is neither on disk nor marked
    as sea, or the tile folder cannot be looked at: _query_terrain answers None
    for those before it reads anything about a tower. False says only that
    the towers' paths decide.
    """
    try:
        dem = _terrain_source()
        reach_lat = reach_km / KM_PER_DEG
        reach_lon = reach_km / (KM_PER_DEG * max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT))
        return bool(dem.missing(rx_lat - reach_lat, rx_lat + reach_lat, rx_lon - reach_lon, rx_lon + reach_lon))
    except Exception:
        return True


def _positive_number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _carrier_terms(
    tower: dict, cellular: CellularScoring
) -> tuple[tuple[float, float, float], tuple[float, float, float] | None] | None:
    """A cellular carrier's ``(processing, sector)`` for the model; None when it cannot be scored.

    Scoreable takes a finite EIRP and a positive frequency and bandwidth. The
    carrier is processed with its own row's ``bandwidth_mhz`` and the reference
    share of its ``radio`` (a row with no radio wider than any LTE channel can
    only be NR), and weighted by its sector pattern when its ``azimuth_deg`` is
    known, omni otherwise.
    """
    eirp = tower.get("eirp_dbm")
    bw_mhz = _positive_number(tower.get("bandwidth_mhz"))
    if not isinstance(eirp, (int, float)) or isinstance(eirp, bool) or not math.isfinite(eirp):
        return None
    if _positive_number(tower.get("frequency_mhz")) is None or bw_mhz is None:
        return None

    radio = tower.get("radio")
    if radio is None and bw_mhz > _LTE_MAX_BANDWIDTH_MHZ:
        radio = "NR"
    share_db = cellular.reference_share_db.get(radio, cellular.unknown_radio_share_db)
    azimuth = tower.get("azimuth_deg")
    sector = None
    if isinstance(azimuth, (int, float)) and not isinstance(azimuth, bool) and math.isfinite(azimuth):
        beamwidth = _positive_number(tower.get("beamwidth_deg")) or cellular.beamwidth_deg
        sector = (float(azimuth), beamwidth, cellular.max_attenuation_db)
    return (bw_mhz * 1e6, cellular.cpi_s, share_db), sector


def score_towers(
    towers: list[dict],
    rx_lat: float,
    rx_lon: float,
    params: ScoringParams,
    calibration: Mapping | None = None,
    rx_descriptors: Mapping[str, object] | None = None,
    cellular: CellularScoring | None = None,
    *,
    smooth_earth: bool = False,
    records: bool = True,
) -> None:
    """Stamp ``expected_area_km2``, ``best_azimuth_deg``, ``horizon_km`` and ``expected_detection_area`` in place.

    Every tower gets all four keys, always: the fields are part of the
    response contract, and a tower the model cannot score (unknown band,
    no EIRP) carries 0.0 and a null ``expected_detection_area`` rather than a
    missing key the sort would then have to guess at. They are written before
    anything can raise for the same reason.

    ``expected_detection_area`` is the per-class record: each aircraft class's
    area and 50 % range at the boresight the ranking chose. Without a
    ``calibration`` it uses the model's own constants (``"default"``); with the
    fleet calibration document it predicts the system constant from the tower's
    ``_fleet_descriptors`` and ``rx_descriptors`` (``"fleet"``), referenced to
    their licensed ``tower_eirp_dbm``, or to the tower's ``eirp_dbm`` without
    the band offset where the licence states none. Either way the
    calibration never touches the three ranking fields. If the per-class
    evaluation fails for a tower, that tower falls back to the plain score and a
    null record.

    A per-tower ``direct_power_dbm_override`` is honoured if present — phase 2
    will set it from a measured direct path.

    Terrain comes from the GLO-30 tiles in ``dem_dir()``, for the whole query
    or not at all, so one answer never ranks towers on two models. It is used
    only when every tile under the receiver's square (rx +- max_range_km) and
    under the path to every scoreable tower is on disk or marked as sea, and
    the tiles hold a height at every point read. Then the receiver's horizon
    is worked out once, and each tower's diffraction loss over its own path,
    from the radiation centre and in the polarisation of its private
    ``_antenna`` (``rcamsl_m``, ``rcagl_m``, ``polarisation``, from the licence
    where it states them); the ranking fields and the default record are
    scored on them and the record says ``"terrain": "dem"``. Otherwise every
    tower is scored exactly as without tiles and says ``"smooth_earth"``: the
    absent tiles are named in one INFO line, a tile that cannot be read in one
    WARNING. A fleet record charges no horizon or terrain loss and hides no
    cell behind terrain; it is taken at the boresight the ranking chose and
    says ``"smooth_earth"``: the calibration's system constants were fitted with
    a free-space path and no terrain term, so each node's own path, horizon and
    terrain included, is already in them and the tiles would charge it twice.

    With ``cellular``, a tower whose ``kind`` is ``"cellular"`` is a carrier
    (see _carrier_terms), scored on the grid ``cellular`` sets. It takes part
    in the one terrain decision with every other tower; for a query of
    carriers alone the receiver's square is rx +- that grid's range. Its
    antenna stands ``antenna_height_m`` above the tiles' height at it, in
    horizontal polarisation. Its record is always ``"default"``, with the
    default class offsets, whatever ``calibration`` holds: the fleet's system
    constants, threshold and class offsets were fitted on broadcast towers.

    ``smooth_earth`` scores on the smooth earth without looking for tiles: for
    a caller that made the query's one terrain decision itself and scores its
    towers in parts. ``records=False`` leaves ``expected_detection_area`` None
    and the three ranking fields as they would be with it.
    """
    for tower in towers:
        tower["expected_area_km2"] = 0.0
        tower["best_azimuth_deg"] = 0.0
        tower["horizon_km"] = 0.0
        tower["expected_detection_area"] = None

    if not towers:
        return

    grid = cell_grid_for(params)
    carrier_grid = None
    cos_lat = max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT)
    # The document's own class offsets, which only a fleet record uses.
    offsets = None
    if calibration is not None:
        offsets = {name: float(calibration["class_offsets_db"][name]) for name in CLASS_NAMES}
    warned = False

    scored: list[_Scored] = []
    for tower in towers:
        height_m = _antenna_height_m(tower.get("antenna_height_m"))
        tower["horizon_km"] = round(radio_horizon_km(height_m, params.rx_height_m), 1)

        if cellular is not None and tower.get("kind") == "cellular":
            terms = _carrier_terms(tower, cellular)
            if terms is None:
                continue
            if carrier_grid is None:
                # The carriers' own grid, through the same cache as the broadcast one.
                carrier_grid = _build_cell_grid(
                    float(cellular.grid_km),
                    float(cellular.max_range_km),
                    float(params.target_alt_km),
                    int(params.n_azimuths),
                    float(params.yagi_hpbw_deg),
                    float(params.yagi_front_to_back_db),
                )
            scored.append(_Scored(tower, height_m, carrier_grid, *terms))
            continue

        band = tower.get("band")
        eirp = tower.get("eirp_dbm")
        freq_mhz = tower.get("frequency_mhz")
        # An unknown band has no bandwidth or CPI to process with, and a
        # missing EIRP nothing to propagate: neither is scoreable, and a
        # guessed number here would rank a tower we know nothing about.
        if band not in params.band_params:
            continue
        if not isinstance(eirp, (int, float)) or isinstance(eirp, bool) or not math.isfinite(eirp):
            continue
        if not isinstance(freq_mhz, (int, float)) or isinstance(freq_mhz, bool) or not freq_mhz > 0:
            continue
        scored.append(_Scored(tower, height_m, grid, None, None))

    terrains = None
    if scored and not smooth_earth:
        # The receiver's square has to cover the widest grid scored.
        reach_km = max(params.max_range_km if item.processing is None else cellular.max_range_km for item in scored)
        terrains = _query_terrain(rx_lat, rx_lon, scored, params, reach_km)

    for i, item in enumerate(scored):
        tower, height_m = item.tower, item.height_m
        carrier = item.processing is not None
        eirp = tower["eirp_dbm"]
        terrain = None if terrains is None else terrains[i]
        # Wrapped, not a bare subtraction: a receiver just east of the
        # antimeridian and a tower just west of it are 20 km apart, not 40000.
        dlon = (float(tower["longitude"]) - rx_lon + 180.0) % 360.0 - 180.0
        x_km = dlon * KM_PER_DEG * cos_lat
        y_km = (float(tower["latitude"]) - rx_lat) * KM_PER_DEG

        band = (tower.get("band") or "") if carrier else tower["band"]
        args = (x_km, y_km, float(eirp), float(tower["frequency_mhz"]), band, height_m, params)
        direct_power_dbm_override = tower.get("direct_power_dbm_override")
        if not records:
            score = _evaluate_tower(
                *args, item.grid, direct_power_dbm_override, terrain, item.processing, item.sector, score_only=True
            ).score
            tower["expected_area_km2"] = score.expected_area_km2
            tower["best_azimuth_deg"] = score.best_azimuth_deg
            tower["horizon_km"] = score.horizon_km
            continue
        try:
            fleet = None
            # Never for a carrier: the document was fitted on broadcast towers.
            if calibration is not None and not carrier:
                descriptors = {**(rx_descriptors or {}), **(tower.get("_fleet_descriptors") or {})}
                mean_db, sigma_db = evaluate(calibration, descriptors)
                # The measured S was computed with the licensed EIRP, which the
                # ranking's EIRP need not be (it can be a 50/60 dBm stand-in).
                licensed_dbm = descriptors.get("tower_eirp_dbm")
                if (
                    isinstance(licensed_dbm, bool)
                    or not isinstance(licensed_dbm, (int, float))
                    or not math.isfinite(licensed_dbm)
                ):
                    licensed_dbm = eirp
                fleet = FleetPrediction(
                    mean_db,
                    sigma_db,
                    float(calibration["threshold_db"]),
                    float(calibration["processing_gain_db"]),
                    float(licensed_dbm),
                )
            # A default record's class thresholds all lie at or above the best row's less the largest offset.
            below_db = None if fleet is not None else max(0.0, *DEFAULT_CLASS_OFFSETS_DB.values())
            model = _evaluate_tower(
                *args, item.grid, direct_power_dbm_override, terrain, item.processing, item.sector, below_db=below_db
            )
            score = model.score
            if fleet is None:
                # The default mode keeps every default constant, the class offsets included.
                classes = _class_figures(model, item.grid, DEFAULT_CLASS_OFFSETS_DB, None, model.countable)
            else:
                # The smooth earth's cells at the ranking's boresight, which is the no-tile
                # record wherever the tiles leave that boresight where it was.
                classes = _class_figures(model, item.grid, offsets, fleet, model.resolvable)
            record = {
                "calibration": "default" if fleet is None else "fleet",
                "system_constant_db": None if fleet is None else fleet.system_constant_db,
                "tau_db": None if fleet is None else float(calibration["tau_db"]),
                "altitude_km": float(params.target_alt_km),
                "azimuth_deg": score.best_azimuth_deg,
                "terrain": "dem" if terrain is not None and fleet is None else "smooth_earth",
                "classes": classes,
            }
        except Exception as exc:
            if not warned:
                logger.warning("Per-class detection area failed, scoring without it: %s", exc)
                warned = True
            score = expected_detection_area(
                *args,
                grid=item.grid,
                direct_power_dbm_override=direct_power_dbm_override,
                terrain=terrain,
                processing=item.processing,
                sector=item.sector,
            )
            record = None
        tower["expected_area_km2"] = score.expected_area_km2
        tower["best_azimuth_deg"] = score.best_azimuth_deg
        tower["horizon_km"] = score.horizon_km
        tower["expected_detection_area"] = record


# ── Upper bounds on a carrier's area, without its grid ───────────────────────
#
# A cellular search has hundreds of thousands of carriers in its radius and
# scores the few that can reach its answer (tower_ranking). Which ones is
# decided on an upper bound of each carrier's expected_area_km2, worked out for
# all of them at once from a table, never below what _evaluate_tower returns:
#
#   * a cell counts at azimuth a when pattern + base >= threshold(a), and
#     base = -20 log10(R_t) - 20 log10(R_r) + the cell-side sector gain. That
#     gain is at most 0 dB and is left out; R_t is at least the slant range
#     over |d - g| of ground (triangle inequality: d the tower's distance from
#     the receiver, g the cell's), so for a tower within a band of distances
#     every cell's base has a ceiling that does not depend on where around the
#     receiver the tower stands. The bistatic-angle cut and terrain only ever
#     remove cells, and are left out too.
#   * Per distance band and azimuth, the table counts the cells whose ceiling
#     reaches each threshold on a 0.1 dB lattice. A carrier's count at azimuth a
#     is at most the table's at the lattice step at or below its threshold.
#   * The cell-side sector gain is bounded instead of dropped: per band and
#     threshold the table also keeps the farthest a cell can be from the
#     receiver and still reach it, and every cell that counts lies inside that
#     disk, so inside a known spread of bearings around the receiver as seen
#     from the tower. The most the sector gives over that spread lowers every
#     threshold of the carrier.
#   * The threshold is taken low: the EIRP high (area does not fall as EIRP
#     rises: the DPI rises with the target signal and never faster), the
#     horizon or terrain loss low (smooth-earth loss at the near end of the
#     distance; 0 under terrain, whose diffraction loss is never negative), the
#     free-space loss to the tower high, the direct-path sector gain and Yagi
#     response low (a sector facing away lowers the DPI, which RAISES the area,
#     so the gain is bounded, not dropped), the bandwidth high (the threshold
#     falls with it: noise rises as B, gain as B*T), the radar constant at the
#     low end of the frequency's rounding. Margins cover the rounded tower
#     fields score_towers reads and float32 inputs.

_BOUND_BAND_STARTS_KM = tuple(np.concatenate([np.arange(0, 10, 0.25), np.arange(10, 30, 0.5), np.arange(30, 81, 1.0)]))
_BOUND_LATTICE_DB = (-240.0, 0.1, 1100)  # first threshold, step, count: up to -130 dB, past any cell's ceiling
_BOUND_SLACK_DB = 1e-6  # float slack between numpy here and _evaluate_tower, far above its ulp drift


@lru_cache(maxsize=4)
def _bound_table(
    grid_km: float,
    max_range_km: float,
    target_alt_km: float,
    n_azimuths: int,
    yagi_hpbw_deg: float,
    yagi_front_to_back_db: float,
) -> tuple[np.ndarray, np.ndarray]:
    """(distance band, azimuth, lattice step) -> how many cells' base ceiling reaches that threshold;
    and (distance band, lattice step) -> the farthest such cell from the receiver at any azimuth, in km.

    Band b holds towers from _BOUND_BAND_STARTS_KM[b] up to the next start; the
    last runs on without end, which holds because the grid ends inside it.
    """
    grid = _build_cell_grid(grid_km, max_range_km, target_alt_km, n_azimuths, yagi_hpbw_deg, yagi_front_to_back_db)
    start, step, count = _BOUND_LATTICE_DB
    lattice = start + step * np.arange(count)
    g_km = np.hypot(grid.x_km, grid.y_km)
    starts = np.asarray(_BOUND_BAND_STARTS_KM)
    ends = np.append(starts[1:], np.inf)
    table = np.empty((starts.size, n_azimuths, count), dtype=np.int16)
    reach = np.empty((starts.size, count))
    head_db = grid.pattern_db - grid.r_r_db[None, :]
    for b, (low, high) in enumerate(zip(starts, ends)):
        gap_km = np.maximum(0.0, np.maximum(low - g_km, g_km - high))
        r_t_m = np.maximum(np.sqrt(gap_km * gap_km + target_alt_km**2) * 1000.0 * (1 - 1e-12), _MIN_RANGE_M)
        ceiling = head_db - 20 * np.log10(r_t_m)[None, :]
        best = ceiling.max(axis=0)
        by_best = np.argsort(best)
        # Farthest cell among those whose best ceiling is at least the threshold.
        farthest = np.maximum.accumulate(g_km[by_best][::-1])[::-1]
        first = np.searchsorted(best[by_best], lattice - _BOUND_SLACK_DB, side="left")
        reach[b] = np.where(first < g_km.size, farthest[np.minimum(first, g_km.size - 1)], 0.0)
        ceiling.sort(axis=1)
        for a in range(n_azimuths):
            table[b, a] = g_km.size - np.searchsorted(ceiling[a], lattice - _BOUND_SLACK_DB, side="left")
    table.setflags(write=False)
    reach.setflags(write=False)
    return table, reach


@lru_cache(maxsize=8)
def _bound_blocks(
    grid_km: float,
    max_range_km: float,
    target_alt_km: float,
    n_azimuths: int,
    yagi_hpbw_deg: float,
    yagi_front_to_back_db: float,
    block: int,
) -> tuple[tuple[np.ndarray, ...], tuple[np.ndarray, ...]]:
    """The grid's cells in blocks of ``block`` x ``block``: per block its cell count, the box of its
    cell centres (x_min, x_max, y_min, y_max), per block and azimuth the largest pattern - 20 log10 R_r
    of its cells, the largest of those over the azimuths, 10^(that / 10) widened by 1e-6, the box's
    centre and its half-diagonal; and the same for squares of 4 x 4 blocks, with the blocks of each
    square (``order[starts[s] : starts[s] + sizes[s]]``) and a radius around its centre that holds
    every block's half-diagonal circle."""
    grid = _build_cell_grid(grid_km, max_range_km, target_alt_km, n_azimuths, yagi_hpbw_deg, yagi_front_to_back_db)
    side = block * grid_km
    bx = np.floor((grid.x_km + max_range_km) / side + 1e-9).astype(np.int64)
    by = np.floor((grid.y_km + max_range_km) / side + 1e-9).astype(np.int64)
    ids, block = np.unique(bx * 100_000 + by, return_inverse=True)
    square = (ids // 100_000 // 4) * 100_000 + ids % 100_000 // 4
    n = ids.size
    cells = np.bincount(block, minlength=n)
    box = np.full((4, n), np.nan)
    box[0], box[2] = np.inf, np.inf
    box[1], box[3] = -np.inf, -np.inf
    np.minimum.at(box[0], block, grid.x_km)
    np.maximum.at(box[1], block, grid.x_km)
    np.minimum.at(box[2], block, grid.y_km)
    np.maximum.at(box[3], block, grid.y_km)
    head = np.full((n_azimuths, n), -np.inf)
    for a in range(n_azimuths):
        np.maximum.at(head[a], block, grid.pattern_db[a] - grid.r_r_db)
    head = np.ascontiguousarray(head.T)
    head_max = head.max(axis=1)
    room = 10 ** (head_max / 10) * (1 + 1e-6)
    centre = np.stack([(box[0] + box[1]) / 2, (box[2] + box[3]) / 2])
    half = np.hypot(box[1] - box[0], box[3] - box[2]) / 2
    blocks = (cells, box, head, head_max, room, centre, half)

    _, square = np.unique(square, return_inverse=True)
    order = np.argsort(square, kind="stable")
    sizes = np.bincount(square)
    starts = np.r_[0, np.cumsum(sizes)[:-1]]
    s_box = np.stack(
        [
            np.minimum.reduceat(box[0][order], starts),
            np.maximum.reduceat(box[1][order], starts),
            np.minimum.reduceat(box[2][order], starts),
            np.maximum.reduceat(box[3][order], starts),
        ]
    )
    s_centre = np.stack([(s_box[0] + s_box[1]) / 2, (s_box[2] + s_box[3]) / 2])
    apart = np.hypot(centre[0] - s_centre[0][square], centre[1] - s_centre[1][square]) + half
    s_half = np.maximum.reduceat(apart[order], starts) * (1 + 1e-9) + 1e-9
    s_head_max = np.maximum.reduceat(head_max[order], starts)
    s_room = np.maximum.reduceat(room[order], starts)
    squares = (s_box, s_head_max, s_room, s_centre, s_half, order, starts, sizes)
    for arr in (*blocks, *squares):
        arr.setflags(write=False)
    return blocks, squares


def _block_sector_db(
    to_x: np.ndarray,
    to_y: np.ndarray,
    radius_km: np.ndarray,
    azimuth_deg: np.ndarray,
    width_deg: np.ndarray,
    rel: float,
    pad_deg: float,
    max_attenuation_db: float,
) -> np.ndarray:
    """The most a sector gives toward any point within ``radius_km`` of (to_x, to_y) from it; 0 for an omni.

    Every such point lies within asin(radius / range) of the bearing to the
    centre, seen from the tower; ``pad_deg`` more is taken off for float error.
    """
    reach = np.hypot(to_x, to_y)
    spread = np.arcsin(np.minimum(radius_km / np.maximum(reach, 1e-12), 1.0))
    spread = np.where(reach > radius_km, np.degrees(spread), 180.0)
    aim = np.degrees(np.arctan2(to_x, to_y)) % 360.0
    off = np.abs((aim - azimuth_deg + 180.0) % 360.0 - 180.0)
    off = np.maximum(off - spread - 360.0 * rel - pad_deg, 0.0)
    gain = -np.minimum(12.0 * (off / width_deg) ** 2, max_attenuation_db)
    return np.where(np.isnan(azimuth_deg), 0.0, gain)


_LENS_MARGIN_DEG = 0.1  # half for the tower's position slack, half (far more than needed) for float error


def _bistatic_lens(
    x_km: np.ndarray, y_km: np.ndarray, max_angle_deg: float, position_km: float
) -> tuple[np.ndarray, ...] | None:
    """Two disks per carrier whose common part only holds points past the bistatic-angle cut.

    With the receiver at the origin and the tower at (x, y), the points that see
    the baseline (length L) under at least b > 90 degrees fill a lens: the common
    part of the two disks of radius L / (2 sin b) centred L |cos b| / (2 sin b)
    either side of the baseline's midpoint, whose rims meet at both ends. b is
    the cut plus _LENS_MARGIN_DEG. Seen from a point at least ``near_km`` from
    the tower, a tower that moves ``position_km`` turns by at most half the
    margin; the other half is far above the float error of the model's angle,
    and the 10 m more in ``near_km`` keeps the 1e-9 in its cosine's denominator
    from moving it anywhere near that (2e-5 degrees for cells 0.5 km across).
    Returns (c1x, c1y, c2x, c2y, radius^2, near_km), each per carrier; None
    when the cut leaves no lens.
    """
    b = math.radians(max_angle_deg + _LENS_MARGIN_DEG)
    if not math.pi / 2 < b < math.pi:
        return None
    length = np.hypot(x_km, y_km)
    radius = length / (2 * math.sin(b))
    offset = radius * -math.cos(b) / np.maximum(length, 1e-12)  # along the unit normal (-y, x) / L
    mx, my = x_km / 2, y_km / 2
    radius = np.maximum(radius * (1 - 1e-9) - 1e-9, 0.0)
    near_km = position_km / math.sin(math.radians(_LENS_MARGIN_DEG / 2)) + 0.01
    return (
        mx - y_km * offset,
        my + x_km * offset,
        mx + y_km * offset,
        my - x_km * offset,
        radius * radius,
        np.full(length.shape, near_km),
    )


def _boxes_in_lens(lens: tuple[np.ndarray, ...], rows: np.ndarray, box: np.ndarray, ground2: np.ndarray) -> np.ndarray:
    """Per (carrier, box) pair: whether the box lies in the carrier's lens (see _bistatic_lens).

    ``box`` is (x_min, x_max, y_min, y_max) per pair and ``ground2`` a lower
    bound on the squared distance from the carrier to the box: a box is in the
    lens when it is at least ``near_km`` from the carrier and its farthest
    corner from either disk's centre lies within the radius.
    """
    c1x, c1y, c2x, c2y, radius2, near_km = lens
    inside = ground2 >= near_km[rows] ** 2
    for cx, cy in ((c1x[rows], c1y[rows]), (c2x[rows], c2y[rows])):
        fx = np.maximum(np.abs(cx - box[0]), np.abs(cx - box[1]))
        fy = np.maximum(np.abs(cy - box[2]), np.abs(cy - box[3]))
        inside &= fx * fx + fy * fy <= radius2[rows]
    return inside


def carrier_area_bounds(
    x_km: np.ndarray,
    y_km: np.ndarray,
    eirp_dbm: np.ndarray,
    freq_mhz: np.ndarray,
    bw_hz: np.ndarray,
    share_db: np.ndarray,
    height_m: np.ndarray,
    azimuth_deg: np.ndarray,
    beamwidth_deg: np.ndarray,
    band_offset_db: np.ndarray,
    params: ScoringParams,
    cellular: CellularScoring,
    *,
    terrain: bool,
    position_km: float,
    rel: float,
    block: int = 0,
) -> np.ndarray:
    """An upper bound on each carrier's expected_area_km2, as score_towers would stamp it.

    Arrays, one entry per carrier, of the values score_towers reads for it
    before rounding: offsets from the receiver on its tangent plane, EIRP,
    frequency, bandwidth in Hz, the radio's reference share (see
    _carrier_terms), mast height, sector azimuth (NaN: omni) and beamwidth.
    ``position_km`` is how far the position score_towers reads can be from
    (x, y), ``rel`` the relative error of the float32 inputs; EIRP, frequency
    and position are widened by the tower row's rounding (0.1 dB, 0.001 MHz)
    and these. ``terrain`` bounds for a query scored on the tiles, else on the
    smooth earth. See the block comment above for why each term is safe.

    ``block`` > 0 takes the real geometry instead of the distance-band table,
    on blocks of ``block`` x ``block`` cells: a block counts all its cells at an
    azimuth when its largest pattern - 20 log10 R_r, less 20 log10 of the slant
    range to the nearest point of the box around its cell centres, plus the
    most the sector can give toward any point of that box, reaches the
    threshold; a block whose box lies in the carrier's bistatic lens, where
    every point is past the bistatic-angle cut (_bistatic_lens), counts for
    nothing. Tens of microseconds a carrier, against the table's one, and
    much tighter; smaller blocks are tighter and dearer.
    """
    grid_km, max_range_km = float(cellular.grid_km), float(cellular.max_range_km)
    geometry = (grid_km, max_range_km, float(params.target_alt_km), int(params.n_azimuths))
    geometry += (float(params.yagi_hpbw_deg), float(params.yagi_front_to_back_db))
    grid = _build_cell_grid(*geometry)

    d_km = np.hypot(x_km, y_km)
    d_lo = np.maximum(d_km - position_km, 0.0)
    d_hi = d_km + position_km
    eirp_hi = eirp_dbm + band_offset_db + 0.05 + rel * np.abs(eirp_dbm) + 1e-9
    f_lo = (freq_mhz - 5e-4) * (1 - 1e-12)
    f_hi = (freq_mhz + 5e-4) * (1 + 1e-12)
    bw_hi = bw_hz * (1 + 1e-12)
    h_hi = np.maximum(height_m * (1 + rel), 1.0)

    lam_hi = C_M_S / (f_lo * 1e6)
    const_hi = 10 * np.log10(lam_hi**2 * params.target_rcs_m2 / (4 * math.pi) ** 3)
    noise_dbm = -174.0 + 10 * np.log10(bw_hi) + params.noise_figure_db
    gproc_db = 10 * np.log10(bw_hi * cellular.cpi_s) + share_db
    if terrain:
        hloss_lo = np.zeros_like(d_km)
    else:
        over = d_lo - 3.57 * (np.sqrt(h_hi) + math.sqrt(max(params.rx_height_m, 1.0)))
        hloss_lo = np.where(over > 0, 20.0 + 0.5 * over, 0.0)
    fspl_hi = 20 * np.log10(np.maximum(d_hi, _MIN_DIRECT_KM) * 1000) + 20 * np.log10(f_hi * 1e6) - 147.55

    # Bearings move with the rounding of the position: by at most this much.
    slack_deg = np.minimum(np.degrees(2 * position_km / np.maximum(d_lo, 1e-9)) + 1e-6, 180.0)
    tower_bearing = np.degrees(np.arctan2(x_km, y_km)) % 360.0
    off_deg = np.abs(((tower_bearing + 180.0) % 360.0 - azimuth_deg + 180.0) % 360.0 - 180.0)
    off_deg = np.minimum(off_deg + slack_deg + 360.0 * rel, 180.0)
    width = beamwidth_deg * (1 - rel)
    sector_lo = np.where(
        np.isnan(azimuth_deg), 0.0, -np.minimum(12.0 * (off_deg / width) ** 2, cellular.max_attenuation_db)
    )
    direct_lo = eirp_hi - fspl_hi - hloss_lo + sector_lo

    yagi_off = np.abs((tower_bearing[:, None] - grid.azimuths_deg[None, :] + 180.0) % 360.0 - 180.0)
    yagi_lo = _yagi_pattern_db(
        np.minimum(yagi_off + slack_deg[:, None], 180.0), params.yagi_hpbw_deg, params.yagi_front_to_back_db
    )
    dpi_dbm = direct_lo[:, None] + params.rx_gain_dbi + yagi_lo - params.cancellation_db
    n_eff_dbm = 10 * np.log10(10 ** (noise_dbm[:, None] / 10) + 10 ** (dpi_dbm / 10))
    fixed = hloss_lo - eirp_hi - params.rx_gain_dbi - const_hi - gproc_db + params.snr_min_db
    threshold_db = n_eff_dbm + fixed[:, None] - _BOUND_SLACK_DB

    if block:
        (cells, box, head, head_max, room, centre, half), squares = _bound_blocks(*geometry, block)
        s_box, s_head_max, s_room, s_centre, s_half, order, starts, sizes = squares
        # Only the (carrier, block) pairs that can count at some azimuth go on:
        # with the sector at most 0 dB, a block reaches a threshold only if its
        # largest pattern - 20 log10 R_r over the azimuths, less 20 log10 R_t,
        # reaches the carrier's lowest one, i.e. R_t^2 <= 10^((head_max - lowest) / 10).
        # Taken 1e-6 wide (4e-6 dB, far above the float error of either side, and
        # of the 1 - 1e-12 below), so a pair dropped here fails the test below at
        # every azimuth, and the counts are the ones every pair gives.
        lowest = np.where(np.isnan(threshold_db), np.inf, threshold_db).min(axis=1)
        scale = 10 ** (-lowest / 10)
        alt2 = params.target_alt_km**2
        # First on squares of 4 x 4 blocks, with the same two tests below (the
        # one above, then with the sector): a square's box holds its blocks'
        # boxes, its largest value and room are its blocks' largest, and its
        # radius holds each block's circle, so its R_t is no longer and its
        # sector gain no lower than any of its blocks' (1e-6 degrees and 1e-9 dB
        # more for float error). A square that fails holds no block that passes.
        dx = np.maximum(
            0.0, np.maximum(s_box[0][None, :] - x_km[:, None], x_km[:, None] - s_box[1][None, :]) - position_km
        )
        dy = np.maximum(
            0.0, np.maximum(s_box[2][None, :] - y_km[:, None], y_km[:, None] - s_box[3][None, :]) - position_km
        )
        slant2 = dx * dx + dy * dy + alt2
        rows, sq = np.nonzero(slant2 * 1e6 <= scale[:, None] * s_room[None, :])
        r_t_m = np.maximum(np.sqrt(slant2[rows, sq]) * 1000.0 * (1 - 1e-12), _MIN_RANGE_M)
        # Every sector gain of omni carriers is 0: a call of omnis alone leaves them out.
        omni = bool(np.isnan(azimuth_deg).all())
        gain = 0.0
        if not omni:
            gain = _block_sector_db(
                s_centre[0][sq] - x_km[rows],
                s_centre[1][sq] - y_km[rows],
                s_half[sq] + position_km + 1e-9,
                azimuth_deg[rows],
                width[rows],
                rel,
                2e-6,
                cellular.max_attenuation_db,
            )
        near = s_head_max[sq] + (-20 * np.log10(r_t_m) + gain) >= lowest[rows] - 1e-9
        rows, sq = rows[near], sq[near]
        # The blocks of the squares left, carrier by carrier as np.nonzero gave them.
        each = sizes[sq]
        blocks = order[np.repeat(starts[sq] - np.cumsum(each) + each, each) + np.arange(each.sum())]
        rows = np.repeat(rows, each)
        dx = np.maximum(0.0, np.maximum(box[0][blocks] - x_km[rows], x_km[rows] - box[1][blocks]) - position_km)
        dy = np.maximum(0.0, np.maximum(box[2][blocks] - y_km[rows], y_km[rows] - box[3][blocks]) - position_km)
        slant2 = dx * dx + dy * dy + alt2
        del dx, dy
        keep = slant2 * 1e6 <= scale[rows] * room[blocks]
        rows, blocks, slant2 = rows[keep], blocks[keep], slant2[keep]
        r_t_m = np.maximum(np.sqrt(slant2) * 1000.0 * (1 - 1e-12), _MIN_RANGE_M)
        ceiling = -20 * np.log10(r_t_m)
        if not omni:
            ceiling = ceiling + _block_sector_db(
                centre[0][blocks] - x_km[rows],
                centre[1][blocks] - y_km[rows],
                half[blocks] + position_km + 1e-9,
                azimuth_deg[rows],
                width[rows],
                rel,
                1e-6,
                cellular.max_attenuation_db,
            )
        # Again with the sector: head_max is one of the block's values, and adding
        # the same float to a smaller one never gives more, so a pair dropped here
        # also fails at every azimuth.
        near = head_max[blocks] + ceiling >= lowest[rows]
        rows, blocks, ceiling, slant2 = rows[near], blocks[near], ceiling[near], slant2[near]
        lens = _bistatic_lens(x_km, y_km, params.max_bistatic_angle_deg, position_km)
        if lens is not None:
            # A block whose box lies in the lens counts for nothing: none of its cells is resolvable.
            # Its squared ground distance from the carrier, low by 1e-9 km^2, far past the float error.
            out = ~_boxes_in_lens(lens, rows, box[:, blocks], slant2 - alt2 - 1e-9)
            rows, blocks, ceiling = rows[out], blocks[out], ceiling[out]
        del slant2
        level = head[blocks]
        level += ceiling[:, None]
        reached = level >= threshold_db[rows]
        counts = np.zeros(threshold_db.shape, dtype=np.int64)
        if rows.size:
            # The pairs come carrier by carrier: one sum per carrier's run.
            runs = np.flatnonzero(np.r_[True, rows[1:] != rows[:-1]])
            weight = cells.astype(np.int8 if block * block <= 127 else np.int32)[blocks]
            counts[rows[runs]] = np.add.reduceat(reached * weight[:, None], runs, axis=0, dtype=np.int64)
        return counts.max(axis=1) * grid.cell_area_km2

    table, reach = _bound_table(*geometry)
    start, step, count = _BOUND_LATTICE_DB
    lattice = start + step * np.arange(count)
    starts = np.asarray(_BOUND_BAND_STARTS_KM)
    bands = [np.searchsorted(starts, d_edge, side="right") - 1 for d_edge in (d_lo, d_hi)]

    # The cell-side ceiling: cells that count lie within `far` of the receiver.
    lowest = np.searchsorted(lattice, threshold_db.min(axis=1), side="right") - 1
    far = np.full(d_km.size, max_range_km)
    known = lowest >= 0
    far[known] = np.maximum(reach[bands[0][known], lowest[known]], reach[bands[1][known], lowest[known]])
    near_end = d_lo - position_km
    spread = np.where(near_end > far, np.degrees(np.arcsin(np.minimum(far / np.maximum(near_end, 1e-12), 1.0))), 180.0)
    toward = np.abs(((tower_bearing + 180.0) % 360.0 - azimuth_deg + 180.0) % 360.0 - 180.0)
    toward = np.maximum(toward - spread - slack_deg - 360.0 * rel - 1e-6, 0.0)
    sector_ceiling = np.where(
        np.isnan(azimuth_deg), 0.0, -np.minimum(12.0 * (toward / width) ** 2, cellular.max_attenuation_db)
    )

    j = (
        np.searchsorted(lattice, threshold_db - sector_ceiling[:, None], side="right") - 1
    )  # step at or below; -1: below all
    cells = np.zeros(d_km.size, dtype=np.int64)
    for band in bands:
        counts = table[band[:, None], np.arange(table.shape[1])[None, :], np.maximum(j, 0)].astype(np.int64)
        counts = np.where(j < 0, grid.x_km.size, counts)
        cells = np.maximum(cells, counts.max(axis=1))
    return cells * grid.cell_area_km2


__all__ = [
    "DEFAULT_ANTENNA_HEIGHT_M",
    "DEFAULT_BAND_OFFSET_DB",
    "DEFAULT_BAND_PARAMS",
    "DEFAULT_RX_HEIGHT_M",
    "CellGrid",
    "CellularScoring",
    "FleetPrediction",
    "ScoringParams",
    "TowerScore",
    "TowerTerrain",
    "cell_grid_for",
    "class_detection_areas",
    "expected_detection_area",
    "horizon_loss_db",
    "radio_horizon_km",
    "score_towers",
    "sector_gain_db",
]
