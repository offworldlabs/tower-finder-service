"""Tests for the terrain horizon and the diffraction loss toward a tower (services/terrain.py).

Every elevation comes from a synthetic surface laid out around a made-up
receiver, so each test states the terrain it needs and nothing else, except
the ITU-R validation profiles the delta-Bullington loss is checked against.
"""

import math

import numpy as np
import pytest
from services import terrain
from services.terrain import TowerEnd, diffraction_losses_db, knife_edge_loss_db, rx_horizon

# A synthetic receiver position, not any real site.
_RX_LAT, _RX_LON = 10.0, 20.0


class _Dem:
    """Elevation in metres from ``height_m(north_km, east_km)``, the offset from the receiver."""

    def __init__(self, height_m):
        self._height_m = height_m

    def elevation(self, lat, lon, spacing_deg=0.0):
        north_km, east_km = np.broadcast_arrays(
            (np.asarray(lat, dtype=float) - _RX_LAT) * 111.2,
            (np.asarray(lon, dtype=float) - _RX_LON) * 111.2 * math.cos(math.radians(_RX_LAT)),
        )
        return np.asarray(self._height_m(north_km, east_km), dtype=float)


_FLAT = _Dem(lambda north_km, east_km: np.zeros_like(north_km))


def _tower_north(km):
    """A 600 MHz tower ``km`` due north of the receiver, its antenna 300 m above sea level, horizontal."""
    return TowerEnd(_RX_LAT + km / 111.2, _RX_LON, 600.0, 300.0, 0.0)


def test_a_ridge_hides_what_is_behind_it_and_nothing_in_front_or_to_the_side():
    # A 300 m wall 9.5-10.5 km north of the receiver, 10 km wide; 0 m elsewhere.
    ridge = _Dem(lambda n, e: np.where((np.abs(n - 10.0) <= 0.5) & (np.abs(e) <= 5.0), 300.0, 0.0))
    horizon = rx_horizon(ridge, _RX_LAT, _RX_LON, 10.0, 30.0)
    # From the antenna at 10 m the wall's near face stands ~1.7 deg up, so its
    # shadow 20 km out reaches ~630 m: a target at 300 m there is deep inside
    # it, one at 3000 m far above it. The same 300 m target 20 km east, or 5 km
    # north (short of the wall), has only ground below the antenna in between.
    seen = horizon.visible([0.0, 0.0, 90.0, 0.0], [20.0, 20.0, 20.0, 5.0], [300.0, 3000.0, 300.0, 300.0])
    assert seen.tolist() == [False, True, True, True]


def test_a_receiver_on_a_hilltop_or_in_a_valley_stands_on_the_ground_under_it():
    # A rounded hill 400 m high at the receiver and 120 m lower 0.5 km out, and
    # the valley that mirrors it, nothing on either: the ground under the
    # receiver is 400 m, where a median of the 0.5 km near zone would read 43 m
    # low on the hill and 43 m high in the valley. Both are quadratics, which
    # the ground fit reproduces to rounding (~1e-11 m), so 1e-6 m is slack.
    def hill(north_km, east_km):
        return 400.0 - 480.0 * (north_km**2 + east_km**2)

    for ground in (_Dem(hill), _Dem(lambda n, e: 800.0 - hill(n, e))):
        horizon = rx_horizon(ground, _RX_LAT, _RX_LON, 10.0, 0.5, near_km=0.5)
        assert horizon.rx_ground_m == pytest.approx(400.0, abs=1e-6)


def test_knife_edge_loss_follows_p526_and_is_zero_well_below_the_line_of_sight():
    # 6.9 + 20 log10(sqrt((v - 0.1)^2 + 1) + v - 0.1), worked by hand.
    assert knife_edge_loss_db(0.0) == pytest.approx(6.0329, abs=1e-4)
    loss = knife_edge_loss_db(np.array([1.0, -0.5, -0.78, -3.0]))
    assert loss.tolist() == pytest.approx([13.9257, 1.9593, 0.0, 0.0], abs=1e-4)


