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
) -> _TowerModel:
    """The model for one tower, shared by the ranking area and the class figures."""
    bp = params.band_params[band]
    bw_hz = float(bp["bw_hz"])
    cpi_s = float(bp["cpi_s"])
    eirp_eff = float(eirp_dbm) + float(params.band_offset_db.get(band, 0.0))

    lam_m = C_M_S / (freq_mhz * 1e6)
    noise_dbm = -174.0 + 10 * math.log10(bw_hz) + params.noise_figure_db
    gproc_db = 10 * math.log10(bw_hz * cpi_s)
    const_db = 10 * math.log10(lam_m**2 * params.target_rcs_m2 / (4 * math.pi) ** 3)

    d_tower_km = math.hypot(tower_x_km, tower_y_km)
    tower_bearing_deg = math.degrees(math.atan2(tower_x_km, tower_y_km)) % 360.0
    if terrain is None:
        hloss_db = horizon_loss_db(d_tower_km, antenna_height_m, params.rx_height_m)
    else:
        hloss_db = terrain.loss_db

    if direct_power_dbm_override is None:
        direct_iso_dbm = eirp_eff - fspl_db(max(d_tower_km, _MIN_DIRECT_KM), freq_mhz) - hloss_db
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
    to_rx_x, to_rx_y = -grid.x_km, -grid.y_km
    r_t_m = np.maximum(np.sqrt(to_tx_x * to_tx_x + to_tx_y * to_tx_y + params.target_alt_km**2) * 1000.0, _MIN_RANGE_M)

    # Bistatic angle at the target, in the horizontal plane as the reference does.
    dot = to_rx_x * to_tx_x + to_rx_y * to_tx_y
    cos_beta = dot / (np.hypot(to_rx_x, to_rx_y) * np.hypot(to_tx_x, to_tx_y) + 1e-9)
    beta_deg = np.degrees(np.arccos(np.clip(cos_beta, -1.0, 1.0)))
    resolvable = beta_deg <= params.max_bistatic_angle_deg
    # The one per-cell mask both the ranking count and the class counts apply.
    countable = resolvable if terrain is None else resolvable & terrain.visible

    # Everything about a cell that does not depend on where the antenna points.
    base_db = -20 * np.log10(r_t_m) - 20 * np.log10(grid.r_r_m)

    # Per azimuth: the surviving direct path raises the noise floor.
    dpi_dbm = (
        direct_iso_dbm
        + params.rx_gain_dbi
        + _yagi_pattern_db(tower_bearing_deg - grid.azimuths_deg, params.yagi_hpbw_deg, params.yagi_front_to_back_db)
        - params.cancellation_db
    )
    n_eff_dbm = 10 * np.log10(10 ** (noise_dbm / 10) + 10 ** (dpi_dbm / 10))

    # Detection test, rearranged so the only (azimuth x cell) array is the one
    # comparison: pattern + base >= threshold(azimuth).
    threshold_db = params.snr_min_db + n_eff_dbm + hloss_db - eirp_eff - params.rx_gain_dbi - const_db - gproc_db
    detected = (grid.pattern_db + base_db[None, :]) >= threshold_db[:, None]
    counts = np.count_nonzero(detected & countable[None, :], axis=1)

    # argmax takes the first maximum and the azimuths ascend, so a tie picks the
    # lowest azimuth — the reference's tie-break, and stable across runs.
    best = int(np.argmax(counts))
    score = TowerScore(
        expected_area_km2=float(counts[best] * grid.cell_area_km2),
        best_azimuth_deg=float(grid.azimuths_deg[best]),
        horizon_km=round(radio_horizon_km(antenna_height_m, params.rx_height_m), 1),
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
    ).score


