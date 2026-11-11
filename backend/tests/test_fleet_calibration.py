"""Tests for the fleet calibration document (services/fleet_calibration.py).

The documents are written by hand from the schema-1 contract, and every expected
figure is worked out by hand in the comment beside it, so a change to the
arithmetic shows up as a wrong number rather than a self-consistent one.
"""

import json
import logging
import math
import os

import pytest
from services.fleet_calibration import CALIBRATION_FILENAME, evaluate, load_calibration


def calibration(**overrides) -> dict:
    """A valid document: the intercept, one linear, one log10 and one category term (UHF the reference)."""
    base = {
        "schema": 1,
        "kind": "retina-fleet-calibration",
        "fitted_on": "2026-11-10",
        "window": ["2026-11-04", "2026-11-10"],
        "nodes": 7,
        "intercept_db": 70.0,
        "terms": [
            {
                "name": "tower_eirp_dbm",
                "kind": "linear",
                "prior_mean_db": 0.0,
                "prior_sd_db": 0.5,
                "nodes": 7,
                "centre": 80.0,
                "coefficient_db": 0.5,
                "spread": 2.0,
            },
            {
                "name": "baseline_km",
                "kind": "log10",
                "prior_mean_db": 0.0,
                "prior_sd_db": 10.0,
                "nodes": 7,
                "centre": 1.5,
                "coefficient_db": -4.0,
                "spread": 0.25,
            },
            {
                "name": "band",
                "kind": "category",
                "prior_mean_db": 0.0,
                "prior_sd_db": 6.0,
                "nodes": 7,
                "reference": "UHF",
                "levels": [
                    {"value": "VHF", "centre": 0.5, "coefficient_db": 2.0},
                    {"value": "FM", "centre": 0.25, "coefficient_db": -3.0},
                ],
            },
        ],
        # Columns: intercept, EIRP, baseline, band=VHF, band=FM. Two 2x2 blocks with a
        # positive determinant (0.0075 and 0.2304) and a variance between them: PSD.
        "covariance_db2": [
            [0.25, 0.05, 0.0, 0.0, 0.0],
            [0.05, 0.04, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.4, 0.0, 0.0],
            [0.0, 0.0, 0.0, 0.4, 0.16],
            [0.0, 0.0, 0.0, 0.16, 0.64],
        ],
        "tau_db": 1.2,
        "tau_at_bound": False,
        "effective_parameters": 2.6,
        "dropped_terms": {"tower_antenna_height_m": "carried by fewer than 3 nodes"},
        "threshold_db": 12.0,
        "processing_gain_db": 64.8,
        "class_offsets_db": {
            "military_transport": 1.1,
            "widebody": 0.8,
            "regional_jet": 0.3,
            "narrowbody": 0.0,
            "turboprop": -0.1,
            "bizjet": -0.2,
            "light_GA": -0.4,
            "helicopter": -0.7,
        },
    }
    base.update(overrides)
    return base


def test_a_valid_document_predicts_the_system_constant_and_its_sigma(tmp_path):
    path = tmp_path / CALIBRATION_FILENAME
    path.write_text(json.dumps(calibration()))
    document = load_calibration(path)
    assert document is not None

    mean, sigma = evaluate(document, {"tower_eirp_dbm": 82.0, "baseline_km": 100.0, "band": "VHF"})

    # x = [1, 82 - 80, log10(100) - 1.5, 1 - 0.5, 0 - 0.25] = [1, 2, 0.5, 0.5, -0.25]
    # mean = 70 + 0.5 * 2 - 4 * 0.5 + 2 * 0.5 - 3 * -0.25 = 70.75
    assert mean == pytest.approx(70.75, abs=1e-9)
    # x'Cx = 0.25 + 0.04 * 4 + 0.4 * 0.25 + 0.4 * 0.25 + 0.64 * 0.0625
    #        + 2 * 0.05 * 1 * 2 + 2 * 0.16 * 0.5 * -0.25 = 0.81; sigma = sqrt(1.2^2 + 0.81) = 1.5
    assert sigma == pytest.approx(1.5, abs=1e-9)
    assert type(mean) is float and type(sigma) is float