def test_flat_ground_costs_nothing_inside_the_radio_horizon_and_the_spherical_earth_loss_beyond_it():
    # Antennas at 10 m and 300 m see each other over a 4/3 earth out to
    # sqrt(2 ae) (sqrt(0.01) + sqrt(0.3)) ~ 84 km. At 30 km the path clears the
    # earth by more than P.526 s3.2 asks; at 120 km the loss is the
    # spherical-earth first term at 600 MHz, horizontal polarisation, over land.
    towers = [_tower_north(30.0), _tower_north(120.0)]
    assert diffraction_losses_db(_FLAT, _RX_LAT, _RX_LON, 0.0, 10.0, towers) == [0.0, pytest.approx(44.994, abs=1e-3)]


# Ground in metres above sea level every 0.1 km over the 96.2 km from the
# transmitter, rural, no clutter: the Regensburg-Munich profile of the ITU-R
# SG3 validation set for P.1812 (I. Stevanovic, OFCOM CH, for WP 3M).
_VALIDATION_PROFILE_M = np.array(
    (
        "395 396 408 408 417 430 430 438 438 445 445 445 437 429 429 429 428 426 427 421 420 420 419 434 434 439 439 "
        "440 433 433 428 427 423 411 420 418 418 423 410 410 400 383 383 385 385 402 406 406 407 388 390 406 411 426 "
        "426 430 432 432 423 423 409 394 394 389 381 381 368 368 350 340 340 340 340 340 342 341 341 341 341 342 342 "
        "346 346 371 400 400 399 409 387 382 392 400 405 407 407 407 416 419 419 413 413 405 403 403 393 391 381 380 "
        "387 376 376 365 360 360 369 387 387 377 377 378 360 360 365 369 370 365 368 366 366 382 398 398 403 403 413 "
        "412 412 409 417 417 423 426 429 420 420 426 426 433 417 412 399 399 390 389 389 396 396 390 390 390 378 380 "
        "371 370 370 370 376 380 383 383 391 400 400 390 390 393 400 400 400 399 390 382 380 375 375 370 370 370 370 "
        "370 370 370 370 370 370 370 370 372 375 377 380 380 380 382 382 382 382 382 380 384 384 386 389 389 393 399 "
        "399 400 401 406 406 410 425 425 430 430 431 438 438 437 437 430 429 427 421 421 424 426 426 427 434 434 435 "
        "435 436 438 438 442 446 448 459 456 458 458 444 438 438 449 449 460 452 452 462 466 466 456 456 454 450 450 "
        "454 449 440 434 432 431 431 438 450 450 451 451 455 436 436 435 448 448 454 449 445 432 432 446 446 448 445 "
        "445 429 429 432 440 440 447 444 453 458 456 450 451 459 472 472 474 468 468 466 466 453 445 445 431 434 432 "
        "445 442 441 441 430 436 436 444 444 458 460 460 458 467 467 464 458 467 472 472 461 461 459 469 469 473 473 "
        "460 459 459 460 460 460 460 464 460 460 461 460 460 461 470 470 474 474 479 484 484 481 487 490 485 490 487 "
        "487 490 488 488 481 480 480 477 477 474 475 475 475 480 478 480 480 483 483 483 483 483 485 485 499 498 498 "
        "482 486 486 495 498 486 477 479 478 478 483 471 471 467 467 461 466 466 475 474 482 486 485 490 489 488 490 "
        "490 497 500 500 488 488 480 480 480 484 488 497 502 504 500 500 487 481 481 480 490 490 496 496 500 500 500 "
        "499 500 500 493 498 492 492 500 491 491 500 500 500 497 497 501 502 502 496 488 488 489 484 474 474 491 493 "
        "493 497 497 501 498 498 487 488 470 477 468 484 477 473 480 480 468 474 474 487 487 490 495 495 504 503 500 "
        "488 496 496 496 496 499 499 489 483 483 472 472 467 469 469 478 476 492 488 490 487 487 488 497 497 501 501 "
        "501 504 504 500 500 500 500 500 503 502 500 490 490 477 469 469 465 465 457 456 456 459 463 468 472 476 464 "
        "454 455 458 458 473 480 480 476 476 483 493 493 496 493 499 503 500 495 495 485 493 493 503 497 497 496 496 "
        "499 506 506 503 499 487 481 485 487 487 502 497 497 500 500 503 496 496 499 497 497 499 499 502 503 504 502 "
        "502 501 493 493 488 488 474 471 471 480 477 481 478 476 476 463 453 444 444 440 437 437 433 433 430 430 430 "
        "430 430 430 429 430 430 430 430 430 430 431 431 431 440 439 461 467 449 458 458 473 492 492 494 494 482 469 "
        "469 466 469 463 472 473 483 487 488 491 491 478 472 472 470 470 473 480 480 479 481 483 480 480 472 472 460 "
        "454 454 453 450 450 451 451 456 447 447 445 445 445 445 445 445 445 445 445 445 445 445 445 445 445 445 445 "
        "445 446 446 446 446 446 447 447 447 447 447 448 448 448 448 448 449 449 450 450 450 450 450 450 450 450 450 "
        "450 450 451 451 451 451 451 452 452 452 452 452 453 453 454 454 454 455 455 455 455 455 455 455 455 455 455 "
        "456 456 456 456 456 457 457 457 458 458 458 459 459 459 460 460 460 460 460 460 460 461 461 461 461 461 462 "
        "462 462 462 462 463 463 463 464 464 464 464 464 465 465 465 465 465 465 465 465 465 465 466 466 467 467 467 "
        "468 468 468 469 469 469 470 470 470 470 470 470 470 470 470 470 471 471 471 471 472 472 473 473 473 473 474 "
        "474 474 474 474 475 475 475 475 475 475 476 476 477 477 477 477 478 478 478 479 479 479 480 480 480 480 480 "
        "480 480 480 481 481 481 481 482 482 482 483 483 483 484 484 484 484 484 484 484 485 485 485 485 485 485 486 "
        "486 487 487 487 488 488 488 489 489 489 489 489 490 490 490 490 490 490 490 490 490 490 491 491 492 492 493 "
        "494 494 494 495 495 495 495 495 495 495 495 495 495 495 495 495 495 496"
    ).split(),
    dtype=float,
)


