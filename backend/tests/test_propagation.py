"""Tests for the terrestrial path-loss model and the under-beam derating.

Mostly relative assertions: the point of Okumura-Hata is the shape of the
loss (with distance, frequency, mast height and environment), and pinning its
absolute value would only pin the formula's constants.
"""

import json
import math

import pytest
from services import tower_ranking
from services.tower_ranking import fspl, hata_excess_loss, path_loss, received_power, underbeam_loss
from tests._helpers import device as _device
from tests._helpers import system as _system


@pytest.fixture()
def restore_config():
    saved = {name: getattr(tower_ranking, name) for name in tower_ranking.CONFIG_SETTINGS}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(tower_ranking, name, value)


def _shipped_default() -> dict:
    with (tower_ranking._SOURCE_DEFAULT_DIR / "tower_config.json").open() as f:
        return json.load(f)


@pytest.fixture()
def shipped(restore_config):
    tower_ranking.apply_config(_shipped_default())


def _propagation(restore_config, **overrides):
    cfg = _shipped_default()
    cfg["propagation"].update(overrides)
    tower_ranking.apply_config(cfg)


# ── Okumura-Hata excess over free space ──────────────────────────────────────


class TestHataExcess:
    def test_never_below_free_space(self):
        for env in tower_ranking.ENVIRONMENTS:
            for d in (0.2, 1, 5, 20, 80):
                for f in (90, 200, 550):
                    for h in (10, 100, 400):
                        assert hata_excess_loss(d, f, h, 5, env) >= 0.0

    def test_grows_with_distance(self):
        losses = [hata_excess_loss(d, 550, 100, 5, "suburban") for d in (2, 5, 10, 20, 40, 80)]
        assert losses == sorted(losses)
        assert losses[-1] > losses[0] + 10

    def test_the_excess_is_roughly_flat_across_bands(self):
        """Hata's frequency slope is close to free space's, so the excess it
        adds barely moves between VHF and UHF; the total still rises with
        frequency through the free-space term it sits on."""
        uhf, vhf = hata_excess_loss(20, 550, 100, 5, "suburban"), hata_excess_loss(20, 190, 100, 5, "suburban")
        assert abs(uhf - vhf) < 3.0
        assert vhf + fspl(20, 190) < uhf + fspl(20, 550)

    def test_environment_order(self):
        urban = hata_excess_loss(20, 550, 100, 5, "urban")
        suburban = hata_excess_loss(20, 550, 100, 5, "suburban")
        open_area = hata_excess_loss(20, 550, 100, 5, "open")
        assert urban > suburban > open_area

    def test_a_taller_mast_loses_less(self):
        assert hata_excess_loss(20, 550, 200, 5, "suburban") < hata_excess_loss(20, 550, 50, 5, "suburban")

    def test_a_higher_receiver_loses_less(self):
        assert hata_excess_loss(20, 550, 100, 10, "suburban") < hata_excess_loss(20, 550, 100, 2, "suburban")

    def test_below_one_km_the_excess_is_held(self):
        """The formula is not valid under 1 km; holding the excess there lets
        the total loss keep falling with distance through free space."""
        assert hata_excess_loss(0.3, 550, 100, 5, "suburban") == hata_excess_loss(1.0, 550, 100, 5, "suburban")

    def test_fm_is_evaluated_at_the_bottom_of_the_valid_band(self):
        assert hata_excess_loss(30, 95.5, 100, 5, "suburban") == hata_excess_loss(30, 150, 100, 5, "suburban")

    def test_masts_beyond_the_valid_height_are_clamped(self):
        assert hata_excess_loss(20, 550, 320, 5, "suburban") == hata_excess_loss(20, 550, 200, 5, "suburban")


# ── path_loss dispatch ───────────────────────────────────────────────────────


class TestPathLoss:
    def test_free_space_model_has_no_excess(self, restore_config):
        _propagation(restore_config, model="free_space")
        assert path_loss(20, 550, 100) == (fspl(20, 550), 0.0)

    def test_terrestrial_model_adds_the_excess(self, shipped):
        total, excess = path_loss(20, 550, 100)
        assert excess > 0
        assert total == pytest.approx(fspl(20, 550) + excess)

    def test_unknown_height_is_modelled_as_a_100_m_mast(self, shipped):
        assert path_loss(20, 550, None) == path_loss(20, 550, 100.0)
        assert path_loss(20, 550, 0) == path_loss(20, 550, 100.0)

    def test_degenerate_inputs_are_lossless(self, shipped):
        assert path_loss(0, 550, 100) == (0.0, 0.0)
        assert path_loss(20, 0, 100) == (0.0, 0.0)


