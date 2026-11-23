"""Terrain the smooth-earth model cannot see: ridges between the receiver and a cell or a tower.

Two answers, both read off a ground elevation model through an
``ElevationSource``:

  * the receiver's terrain horizon by bearing and distance, so a cell hidden
    behind a ridge can be masked: a target there is below the skyline the
    receiver sees and no amount of EIRP puts it in view;
  * the diffraction loss over the receiver-to-tower profile, by the
    delta-Bullington method of ITU-R P.526-16 s4.5: Bullington's single
    equivalent edge over the real profile, corrected by the spherical-earth
    loss (s3.2) that a Bullington edge over a smooth profile underestimates.

Geometry is the equirectangular tangent plane around the receiver that the
detection-area scoring grids on. A point at bearing theta (degrees clockwise
from north) and ground distance d km sits at

    lat = rx_lat + d cos(theta) / KM_PER_DEG
    lon = rx_lon + d sin(theta) / (KM_PER_DEG * max(cos(rx_lat), 0.01))

and a tower's ground distance and bearing come from the same plane. The earth
curves away under that plane: on a 4/3 earth (R = 6371 km, k = 4/3, the
standard refraction allowance) a point d from the receiver lies d^2 / (2 k R)
below it, and a point d1 from one end of a path and d2 from the other bulges
d1 d2 / (2 k R) above the straight line between the ends.

Elevations are metres above sea level. Every computation asks its source once,
with every point it needs in one vectorised call, because a receiver horizon
at the defaults is ~576k samples, and says how far apart its points are so the
source can answer at that resolution. A sample the source cannot answer (NaN:
no tile, or nodata) makes the whole answer None rather than a guess, so the
caller falls back to the smooth-earth model instead of treating a hole as flat
ground.

A surface model such as GLO-30 holds the tops of roofs and trees, and it
cannot say whether an antenna is above or below the ones around it. ITU-R
P.526-16 s4.5 keeps a profile's ends on the terrain under the antennas, warns
against cover heights near them making the horizon unrealistically high, and
raises a terminal below the cover to the cover height, with a separate
height-gain correction. Given ``near_km`` the receiver here is a
simplification of that: its reference surface is the terrain height under it
estimated from the surface model, the ground that the samples within
``near_km`` of it lie on with the roofs and trees among them read as outliers
(_ground_m); its antenna stands ``rx_height_m`` above that, with no separate
height-gain correction; and no sample within ``near_km`` of it (or, on a
profile, of either end) is an obstacle. 0, the default, is a bare-earth
reading: the reference is the height at the receiver's own point and every
sample counts.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple, Protocol

import numpy as np

# The scoring's tangent plane (tower_scoring.KM_PER_DEG and its cos-lat floor),
# repeated rather than imported: the scoring builds on this module, and an
# import back would be circular. Change one, change both.
KM_PER_DEG = 111.2
_MIN_COS_LAT = 0.01  # floor for cos(rx_lat) so the lon scaling stays finite at the poles

# 4/3 of the 6371 km mean earth radius: standard atmospheric refraction bends
# a radio ray down, which is the same as a straight ray over a flatter earth.
_EFFECTIVE_EARTH_RADIUS_M = 4.0 / 3.0 * 6_371_000.0

_EFFECTIVE_EARTH_RADIUS_KM = _EFFECTIVE_EARTH_RADIUS_M / 1000.0

_C_M_S = 299_792_458.0

# Below this Fresnel parameter the edge sits well under the line of sight and
# P.526's approximation is taken as no loss (it reaches ~0 dB there).
_V_NO_LOSS = -0.78

# Ground constants of the spherical-earth loss over land: relative permittivity
# and conductivity in S/m. P.526 gives the formulas; the land values are those
# of ITU-R P.452-18 s4.2.2.1 (and P.1812), the delta-Bullington method's home.
_LAND_PERMITTIVITY = 22.0
_LAND_CONDUCTIVITY_S_M = 0.003

# The least height above the profile at its own end that an antenna stands. A
# licensed altitude under the surface model (canopy or a roof at the tower) is
# raised to it, as P.526 s4.5 raises a terminal under the ground cover, and the
# spherical-earth formulas need both heights above the surface.
_MIN_ANTENNA_HEIGHT_M = 1.0

# A sample this close to near_km counts as within it, so 0.1 km steps that sum
# to 0.30000000000000004 still land inside a 0.3 km near zone.
_NEAR_SLACK_KM = 1e-9

# The ground fit under the receiver (_ground_m): Tukey's biweight constant, in
# units of the residuals' scale; 1.4826 MAD, the standard deviation of Gaussian
# residuals; and when to stop refitting. A weight that moves by less than a
# millionth of its sample's own from one round to the next has stopped
# changing, and the answer then moves by well under a millimetre.
_BIWEIGHT_K = 4.685
_MAD_TO_SIGMA = 1.4826
_GROUND_FIT_ROUNDS = 20
_GROUND_FIT_SETTLED = 1e-6


class ElevationSource(Protocol):
    """Ground elevation in metres above sea level, NaN where it is not known.

    ``spacing_deg`` is how far apart the caller's points are, in degrees of
    arc; the source may answer from data no coarser than that (0: its finest).
    """

    def elevation(self, lat: np.ndarray, lon: np.ndarray, *, spacing_deg: float = 0.0) -> np.ndarray: ...


def _elevations(dem: ElevationSource, lat: np.ndarray, lon: np.ndarray, step_km: float) -> np.ndarray | None:
    """One call to the source for every point, ``step_km`` apart; None if any of them is unknown."""
    z = np.asarray(dem.elevation(lat, lon, spacing_deg=step_km / KM_PER_DEG), dtype=float)
    if np.isnan(z).any():
        return None
    return z


def _plane_to_latlon(rx_lat: float, rx_lon: float, east_km, north_km) -> tuple[np.ndarray, np.ndarray]:
    cos_lat = max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT)
    lat = rx_lat + np.asarray(north_km, dtype=float) / KM_PER_DEG
    # Wrapped so a profile that crosses the antimeridian asks for -179.9, not 180.1.
    lon = (rx_lon + np.asarray(east_km, dtype=float) / (KM_PER_DEG * cos_lat) + 180.0) % 360.0 - 180.0
    return lat, lon


def _elevation_angle_rad(alt_m, rx_alt_m: float, ground_km) -> np.ndarray:
    """Angle above the receiver's horizontal at which a point at ``alt_m`` appears.

    The 4/3-earth drop d^2 / (2kR) sinks distant terrain and targets below the
    angle the flat plane alone would give: ~6 m at 10 km, ~380 m at 80 km.
    """
    d_m = np.asarray(ground_km, dtype=float) * 1000.0
    return np.arctan2(np.asarray(alt_m, dtype=float) - rx_alt_m - d_m**2 / (2.0 * _EFFECTIVE_EARTH_RADIUS_M), d_m)


@dataclass(frozen=True, eq=False)
class Horizon:
    """The skyline a receiver sees: for each bearing, the highest terrain angle so far, by distance.

    ``horizon_rad[b, k]`` is the largest elevation angle of any terrain sample
    on bearing ``bearings_deg[b]`` out to and including ``distances_km[k]``,
    seen from the antenna at ``rx_alt_m`` (metres above sea level), leaving out
    the samples within the near zone, which stand at -pi/2. The arrays are
    read-only.
    """

    bearings_deg: np.ndarray  # (n_bearings,) evenly spaced from 0
    distances_km: np.ndarray  # (n_distances,) step_km, 2 step_km, ... out to max_range_km
    horizon_rad: np.ndarray  # (n_bearings, n_distances) running maximum of the terrain angle
    rx_ground_m: float  # the receiver's reference surface, the terrain under it, metres above sea level
    rx_alt_m: float  # receiver antenna altitude: that surface plus the antenna's height above it

    def __post_init__(self) -> None:
        for array in (self.bearings_deg, self.distances_km, self.horizon_rad):
            array.setflags(write=False)

    def visible(self, bearing_deg, ground_km, target_alt_m) -> np.ndarray:
        """Whether a target clears every ridge between it and the receiver.

        A target at ``target_alt_m`` (metres above sea level), ``ground_km`` out
        on ``bearing_deg``, is in view when its own elevation angle is at least
        the horizon formed by the terrain strictly closer than it. Arguments
        broadcast. The bearing snaps to the nearest sampled one. A target closer
        than the first sample has no terrain in between and is visible. Past
        the sampled range the horizon is the one over every sample: terrain
        beyond ``max_range_km`` was never sampled, so a ridge out there hides
        nothing.
        """
        bearing, ground, alt = np.broadcast_arrays(
            np.asarray(bearing_deg, dtype=float),
            np.asarray(ground_km, dtype=float),
            np.asarray(target_alt_m, dtype=float),
        )
        n_bearings = self.bearings_deg.size
        b = np.rint(np.mod(bearing, 360.0) / (360.0 / n_bearings)).astype(int) % n_bearings
        closer = np.searchsorted(self.distances_km, ground, side="left")
        skyline = self.horizon_rad[b, np.maximum(closer - 1, 0)]
        return (closer == 0) | (_elevation_angle_rad(alt, self.rx_alt_m, ground) >= skyline)


def _ground_m(z: np.ndarray, east_km: np.ndarray, north_km: np.ndarray, weight: np.ndarray) -> float:
    """The terrain height at the receiver, (0, 0), under surface-model heights ``z`` around it.

    A quadratic surface c0 + c1 e + c2 n + c3 e^2 + c4 e n + c5 n^2 over the
    samples' east and north offsets, fitted by iteratively reweighted least
    squares: each sample counts its ``weight`` times Tukey's biweight of its
    residual on a scale of 1.4826 MAD of the residuals (1 m when the MAD is
    0), refitted until no weight moves by more than a millionth of the
    sample's own ``weight``, or for 20 rounds. Roofs and crowns stand metres
    to tens of metres off the ground the other samples follow, so they end at
    zero weight. The answer is c0, the fitted ground at the receiver, plus the
    median residual of the samples still weighed. A reweighting that would
    weigh every sample zero is not taken: the last fit stands, with the
    weights it was made with.
    """
    design = np.column_stack((np.ones_like(east_km), east_km, north_km, east_km**2, east_km * north_km, north_km**2))
    # Fitted about the median height, which c0 absorbs: where the samples kept
    # are flat, the residuals and the answer come out exact.
    level = float(np.median(z))
    height = z - level
    kept = weight
    for _ in range(_GROUND_FIT_ROUNDS):
        root = np.sqrt(kept)
        coef = np.linalg.lstsq(design * root[:, None], height * root, rcond=None)[0]
        residual = height - design @ coef
        mad = float(np.median(np.abs(residual - np.median(residual))))
        u = residual / (_BIWEIGHT_K * (_MAD_TO_SIGMA * mad if mad > 0 else 1.0))
        reweighted = weight * np.where(np.abs(u) < 1.0, (1.0 - u * u) ** 2, 0.0)
        if not reweighted.any():
            break
        settled = float(np.max(np.abs(reweighted - kept) / weight)) <= _GROUND_FIT_SETTLED
        kept = reweighted
        if settled:
            break
    return level + float(coef[0]) + float(np.median(residual[kept > 0]))


def rx_horizon(
    dem: ElevationSource,
    rx_lat: float,
    rx_lon: float,
    rx_height_m: float,
    max_range_km: float,
    *,
    near_km: float = 0.0,
    bearing_step_deg: float = 0.5,
    step_km: float = 0.1,
) -> Horizon | None:
    """The terrain horizon of a receiver ``rx_height_m`` above its reference surface, out to ``max_range_km``.

    Profiles run along every bearing (the step is rounded so the bearings
    divide the circle evenly) with a sample every ``step_km``. The reference
    surface is the ground (_ground_m) under the receiver's own point and every
    sample within ``near_km`` of it, each weighed by its distance out so a ring
    counts as the area it stands for, the receiver's own point as one half a
    step out; those samples are left out of the skyline (see the module
    docstring). None when the elevation of any sample, the receiver's own
    included, is unknown.
    """
    if bearing_step_deg <= 0 or step_km <= 0 or near_km < 0:
        raise ValueError("bearing_step_deg and step_km must be positive, near_km not negative")
    n_distances = math.floor(max_range_km / step_km + 1e-9)
    if n_distances < 1:
        raise ValueError("max_range_km must cover at least one step_km")
    n_bearings = max(1, round(360.0 / bearing_step_deg))
    bearings = np.arange(n_bearings) * (360.0 / n_bearings)
    distances = np.arange(1, n_distances + 1) * step_km

    theta = np.radians(bearings)[:, None]
    east_km, north_km = distances * np.sin(theta), distances * np.cos(theta)
    lat, lon = _plane_to_latlon(rx_lat, rx_lon, east_km, north_km)
    z = _elevations(dem, np.append(rx_lat, lat.ravel()), np.append(rx_lon, lon.ravel()), step_km)
    if z is None:
        return None

    rings = z[1:].reshape(n_bearings, n_distances)
    near = distances <= near_km + _NEAR_SLACK_KM
    rx_ground_m = _ground_m(
        np.append(z[0], rings[:, near]),
        np.append(0.0, east_km[:, near]),
        np.append(0.0, north_km[:, near]),
        np.append(step_km / 2.0, np.broadcast_to(distances[near], (n_bearings, int(near.sum())))),
    )
    rx_alt_m = rx_ground_m + rx_height_m
    angles = _elevation_angle_rad(rings, rx_alt_m, distances[None, :])
    angles[:, near] = -math.pi / 2  # straight down: hides nothing
    return Horizon(
        bearings_deg=bearings,
        distances_km=distances,
        horizon_rad=np.maximum.accumulate(angles, axis=1),
        rx_ground_m=rx_ground_m,
        rx_alt_m=rx_alt_m,
    )


def knife_edge_loss_db(v: float | np.ndarray) -> float | np.ndarray:
    """Diffraction loss in dB of a single knife edge with Fresnel parameter ``v``.

    ITU-R P.526-16 eq. 31: J(v) = 6.9 + 20 log10(sqrt((v - 0.1)^2 + 1) + v - 0.1)
    for v > -0.78, and 0 at or below it, where the edge sits more than ~0.55 of
    the first Fresnel zone radius under the line of sight. 6 dB at grazing
    (v = 0). Takes a float or an array and returns the same.
    """
    v = np.asarray(v, dtype=float)
    clipped = np.maximum(v, _V_NO_LOSS)  # keeps the log's argument positive where the branch discards it
    loss = np.where(v <= _V_NO_LOSS, 0.0, 6.9 + 20.0 * np.log10(np.sqrt((clipped - 0.1) ** 2 + 1.0) + clipped - 0.1))
    return float(loss) if loss.ndim == 0 else loss


def _bullington_db(
    d_km: np.ndarray, g_m: np.ndarray, hts_m: float, hrs_m: float, wavelength_m: float, ae_km: float
) -> float:
    """Bullington loss in dB over a profile, ITU-R P.526-16 eq. 49-57.

    Slopes are in m/km from each antenna, the profile raised by the earth's
    bulge 500 d (D - d) / ae. A line-of-sight path takes J at the sample with
    the largest Fresnel parameter; a trans-horizon one at the Bullington point,
    where the steepest rays from the two ends meet. Then the path-length term
    (1 - exp(-Luc / 6)) (10 + 0.02 D).
    """
    path_km = float(d_km[-1])
    d, g = d_km[1:-1], g_m[1:-1]
    rise_m = g + 500.0 * d * (path_km - d) / ae_km
    s_tim = float(np.max((rise_m - hts_m) / d))
    s_tr = (hrs_m - hts_m) / path_km
    if s_tim < s_tr:
        line_m = (hts_m * (path_km - d) + hrs_m * d) / path_km
        v = float(np.max((rise_m - line_m) * np.sqrt(0.002 * path_km / (wavelength_m * d * (path_km - d)))))
    else:
        s_rim = float(np.max((rise_m - hrs_m) / (path_km - d)))
        # The rays meet strictly between the ends unless the edge grazes the
        # line between the antennas, where the meeting point is lost to
        # rounding and v is 0.
        v = 0.0
        if s_tim + s_rim > 0:
            d_b = (hrs_m - hts_m + s_rim * path_km) / (s_tim + s_rim)
            if 0.0 < d_b < path_km:
                above_m = hts_m + s_tim * d_b - (hts_m * (path_km - d_b) + hrs_m * d_b) / path_km
                v = above_m * math.sqrt(0.002 * path_km / (wavelength_m * d_b * (path_km - d_b)))
    loss = knife_edge_loss_db(v)
    return loss + (1.0 - math.exp(-loss / 6.0)) * (10.0 + 0.02 * path_km)


def _smooth_surface_m(d_km: np.ndarray, h_m: np.ndarray, hts_m: float, hrs_m: float) -> tuple[float, float]:
    """The smooth surface's height under each antenna, ITU-R P.526-16 eq. 58-63.

    The least-squares line through the profile, lowered toward each end by
    the highest obstruction above the straight path in proportion to the angle
    it subtends there, and never above the profile at that end.
    """
    path_km = float(d_km[-1])
    step = np.diff(d_km)
    v1 = float(np.sum(step * (h_m[1:] + h_m[:-1])))
    v2 = float(np.sum(step * (h_m[1:] * (2.0 * d_km[1:] + d_km[:-1]) + h_m[:-1] * (d_km[1:] + 2.0 * d_km[:-1]))))
    hst_m = (2.0 * v1 * path_km - v2) / path_km**2
    hsr_m = (v2 - v1 * path_km) / path_km**2
    d = d_km[1:-1]
    above_m = h_m[1:-1] - (hts_m * (path_km - d) + hrs_m * d) / path_km
    highest_m = float(np.max(above_m))
    if highest_m > 0:
        a_t = float(np.max(above_m / d))
        a_r = float(np.max(above_m / (path_km - d)))
        hst_m -= highest_m * a_t / (a_t + a_r)
        hsr_m -= highest_m * a_r / (a_t + a_r)
    return min(hst_m, float(h_m[0])), min(hsr_m, float(h_m[-1]))


def _first_term_db(d_km: float, h1_m: float, h2_m: float, ae_km: float, freq_mhz: float, vertical: bool) -> float:
    """First-term spherical-earth diffraction loss in dB, ITU-R P.526-16 eq. 11a-18b, land ground."""
    # 18000 sigma / f: the imaginary part of the ground's complex relative permittivity.
    lossy = 18000.0 * _LAND_CONDUCTIVITY_S_M / freq_mhz
    k = 0.36 * (ae_km * freq_mhz) ** (-1.0 / 3.0) * ((_LAND_PERMITTIVITY - 1.0) ** 2 + lossy**2) ** -0.25
    if vertical:
        k *= math.sqrt(_LAND_PERMITTIVITY**2 + lossy**2)
    beta = (1.0 + 1.6 * k**2 + 0.67 * k**4) / (1.0 + 4.5 * k**2 + 1.53 * k**4)
    x = 2.188 * beta * freq_mhz ** (1.0 / 3.0) * ae_km ** (-2.0 / 3.0) * d_km
    if x >= 1.6:
        distance_term = 11.0 + 10.0 * math.log10(x) - 17.6 * x
    else:
        distance_term = -20.0 * math.log10(x) - 5.6488 * x**1.425
    floor = 2.0 + 20.0 * math.log10(k)

    def height_gain(h_m: float) -> float:
        b = beta * 9.575e-3 * beta * freq_mhz ** (2.0 / 3.0) * ae_km ** (-1.0 / 3.0) * h_m
        if b > 2.0:
            return max(17.6 * math.sqrt(b - 1.1) - 5.0 * math.log10(b - 1.1) - 8.0, floor)
        return max(20.0 * math.log10(b + 0.1 * b**3), floor)

    return -distance_term - height_gain(h1_m) - height_gain(h2_m)


def _spherical_earth_db(
    d_km: float, h1_m: float, h2_m: float, ae_km: float, freq_mhz: float, wavelength_m: float, vertical: bool
) -> float:
    """Spherical-earth diffraction loss in dB at any distance, ITU-R P.526-16 s3.2 eq. 21-25.

    Beyond the line-of-sight range the first term; inside it 0 where the path
    clears the earth by the required margin, else the first term over the
    radius that would just graze, scaled by the share of that margin missing.
    """
    if d_km >= math.sqrt(2.0 * ae_km) * (math.sqrt(0.001 * h1_m) + math.sqrt(0.001 * h2_m)):
        return _first_term_db(d_km, h1_m, h2_m, ae_km, freq_mhz, vertical)
    m = 250.0 * d_km**2 / (ae_km * (h1_m + h2_m))
    c = (h1_m - h2_m) / (h1_m + h2_m)
    cos_arg = min(max(1.5 * c * math.sqrt(3.0 * m / (m + 1.0) ** 3), -1.0), 1.0)
    b = 2.0 * math.sqrt((m + 1.0) / (3.0 * m)) * math.cos(math.pi / 3.0 + math.acos(cos_arg) / 3.0)
    d1 = d_km / 2.0 * (1.0 + b)
    d2 = d_km - d1
    clearance_m = ((h1_m - 500.0 * d1**2 / ae_km) * d2 + (h2_m - 500.0 * d2**2 / ae_km) * d1) / d_km
    required_m = 17.456 * math.sqrt(d1 * d2 * wavelength_m / d_km)
    if clearance_m > required_m:
        return 0.0
    grazing_ae_km = 500.0 * (d_km / (math.sqrt(h1_m) + math.sqrt(h2_m))) ** 2
    loss = _first_term_db(d_km, h1_m, h2_m, grazing_ae_km, freq_mhz, vertical)
    return 0.0 if loss < 0 else (1.0 - clearance_m / required_m) * loss


def delta_bullington_loss_db(
    d_km,
    h_m,
    hts_m: float,
    hrs_m: float,
    freq_mhz: float,
    *,
    vertical: bool = False,
    ae_km: float = _EFFECTIVE_EARTH_RADIUS_KM,
) -> float:
    """Diffraction loss in dB over a terrain profile, ITU-R P.526-16 s4.5 (delta-Bullington).

    ``d_km`` runs from 0 at the transmitter to the path length at the
    receiver, ascending, with at least one sample between the ends; ``h_m`` is
    the profile height in metres above sea level at each, the two ends
    included. ``hts_m`` and ``hrs_m`` are the antennas' altitudes in metres
    above sea level, each above the profile at its own end. ``ae_km`` is the
    effective earth radius (4/3 earth by default); the ground is land, and the
    polarisation horizontal unless ``vertical``.

    Ld = Lba + max(Lsph - Lbs, 0), eq. 66: the Bullington loss over the
    profile, plus whatever the spherical-earth loss between the antennas'
    heights above the smooth surface exceeds the Bullington loss over that
    smooth surface. On a smooth path that is the spherical-earth loss alone.
    """
    d_km = np.asarray(d_km, dtype=float)
    h_m = np.asarray(h_m, dtype=float)
    if d_km.size < 3 or d_km.shape != h_m.shape or d_km[0] != 0 or not (np.diff(d_km) > 0).all():
        raise ValueError("d_km must ascend from 0 over at least three samples, one height each")
    if not (hts_m > h_m[0] and hrs_m > h_m[-1]) or freq_mhz <= 0:
        raise ValueError("each antenna must stand above the profile at its end, and freq_mhz must be positive")
    wavelength_m = _C_M_S / (freq_mhz * 1e6)
    actual_db = _bullington_db(d_km, h_m, hts_m, hrs_m, wavelength_m, ae_km)
    hst_m, hsr_m = _smooth_surface_m(d_km, h_m, hts_m, hrs_m)
    hts_eff_m, hrs_eff_m = hts_m - hst_m, hrs_m - hsr_m
    smooth_db = _bullington_db(d_km, np.zeros_like(h_m), hts_eff_m, hrs_eff_m, wavelength_m, ae_km)
    sphere_db = _spherical_earth_db(float(d_km[-1]), hts_eff_m, hrs_eff_m, ae_km, freq_mhz, wavelength_m, vertical)
    return actual_db + max(sphere_db - smooth_db, 0.0)


class TowerEnd(NamedTuple):
    """The tower end of a path to the receiver: where it is, and where its antenna is."""

    lat: float
    lon: float
    freq_mhz: float
    altitude_m: float | None  # the antenna's altitude in metres above sea level, if known (a licence's RCAMSL)
    height_m: float  # otherwise the antenna stands this far above the surface at the tower
    vertical: bool = False  # vertical polarisation, else horizontal


def diffraction_losses_db(
    dem: ElevationSource,
    rx_lat: float,
    rx_lon: float,
    rx_ground_m: float,
    rx_height_m: float,
    towers: Sequence[TowerEnd],
    *,
    near_km: float = 0.0,
    step_km: float = 0.1,
) -> list[float] | None:
    """Diffraction loss in dB over the path from each tower to the receiver, by delta_bullington_loss_db.

    Each profile runs from the tower to the receiver with a sample every
    ``step_km`` or a little less, at the heights the source reads, 4/3 earth.
    Its ends are the surface the source reads at the tower and the receiver's
    reference surface ``rx_ground_m`` (Horizon.rx_ground_m), and in between it
    holds only the samples more than ``near_km`` from both ends (see the
    module docstring). The tower's antenna is at ``altitude_m`` or
    ``height_m`` above its end, the receiver's ``rx_height_m`` above its own,
    each at least 1 m above the profile at its end. 0.0 for a path with no
    sample left between the ends; None when the elevation of any sample of any
    path, the tower ends included, is unknown.
    """
    if step_km <= 0 or near_km < 0 or any(not tower.freq_mhz > 0 for tower in towers):
        raise ValueError("step_km and every freq_mhz must be positive, near_km not negative")
    cos_lat = max(math.cos(math.radians(rx_lat)), _MIN_COS_LAT)
    paths, lats, lons = [], [], []
    for tower in towers:
        east_km = ((tower.lon - rx_lon + 180.0) % 360.0 - 180.0) * KM_PER_DEG * cos_lat
        north_km = (tower.lat - rx_lat) * KM_PER_DEG
        path_km = math.hypot(east_km, north_km)
        n_segments = math.ceil(path_km / step_km)
        frac = np.arange(1, n_segments) / max(n_segments, 1)  # interior samples only, from the tower
        frac = frac[np.minimum(frac, 1.0 - frac) * path_km > near_km + _NEAR_SLACK_KM]
        lat, lon = _plane_to_latlon(rx_lat, rx_lon, (1.0 - frac) * east_km, (1.0 - frac) * north_km)
        paths.append((path_km, frac))
        lats += [[tower.lat], lat]
        lons += [[tower.lon], lon]
    z = _elevations(dem, np.concatenate(lats or [[]]), np.concatenate(lons or [[]]), step_km)
    if z is None:
        return None

    hrs_m = rx_ground_m + max(float(rx_height_m), _MIN_ANTENNA_HEIGHT_M)
    losses, start = [], 0
    for tower, (path_km, frac) in zip(towers, paths):
        ground_m, heights_m = float(z[start]), z[start + 1 : start + 1 + frac.size]
        start += 1 + frac.size
        if frac.size == 0:
            losses.append(0.0)
            continue
        alt_m = ground_m + tower.height_m if tower.altitude_m is None else tower.altitude_m
        hts_m = max(float(alt_m), ground_m + _MIN_ANTENNA_HEIGHT_M)
        d_km = np.concatenate(([0.0], frac, [1.0])) * path_km
        h_m = np.concatenate(([ground_m], heights_m, [rx_ground_m]))
        losses.append(delta_bullington_loss_db(d_km, h_m, hts_m, hrs_m, tower.freq_mhz, vertical=tower.vertical))
    return losses
