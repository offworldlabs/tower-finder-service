"""
Pydantic models for the retina-spectrum measurement payload.

The spectrum analyser produces one Measurement per detected signal.
The MeasurementPayload bundles those with the receiver's location and
optional search parameters so a single POST body is self-contained.
"""

from pydantic import BaseModel, Field


class Measurement(BaseModel):
    """A single signal detected by the spectrum analyser."""

    freq_mhz: float = Field(..., gt=0, lt=10_000, description="Centre frequency in MHz")
    snr_db: float | None = Field(None, description="Signal-to-noise ratio in dB. None for TV channels.")
    obw_fraction: float | None = Field(
        None, ge=0.0, le=1.0,
        description="Occupied bandwidth as a fraction of the channel bandwidth. None for TV channels.",
    )
    score: float = Field(..., description="Composite passive-radar suitability score")
    power_db: float | None = Field(None, description="Measured signal power in dBFS or dBm. None for FM channels.")
    band: str = Field(..., description="Band reported by the analyser: FM, VHF, or UHF")


class MeasurementPayload(BaseModel):
    """Full payload POSTed by retina-spectrum to request an enriched tower list."""

    lat: float = Field(..., ge=-90, le=90, description="Receiver latitude")
    lon: float = Field(..., ge=-180, le=180, description="Receiver longitude")
    measurements: list[Measurement] = Field(
        default_factory=list,
        description="Signals detected by the spectrum analyser",
    )
    # Optional search parameters — same semantics as the GET /api/towers query params.
    radius_km: int = Field(0, ge=0, le=300, description="Search radius (0 = use server default)")
    limit: int = Field(0, ge=0, le=200, description="Max towers to return (0 = use server default)")
    source: str = Field("auto", description="Data source: us, au, ca, or auto")
