"""Pydantic models for fleet feedback on a tower we recommended.

Two producers post the same row shape:

* ``calibration`` — a node's Auto-Calibrate, one row per candidate tower it
  tried, carrying what the tuner actually saw (``outcome``, ``max_evidence``,
  ``max_detections``, the gains it settled on).
* ``archive`` — a later retina-server job, one row per tower per window,
  carrying archive-derived aggregates (verified range, ADS-B match rate).

One table rather than two: the residual model in ``services.tower_feedback``
treats both as evidence about the same quantity (how much area that tower
really lights up from that receiver), and a shared shape keeps the ingest,
the cap and the query from being written twice.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# What Auto-Calibrate can report per candidate. Only the first three carry a
# usable residual today (see OUTCOME_MULTIPLIERS); the rest say the candidate
# was never really tested, and are stored so the fleet's coverage of a tower
# can be told apart from its verdict on one.
CALIBRATION_OUTCOMES = (
    "confirmed_track",
    "no_confirmed_track",
    "unstable_overload",
    "tuned",
    "tuning_not_applied",
    "skipped_no_time",
    "not_reached",
)
# The archive job has no per-attempt verdict: it observed the tower over a
# window and reports aggregates.
ARCHIVE_ONLY_OUTCOMES = ("observed",)

Outcome = Literal[
    "confirmed_track",
    "no_confirmed_track",
    "unstable_overload",
    "tuned",
    "tuning_not_applied",
    "skipped_no_time",
    "not_reached",
    "observed",
]


class TowerOutcome(BaseModel):
    """One fleet observation of one tower from one receiver."""

    # Unknown keys are a 422 rather than silently dropped. A node that misspells
    # `max_detections` would otherwise keep posting rows that weigh 1.0 with no
    # evidence in them, and nothing in the loop would ever say so. The cost is
    # that a node shipping a new field before the service knows it gets a 422,
    # which is loud, recoverable, and the direction we want to fail in.
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(..., min_length=1, max_length=64, description="Stable id of the reporting node")
    rx_lat: float = Field(..., ge=-90, le=90, description="Receiver latitude")
    rx_lon: float = Field(..., ge=-180, le=180, description="Receiver longitude")
    tx_lat: float = Field(..., ge=-90, le=90, description="Transmitter latitude")
    tx_lon: float = Field(..., ge=-180, le=180, description="Transmitter longitude")
    # Hz, not MHz: this is what the node's tuner is configured with, and a unit
    # conversion at the edge is one more place for a factor of 1e6 to go wrong.
    fc_hz: float = Field(..., ge=1e6, le=6e9, description="Centre frequency the node tuned, in Hz")
    callsign: str | None = Field(None, max_length=32, description="Callsign, when the node knows one")
    source: Literal["calibration", "archive"] = Field(..., description="Which producer wrote this row")
    outcome: Outcome = Field(..., description="What happened; 'observed' is archive-only")

    # ── Calibration fields ───────────────────────────────────────────────────
    max_evidence: int | None = Field(None, ge=0, le=2, description="0 none, 1 detections, 2 active track")
    max_detections: int | None = Field(None, ge=0, description="Peak detection count seen")
    duration_s: float | None = Field(None, ge=0, description="Seconds spent on this candidate")
    # Bounded, not free ints: these are hardware register values, and a row
    # claiming gain_a=10**9 is a bug upstream, not a reading.
    gain_a: int | None = Field(None, ge=0, le=255, description="Final gain A")
    gain_b: int | None = Field(None, ge=0, le=255, description="Final gain B")
    lna_state: int | None = Field(None, ge=0, le=255, description="Final LNA state")

    # ── Archive fields ───────────────────────────────────────────────────────
    verified_range_p85_km: float | None = Field(None, ge=0, le=2000, description="p85 verified range, km")
    adsb_match_rate: float | None = Field(None, ge=0, le=1, description="Fraction of tracks matched to ADS-B")
    snr_median_db: float | None = Field(None, ge=-100, le=200, description="Median SNR over the window")
    hours_observed: float | None = Field(None, ge=0, description="Hours of archive behind this row")

    # Server-filled when absent: a node with a bad clock or no NTP still lands a
    # row we can order, and "when we heard it" is kept separately as received_at.
    observed_at: datetime | None = Field(None, description="When the node observed this; server fills now()")

    @model_validator(mode="after")
    def _outcome_matches_source(self) -> "TowerOutcome":
        if self.source == "calibration" and self.outcome in ARCHIVE_ONLY_OUTCOMES:
            raise ValueError(f"outcome {self.outcome!r} is only valid for source 'archive'")
        return self


# Bounded batch. The whole body is parsed and inserted inside one request on a
# single worker, so an unbounded list is a stall for every other caller; a node
# reports at most three candidates and the archive job can page.
MAX_BATCH = 100

TowerOutcomeBatch = Annotated[list[TowerOutcome], Field(max_length=MAX_BATCH)]