def test_the_delta_bullington_loss_matches_the_published_itu_r_validation_results():
    """Ld of the SG3 validation runs, each at the median earth radius it was
    published for: the profile above at 98.2 MHz, horizontal, with 12 m and
    19 m antennas (trans-horizon) and with 200 m ones (line of sight); flat
    land at 100 MHz, vertical, 10 m antennas, 5 km apart (inside the radio
    horizon) and 100 km apart (beyond it)."""
    hilly_km = np.linspace(0.0, 96.2, _VALIDATION_PROFILE_M.size)
    cases = [
        (hilly_km, _VALIDATION_PROFILE_M, 407.0, 515.0, 98.2, False, 8930.776786, 60.53920448),
        (hilly_km, _VALIDATION_PROFILE_M, 595.0, 696.0, 98.2, False, 8930.776786, 13.64139205),
        (np.linspace(0.0, 5.0, 501), np.zeros(501), 10.0, 10.0, 100.0, True, 8738.167287, 20.02432081),
        (np.linspace(0.0, 100.0, 101), np.zeros(101), 10.0, 10.0, 100.0, True, 8735.511968, 67.30014953),
    ]
    losses = [
        terrain.delta_bullington_loss_db(d, h, hts, hrs, f, vertical=v, ae_km=ae)
        for d, h, hts, hrs, f, v, ae, _ in cases
    ]
    assert losses == pytest.approx([case[-1] for case in cases], abs=1e-3)


def test_a_hole_in_the_elevation_data_gives_no_answer_rather_than_flat_ground():
    # No data 14-16 km north of the receiver, as where a tile is missing.
    holed = _Dem(lambda n, e: np.where(np.abs(n - 15.0) <= 1.0, np.nan, 0.0))
    assert rx_horizon(holed, _RX_LAT, _RX_LON, 10.0, 30.0) is None
    assert diffraction_losses_db(holed, _RX_LAT, _RX_LON, 0.0, 10.0, [_tower_north(30.0)]) is None