# ── Under-beam derating ──────────────────────────────────────────────────────


class TestUnderbeam:
    def test_nothing_at_range(self, shipped):
        # 20 km from a 250 m mast is 0.7 degrees down: inside the tilted beam.
        assert underbeam_loss(20, 250, "VHF") == 0.0

    def test_capped_close_to_a_tall_mast(self, shipped):
        # 3 km from a 320 m mast: 6 degrees down, far outside a 2 degree UHF beam.
        assert underbeam_loss(3, 320, "UHF") == tower_ranking.MAX_UNDERBEAM_LOSS_DB

    def test_parabolic_inside_the_cap(self, shipped):
        depression = math.degrees(math.atan2(320, 3000))
        expected = 12.0 * ((depression - 1.0) / 8.0) ** 2
        assert underbeam_loss(3, 320, "FM") == pytest.approx(expected)
        assert expected < tower_ranking.MAX_UNDERBEAM_LOSS_DB

    def test_a_wider_beam_derates_less(self, shipped):
        assert underbeam_loss(3, 320, "FM") < underbeam_loss(3, 320, "VHF") <= underbeam_loss(3, 320, "UHF")

    def test_unknown_band_uses_the_vhf_beam(self, shipped):
        assert underbeam_loss(3, 320, None) == underbeam_loss(3, 320, "VHF")

    def test_unknown_height_is_not_derated(self, shipped):
        """No height, no angle: a zero on record is treated as unknown, not as
        a transmitter lying on the ground."""
        assert underbeam_loss(3, None, "UHF") == 0.0
        assert underbeam_loss(3, 0, "UHF") == 0.0

    def test_disabled_by_a_zero_cap(self, restore_config):
        _propagation(restore_config, max_underbeam_loss_db=0)
        assert underbeam_loss(3, 320, "UHF") == 0.0


# ── received_power ───────────────────────────────────────────────────────────


class TestReceivedPower:
    def test_free_space_model_reproduces_the_old_formula(self, restore_config):
        _propagation(restore_config, model="free_space", max_underbeam_loss_db=0)
        assert received_power(80, 20, 550) == pytest.approx(80 + tower_ranking.RX_ANTENNA_GAIN_DBI - fspl(20, 550))

    def test_terrestrial_model_is_below_free_space(self, shipped):
        free = 80 + tower_ranking.RX_ANTENNA_GAIN_DBI - fspl(20, 550)
        assert received_power(80, 20, 550, 100, "UHF") < free - 10

    def test_height_and_band_are_optional(self, shipped):
        assert received_power(80, 20, 550) == received_power(80, 20, 550, None, None)


# ── Through process_and_rank ─────────────────────────────────────────────────