def test_an_absent_descriptor_puts_its_term_at_the_fleet_average_and_widens_the_band():
    document = calibration()
    # EIRP missing, baseline None, a band that is neither the reference nor a level:
    # every column but the intercept's is 0.
    mean, sigma = evaluate(document, {"baseline_km": None, "band": "DAB"})

    assert mean == pytest.approx(70.0, abs=1e-9)
    # 1.2^2 + 0.25, plus each term's spread: EIRP (0.5^2 + 0.04) * 2^2 = 1.16, baseline
    # (4^2 + 0.4) * 0.25^2 = 1.025, the band 4.3675 + 6^2 (below): sigma^2 = 44.2425.
    assert sigma == pytest.approx(math.sqrt(44.2425), abs=1e-9)

    # EIRP missing, or a string where a number belongs: x = [1, 0, 0.5, 0.5, -0.25],
    # mean 69.75, x'Cx = 0.25 + 0.1 + 0.1 + 0.04 - 0.04 = 0.45; 1.44 + 0.45 + 1.16 = 3.05.
    for eirp in ({}, {"tower_eirp_dbm": "82"}):
        prediction = evaluate(document, {**eirp, "baseline_km": 100.0, "band": "VHF"})
        assert prediction == pytest.approx((69.75, math.sqrt(3.05)), abs=1e-9)

    # Band missing: x = [1, 2, 0.5, 0, 0], mean 69, x'Cx = 0.25 + 0.16 + 0.1 + 0.2 = 0.71. With
    # p = (0.5, 0.25), P = diag(p) - pp' = [[0.25, -0.125], [-0.125, 0.1875]]; b'Pb = 1 + 1.5
    # + 1.6875 and tr(P C_band) = 0.1 - 0.04 + 0.12 add 4.3675: 1.44 + 0.71 + 4.3675 = 6.5175.
    tower = {"tower_eirp_dbm": 82.0, "baseline_km": 100.0}
    assert evaluate(document, tower) == pytest.approx((69.0, math.sqrt(6.5175)), abs=1e-9)
    # A band no node held, and a bool, which reads as "true": 6^2 more, 42.5175.
    for band in ("DAB", True):
        assert evaluate(document, {**tower, "band": band}) == pytest.approx((69.0, math.sqrt(42.5175)), abs=1e-9)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"covariance_db2": [row[:4] for row in calibration()["covariance_db2"][:4]]}, "covariance_db2"),
        ({"terms": [{k: v for k, v in t.items() if k != "spread"} for t in calibration()["terms"]]}, "terms[0].spread"),
        ({"tau_at_bound": True}, "tau_at_bound"),
    ],
    ids=["covariance-size", "spread-missing", "tau-at-bound"],
)
def test_a_refused_document_warns_once_per_version_and_its_correction_loads(tmp_path, caplog, overrides, field):
    path = tmp_path / CALIBRATION_FILENAME
    path.write_text(json.dumps(calibration(**overrides)))

    with caplog.at_level(logging.WARNING, logger="services.fleet_calibration"):
        assert load_calibration(path) is None
        assert load_calibration(path) is None

    warnings = [r.getMessage() for r in caplog.records if r.name == "services.fleet_calibration"]
    assert len(warnings) == 1
    assert field in warnings[0] and str(path) in warnings[0]
    assert "using the default constants" in warnings[0]

    path.write_text(json.dumps(calibration()))
    written = path.stat()
    os.utime(path, ns=(written.st_atime_ns, written.st_mtime_ns + 1_000_000_000))
    assert load_calibration(path) == calibration()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the file mode")
def test_a_document_made_readable_again_loads_without_being_rewritten(tmp_path, caplog):
    path = tmp_path / CALIBRATION_FILENAME
    path.write_text(json.dumps(calibration()))
    os.chmod(path, 0)

    with caplog.at_level(logging.WARNING, logger="services.fleet_calibration"):
        assert load_calibration(path) is None
        assert load_calibration(path) is None
    assert len([r for r in caplog.records if r.name == "services.fleet_calibration"]) == 1

    os.chmod(path, 0o644)
    assert load_calibration(path) == calibration()


def test_a_covariance_carrying_the_six_decimal_rounding_is_accepted(tmp_path, caplog):
    path = tmp_path / CALIBRATION_FILENAME
    linear = calibration()["terms"][0]
    rounded = [[1.0, 1.000001], [1.0, 1.0]]
    path.write_text(json.dumps(calibration(terms=[linear], covariance_db2=rounded)))

    with caplog.at_level(logging.WARNING, logger="services.fleet_calibration"):
        assert load_calibration(path) is not None
    assert not [r for r in caplog.records if r.name == "services.fleet_calibration"]
