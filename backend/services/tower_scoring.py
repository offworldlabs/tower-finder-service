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

The receiver is a Yagi, so the answer depends on where it points: the model
sweeps ``n_azimuths`` boresights and reports the best one. That azimuth is the
second output, and it is actionable — it is where the operator aims.

Pure module: no config I/O of its own. The caller resolves every knob into a
``ScoringParams`` and passes it in; ``services/tower_ranking.py`` builds one
from tower_config.json.

Ported from a validated pure-Python reference (same equation, same knobs, same
defaults) and vectorised with numpy, because the reference takes ~13 s for the
200 towers a metro search returns and this runs inside the request.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple

import numpy as np

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
    receiver and the pattern only needs a cell's bearing — so it is built once
    per distinct set of geometry knobs and reused for every tower and every
    request. That is the whole reason 200 towers score in well under a second:
    the per-tower work is one (n_azimuths x n_cells) comparison.
    """

    x_km: np.ndarray  # east offset of each cell from the receiver
    y_km: np.ndarray  # north offset
    r_r_m: np.ndarray  # receiver-to-cell slant range, target altitude included
    azimuths_deg: np.ndarray  # the boresights swept
    pattern_db: np.ndarray  # (n_azimuths, n_cells) Yagi response, relative to boresight gain
    cell_area_km2: float


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

    return CellGrid(
        x_km=x,
        y_km=y,
        r_r_m=r_r_m,
        azimuths_deg=azimuths,
        pattern_db=pattern,
        cell_area_km2=grid_km**2,
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
) -> TowerScore:
    """Detection area and best boresight for one tower, in km^2 and degrees.

    ``tower_x_km`` / ``tower_y_km`` are east/north offsets from the receiver.
    ``direct_power_dbm_override`` replaces the modelled direct-path power at the
    receiver *before* the antenna pattern is applied — a measured direct path
    (phase 2) says what the DPI really is, where the model only guesses. It
    changes the noise floor, not the target path: the energy that reaches the
    target is still the licensed EIRP.
    """
    if grid is None:
        grid = cell_grid_for(params)

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
    hloss_db = horizon_loss_db(d_tower_km, antenna_height_m, params.rx_height_m)

    if direct_power_dbm_override is None:
        direct_iso_dbm = eirp_eff - fspl_db(max(d_tower_km, _MIN_DIRECT_KM), freq_mhz) - hloss_db
    else:
        direct_iso_dbm = float(direct_power_dbm_override)

    if grid.x_km.size == 0:
        return TowerScore(0.0, 0.0, round(radio_horizon_km(antenna_height_m, params.rx_height_m), 1))

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
    counts = np.count_nonzero(detected & resolvable[None, :], axis=1)

    # argmax takes the first maximum and the azimuths ascend, so a tie picks the
    # lowest azimuth — the reference's tie-break, and stable across runs.
    best = int(np.argmax(counts))
    return TowerScore(
        expected_area_km2=float(counts[best] * grid.cell_area_km2),
        best_azimuth_deg=float(grid.azimuths_deg[best]),
        horizon_km=round(radio_horizon_km(antenna_height_m, params.rx_height_m), 1),
    )


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


def score_towers(towers: list[dict], rx_lat: float, rx_lon: float, params: ScoringParams) -> None:
    """Stamp ``expected_area_km2``, ``best_azimuth_deg`` and ``horizon_km`` in place.

    Every tower gets all three keys, always: the fields are part of the
    response contract, and a tower the model cannot score (unknown band,
    no EIRP) carries 0.0 rather than a missing key the sort would then have to
    guess at. They are written before anything can raise for the same reason.

    A per-tower ``direct_power_dbm_override`` is honoured if present — phase 2
    will set it from a measured direct path.
    """
    for tower in towers:
        tower["expected_area_km2"] = 0.0
        tower["best_azimuth_deg"] = 0.0
        tower["horizon_km"] = 0.0

    if not towers:
        return

    grid = cell_grid_for(params)
    cos_lat = max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT)

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

        # Wrapped, not a bare subtraction: a receiver just east of the
        # antimeridian and a tower just west of it are 20 km apart, not 40000.
        dlon = (float(tower["longitude"]) - rx_lon + 180.0) % 360.0 - 180.0
        x_km = dlon * KM_PER_DEG * cos_lat
        y_km = (float(tower["latitude"]) - rx_lat) * KM_PER_DEG

        score = expected_detection_area(
            x_km,
            y_km,
            float(eirp),
            float(freq_mhz),
            band,
            height_m,
            params,
            grid=grid,
            direct_power_dbm_override=tower.get("direct_power_dbm_override"),
        )
        tower["expected_area_km2"] = score.expected_area_km2
        tower["best_azimuth_deg"] = score.best_azimuth_deg
        tower["horizon_km"] = score.horizon_km


__all__ = [
    "DEFAULT_ANTENNA_HEIGHT_M",
    "DEFAULT_BAND_OFFSET_DB",
    "DEFAULT_BAND_PARAMS",
    "DEFAULT_RX_HEIGHT_M",
    "CellGrid",
    "ScoringParams",
    "TowerScore",
    "cell_grid_for",
    "expected_detection_area",
    "horizon_loss_db",
    "radio_horizon_km",
    "score_towers",
]