def _areas_and_ranges(
    cells_db: np.ndarray, countable: np.ndarray, thresholds_db: np.ndarray, grid: CellGrid, row: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cell count, 50 % range and whether it crossed, per threshold, on one pattern row.

    The count takes every cell, the range only the cells in that row's beam: a
    node measures its range from the aircraft in its view, and over whole rings
    the Yagi's back lobe would end the walk early. A ring's share is its
    counted beam cells over all its beam cells, so a cell that is not countable
    reads as not detected, and a ring with no beam cell is skipped. The range
    is the outer edge of the last ring before the first one under half (0.0
    when there is none), or of the last ring when none falls under half.
    """
    n_thr, n_rings = thresholds_db.size, grid.ring_outer_km.size
    order = np.argsort(thresholds_db, kind="stable")
    # A cell is counted at threshold t when cells_db >= t: with the thresholds
    # sorted, exactly the first `cleared` of them, ties included (side="right").
    cleared = np.searchsorted(thresholds_db[order], cells_db[countable], side="right")
    # One bin per ring, in the beam or not, and thresholds cleared.
    slot = (grid.ring[countable] * 2 + grid.in_beam[row][countable]) * (n_thr + 1) + cleared
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

    cells_db = grid.pattern_db[model.best] + model.base_db
    counts, range_km, crossed = _areas_and_ranges(cells_db, countable, thresholds_db, grid, model.best)

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


def _query_terrain(
    rx_lat: float, rx_lon: float, scored: list[tuple[dict, float]], params: ScoringParams, grid: CellGrid
) -> list[TowerTerrain] | None:
    """The terrain of each (tower, mast height) in ``scored``, in order; None to score them all on smooth earth.

    Terrain for one tower means terrain for all of them, and it needs every
    tile under the receiver's square, rx +- max_range_km, and under the path
    to each tower, on disk or marked as sea, plus a height at every point the
    horizon and the profiles read. Absent tiles are found before a height is
    read and named in one INFO line. Any failure to read or work out the
    terrain is one WARNING naming it. The horizon and the profiles share the
    receiver's reference surface and the near zone ``terrain_near_km``.
    """
    try:
        dem = _terrain_source()
        reach_lat = params.max_range_km / KM_PER_DEG
        reach_lon = params.max_range_km / (KM_PER_DEG * max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT))
        absent = set(dem.missing(rx_lat - reach_lat, rx_lat + reach_lat, rx_lon - reach_lon, rx_lon + reach_lon))
        ends = [(float(tower["latitude"]), float(tower["longitude"])) for tower, _ in scored]
        absent.update(dem.missing_along(rx_lat, rx_lon, ends))
        if absent:
            logger.info("Terrain tiles not on disk, scoring every tower on smooth earth: %s", ", ".join(sorted(absent)))
            return None
        near_km = params.terrain_near_km
        horizon = rx_horizon(dem, rx_lat, rx_lon, params.rx_height_m, params.max_range_km, near_km=near_km)
        losses = None
        if horizon is not None:
            towers = [_tower_end(tower, height_m) for tower, height_m in scored]
            losses = diffraction_losses_db(
                dem, rx_lat, rx_lon, horizon.rx_ground_m, params.rx_height_m, towers, near_km=near_km
            )
        if losses is None:
            logger.info("Terrain tiles hold no height where the query reads them, scoring every tower on smooth earth")
            return None
        visible = _receiver_view(horizon, params, grid)
        return [TowerTerrain(visible, loss_db) for loss_db in losses]
    except Exception as exc:
        logger.warning("Terrain unreadable, scoring every tower on smooth earth: %s", exc)
        return None


def score_towers(
    towers: list[dict],
    rx_lat: float,
    rx_lon: float,
    params: ScoringParams,
    calibration: Mapping | None = None,
    rx_descriptors: Mapping[str, object] | None = None,
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
    """
    for tower in towers:
        tower["expected_area_km2"] = 0.0
        tower["best_azimuth_deg"] = 0.0
        tower["horizon_km"] = 0.0
        tower["expected_detection_area"] = None

    if not towers:
        return

    grid = cell_grid_for(params)
    cos_lat = max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT)
    if calibration is not None:
        offsets = {name: float(calibration["class_offsets_db"][name]) for name in CLASS_NAMES}
    else:
        offsets = dict(DEFAULT_CLASS_OFFSETS_DB)
    warned = False

    scored = []
    for tower in towers:
        height_m = _antenna_height_m(tower.get("antenna_height_m"))
        tower["horizon_km"] = round(radio_horizon_km(height_m, params.rx_height_m), 1)

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
        scored.append((tower, height_m))

    terrains = _query_terrain(rx_lat, rx_lon, scored, params, grid) if scored else None

    for i, (tower, height_m) in enumerate(scored):
        eirp = tower["eirp_dbm"]
        terrain = None if terrains is None else terrains[i]
        # Wrapped, not a bare subtraction: a receiver just east of the
        # antimeridian and a tower just west of it are 20 km apart, not 40000.
        dlon = (float(tower["longitude"]) - rx_lon + 180.0) % 360.0 - 180.0
        x_km = dlon * KM_PER_DEG * cos_lat
        y_km = (float(tower["latitude"]) - rx_lat) * KM_PER_DEG

        args = (x_km, y_km, float(eirp), float(tower["frequency_mhz"]), tower["band"], height_m, params)
        direct_power_dbm_override = tower.get("direct_power_dbm_override")
        try:
            fleet = None
            if calibration is not None:
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
            model = _evaluate_tower(*args, grid, direct_power_dbm_override, terrain)
            score = model.score
            if fleet is None:
                classes = _class_figures(model, grid, offsets, None, model.countable)
            else:
                # The smooth earth's cells at the ranking's boresight, which is the no-tile
                # record wherever the tiles leave that boresight where it was.
                classes = _class_figures(model, grid, offsets, fleet, model.resolvable)
            record = {
                "calibration": "fleet" if calibration is not None else "default",
                "system_constant_db": fleet.system_constant_db if fleet else None,
                "tau_db": float(calibration["tau_db"]) if calibration is not None else None,
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
                grid=grid,
                direct_power_dbm_override=direct_power_dbm_override,
                terrain=terrain,
            )
            record = None
        tower["expected_area_km2"] = score.expected_area_km2
        tower["best_azimuth_deg"] = score.best_azimuth_deg
        tower["horizon_km"] = score.horizon_km
        tower["expected_detection_area"] = record


__all__ = [
    "DEFAULT_ANTENNA_HEIGHT_M",
    "DEFAULT_BAND_OFFSET_DB",
    "DEFAULT_BAND_PARAMS",
    "DEFAULT_RX_HEIGHT_M",
    "CellGrid",
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
]