class TestRanking:
    LAT, LON = 33.749, -84.388

    def _rank(self, devices):
        return tower_ranking.process_and_rank([_system(devices)], self.LAT, self.LON)

    def test_stamps_the_loss_breakdown(self, shipped):
        (tower,) = self._rank([_device(545.0, 33.93, -84.388, eirp=10_000.0, antenna_height=250)])
        assert tower["path_loss_db"] > fspl(tower["distance_km"], 545.0)
        assert tower["excess_path_loss_db"] == pytest.approx(
            tower["path_loss_db"] - fspl(tower["distance_km"], 545.0), abs=0.15
        )
        assert tower["underbeam_loss_db"] == 0.0
        assert tower["received_power_dbm"] == pytest.approx(
            tower["eirp_dbm"] + tower_ranking.RX_ANTENNA_GAIN_DBI - tower["path_loss_db"], abs=0.15
        )

    def test_a_close_tower_under_a_tall_mast_is_derated(self, shipped):
        # 3 km north of the receiver, 320 m mast, UHF: the sheet's WGGB-TV case.
        (tower,) = self._rank([_device(545.0, 33.776, -84.388, eirp=10_000.0, antenna_height=320)])
        assert tower["underbeam_loss_db"] == tower_ranking.MAX_UNDERBEAM_LOSS_DB
        assert tower["received_power_dbm"] == pytest.approx(
            tower["eirp_dbm"] + tower_ranking.RX_ANTENNA_GAIN_DBI - tower["path_loss_db"] - tower["underbeam_loss_db"],
            abs=0.15,
        )

    def test_a_record_without_a_height_borrows_its_neighbour_on_the_mast(self, shipped):
        """FCC low-power records often carry antennaHeight 0 while the
        full-power station on the same mast carries the real figure. Without
        this the LD channels at the sheet's WGGB site escape the derating the
        WGGB record gets, and outrank it from the same antenna."""
        full = _device(545.0, 33.776, -84.388, callsign="FULL", eirp=10_000.0, antenna_height=320)
        low = _device(509.0, 33.776, -84.388, callsign="LOW", eirp=1_000.0, antenna_height=0)
        towers = {t["callsign"]: t for t in self._rank([full, low])}
        assert towers["LOW"]["underbeam_loss_db"] == towers["FULL"]["underbeam_loss_db"]
        assert towers["LOW"]["excess_path_loss_db"] == pytest.approx(towers["FULL"]["excess_path_loss_db"], abs=0.5)
        assert towers["LOW"]["antenna_height_m"] == 0  # the record itself is reported as it came

    def test_a_tower_without_a_height_on_record(self, shipped):
        (tower,) = self._rank([_device(545.0, 33.776, -84.388, eirp=10_000.0, antenna_height=None)])
        assert tower["underbeam_loss_db"] == 0.0
        assert tower["excess_path_loss_db"] > 0

    def test_the_derating_can_reorder_a_close_tower_below_a_farther_one(self, shipped):
        """The reason the derating exists: 3 km under a mast is not the
        loudest signal in town, whatever free space says."""
        close = _device(545.0, 33.776, -84.388, callsign="CLOSE", eirp=10_000.0, antenna_height=320)
        farther = _device(551.0, 33.83, -84.388, callsign="FARTHER", eirp=10_000.0, antenna_height=320)
        towers = self._rank([close, farther])
        assert [t["callsign"] for t in towers] == ["FARTHER", "CLOSE"]


# ── Config ───────────────────────────────────────────────────────────────────


class TestConfig:
    def test_shipped_default_names_the_terrestrial_model(self):
        assert _shipped_default()["propagation"]["model"] == "hata"
        assert tower_ranking.validate_config(_shipped_default()) is None

    def test_absent_section_defaults_to_the_terrestrial_model(self, restore_config):
        """A deployed overlay predates this section; it must not fall back to
        free space just because nobody has PUT a config since."""
        tower_ranking.apply_config({})
        assert tower_ranking.PROPAGATION_MODEL == "hata"
        assert tower_ranking.ENVIRONMENT == "suburban"
        assert tower_ranking.RX_HEIGHT_M == 5.0
        assert tower_ranking.BEAM_TILT_DEG == 1.0
        assert tower_ranking.VERTICAL_BEAMWIDTH_DEG == tower_ranking.DEFAULT_VERTICAL_BEAMWIDTH_DEG
        assert tower_ranking.MAX_UNDERBEAM_LOSS_DB == 20.0

    def test_partial_beamwidths_merge_with_the_defaults(self, restore_config):
        tower_ranking.apply_config({"propagation": {"vertical_beamwidth_deg": {"UHF": 1.0}}})
        assert tower_ranking.VERTICAL_BEAMWIDTH_DEG == {"FM": 8.0, "VHF": 4.0, "UHF": 1.0}

    def test_applied_values_are_copied_not_referenced(self, restore_config):
        body = {"propagation": {"vertical_beamwidth_deg": {"UHF": 1.0}}}
        tower_ranking.apply_config(body)
        body["propagation"]["vertical_beamwidth_deg"]["UHF"] = 99.0
        assert tower_ranking.VERTICAL_BEAMWIDTH_DEG["UHF"] == 1.0

    @pytest.mark.parametrize(
        "section",
        [
            {"model": "longley_rice"},
            {"environment": "downtown"},
            {"rx_height_m": -1},
            {"rx_height_m": "5"},
            {"beam_tilt_deg": -0.5},
            {"max_underbeam_loss_db": None},
            {"vertical_beamwidth_deg": [8, 4, 2]},
            {"vertical_beamwidth_deg": {"UHF": 0}},
            {"vertical_beamwidth_deg": {"UHF": "2"}},
        ],
    )
    def test_rejects(self, section):
        assert tower_ranking.validate_config({"propagation": section}) is not None

    def test_non_object_section_rejected(self):
        assert tower_ranking.validate_config({"propagation": "hata"}) is not None

    def test_free_space_is_one_line_away(self, restore_config):
        _propagation(restore_config, model="free_space")
        assert tower_ranking.PROPAGATION_MODEL == "free_space"
        assert path_loss(20, 550, 100)[1] == 0.0
